"""이 서비스가 주고받는 데이터의 '모양'을 한곳에 모아둔 파일.

여기 있는 dataclass들이 파이프라인 전체의 공용 언어다:

    공고 수집(Notice) → 중복 통합(NoticeCluster) → 자격 판정(EligibilityReport)
    → 신청서 초안(Draft) → 제출 전 점검(CheckIssue)

기업마당과 K-Startup은 필드 이름도, 날짜 표기도, 담고 있는 정보의 양도 다르다.
그 차이는 tools/bizinfo_client.py·tools/kstartup_client.py 두 어댑터가 흡수하고,
그 바깥의 코드는 전부 아래 Notice 하나만 알면 되도록 만든다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date

# 자격 판정 결과 3분류. 이 문자열은 UI 뱃지·골든셋 CSV·DB에 그대로 쓰이므로
# 여기 정의된 값 외의 문자열이 돌아다니지 않게 한다.
VERDICT_OK = "가능"
VERDICT_NO = "불가"
VERDICT_CHECK = "확인필요"
VERDICTS = (VERDICT_OK, VERDICT_NO, VERDICT_CHECK)

# 요건을 나누는 축. 공고문 문장은 자유롭게 쓰여 있지만, 판정은 이 축들로 환원해야
# 회사 프로필과 기계적으로 대조할 수 있다.
AXIS_INDUSTRY = "업종"
AXIS_YEARS = "업력"
AXIS_REGION = "지역"
AXIS_SIZE = "규모"
AXIS_ETC = "기타"
AXES = (AXIS_INDUSTRY, AXIS_YEARS, AXIS_REGION, AXIS_SIZE, AXIS_ETC)

# 점검 결과의 심각도.
SEV_ERROR = "오류"      # 이대로 내면 반려/탈락 위험
SEV_WARN = "경고"       # 담당자가 확인해야 함
SEV_INFO = "정보"       # 참고 사항


@dataclass
class Notice:
    """수집한 공고 1건 (기업마당·K-Startup 공통 형태).

    source/source_id 조합이 원본 기관에서의 고유키이고, id는 우리 시스템 안에서
    쓰는 키다(f"{source}:{source_id}"). 두 기관이 같은 사업을 올린 경우
    cluster_id가 같아진다.
    """
    source: str                 # "기업마당" | "K-Startup"
    source_id: str              # 기관이 매긴 공고 ID (pblancId / pbanc_sn)
    title: str
    agency: str = ""            # 소관기관명
    url: str = ""               # 원문 공고 URL
    summary: str = ""           # 사업개요
    target_text: str = ""       # 지원대상 원문 (자격 판정의 주 재료)
    exclude_text: str = ""      # 신청 제외대상 원문 (K-Startup만 제공)
    support_field: str = ""     # 지원분야 (기업마당 대분류 / K-Startup 사업분류)
    region_text: str = ""       # 지원지역 원문
    apply_begin: date | None = None
    apply_end: date | None = None
    docs_text: str = ""         # 신청방법·제출서류 원문
    # 공고 첨부파일 [{kind: "서식"|"공고문", name, url}]. 기업마당만 제공한다.
    # '서식'은 신청서 한글 양식이라, 담당자가 초안을 옮겨 담을 바로 그 파일이다.
    attachments: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)   # 원본 JSON 통째로 보관
    cluster_id: str = ""        # 중복 통합 후 채워짐

    @property
    def id(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def d_day(self) -> int | None:
        """마감까지 남은 일수. 오늘이 마감이면 0, 지났으면 음수. 마감일 미상이면 None."""
        if self.apply_end is None:
            return None
        return (self.apply_end - date.today()).days

    def full_text(self) -> str:
        """자격 판정 LLM에 넣을 공고 원문 전체.

        요건 추출 결과의 quote(인용)가 '진짜 공고문에 있는 문장인지' 검사할 때도
        이 문자열을 기준으로 삼는다 — 그래서 판정에 쓰는 텍스트와 검증에 쓰는
        텍스트가 반드시 같아야 한다.
        """
        parts = [self.title, self.support_field, self.region_text, self.summary,
                 self.target_text, self.exclude_text, self.docs_text]
        return "\n".join(p for p in parts if p)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        d["d_day"] = self.d_day
        d["apply_begin"] = self.apply_begin.isoformat() if self.apply_begin else None
        d["apply_end"] = self.apply_end.isoformat() if self.apply_end else None
        d["raw"] = self.raw
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Notice":
        d = dict(d)
        d.pop("id", None)
        d.pop("d_day", None)
        for k in ("apply_begin", "apply_end"):
            v = d.get(k)
            d[k] = date.fromisoformat(v) if isinstance(v, str) and v else None
        # DB에서는 JSON 문자열로 돌아오고, 나중에 추가된 컬럼은 예전 행에서 NULL 이다
        # (ALTER TABLE 로 붙인 컬럼은 기존 행이 NULL). None 을 그대로 흘려보내면
        # 나중에 순회할 때 터지므로 여기서 빈 값으로 맞춘다.
        for k, empty in (("raw", {}), ("attachments", [])):
            v = d.get(k)
            if v is None or v == "":
                d[k] = empty
            elif isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except json.JSONDecodeError:
                    d[k] = empty
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def forms(self) -> list[dict]:
        """신청서 서식 파일만. 초안을 옮겨 담을 대상이라 따로 뽑아 쓴다."""
        return [a for a in self.attachments if a.get("kind") == "서식"]


@dataclass
class NoticeCluster:
    """같은 사업으로 판단된 공고들의 묶음.

    기업마당과 K-Startup에 중복 게시된 공고를 하나로 합친 결과다. 화면에는
    representative 1건만 보여주고, members에 원본들을 남겨 '어느 기관에서 왔는지'와
    '왜 같다고 판단했는지'를 근거로 제시한다.
    """
    cluster_id: str
    representative: Notice
    members: list[Notice] = field(default_factory=list)
    reason: str = ""            # 통합 근거 (유사도 점수 또는 LLM 판단 이유)

    @property
    def sources(self) -> list[str]:
        return sorted({m.source for m in self.members} | {self.representative.source})

    @property
    def is_merged(self) -> bool:
        return len(self.sources) > 1


@dataclass
class Requirement:
    """공고문에서 뽑아낸 자격요건 1개.

    quote가 이 dataclass의 핵심이다. LLM이 요건을 '지어내는' 것을 막기 위해,
    공고문 원문에 실제로 존재하는 문장만 근거로 인정한다(agent/eligibility.py에서
    부분 문자열 검사). 근거 없는 요건은 판정에 쓰지 않고 버린다.
    """
    axis: str                   # AXES 중 하나
    operator: str               # 포함 | 제외 | 이상 | 이하 | 범위 | 기타
    value: str                  # "제조업", "7년 이내", "광주·전남", "10인 미만" 등
    quote: str                  # 공고문 원문 인용 (근거)


@dataclass
class RequirementVerdict:
    """요건 1개에 대한 판정 결과 — 화면의 판정표 한 줄."""
    requirement: Requirement
    verdict: str                # VERDICTS 중 하나
    company_value: str          # 대조에 쓴 우리 회사 값
    reason: str                 # 왜 그렇게 판정했는지 (한 문장)


@dataclass
class EligibilityReport:
    """공고 1건에 대한 자격 판정 리포트 전체."""
    notice_id: str
    overall: str                            # VERDICTS 중 하나
    rows: list[RequirementVerdict] = field(default_factory=list)
    required_docs: list[str] = field(default_factory=list)   # 제출서류 목록
    schedule: dict = field(default_factory=dict)             # 접수 시작/마감/D-day
    note: str = ""                          # 폴백 사용 등 담당자에게 알릴 사항

    def to_dict(self) -> dict:
        return {
            "notice_id": self.notice_id,
            "overall": self.overall,
            "rows": [
                {
                    "axis": r.requirement.axis,
                    "operator": r.requirement.operator,
                    "value": r.requirement.value,
                    "quote": r.requirement.quote,
                    "verdict": r.verdict,
                    "company_value": r.company_value,
                    "reason": r.reason,
                }
                for r in self.rows
            ],
            "required_docs": self.required_docs,
            "schedule": self.schedule,
            "note": self.note,
        }


@dataclass
class DraftSection:
    """신청서 초안의 항목 1개 (예: '사업 개요', '추진 계획')."""
    title: str
    body: str
    sources: list[str] = field(default_factory=list)   # 참고한 과거 신청서 파일명


@dataclass
class Draft:
    """공고 1건에 대한 신청서 초안 전체."""
    notice_id: str
    sections: list[DraftSection] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 프로필에 값이 없어 비워둔 항목
    note: str = ""
    # 첨부 서식에서 읽어낸 정보. form_file 이 있으면 항목이 그 서식을 따른 것이다.
    form_file: str = ""
    form_fields: list[str] = field(default_factory=list)  # 값만 채우면 되는 칸

    def to_dict(self) -> dict:
        return {
            "notice_id": self.notice_id,
            "sections": [asdict(s) for s in self.sections],
            "unresolved": self.unresolved,
            "note": self.note,
            "form_file": self.form_file,
            "form_fields": self.form_fields,
        }

    def to_text(self) -> str:
        """복사해서 한글 문서에 붙여넣기 좋은 평문."""
        out = []
        for s in self.sections:
            out.append(f"■ {s.title}\n{s.body}")
        return "\n\n".join(out)


@dataclass
class CheckIssue:
    """제출 전 점검에서 발견한 문제 1건."""
    severity: str               # SEV_ERROR | SEV_WARN | SEV_INFO
    kind: str                   # "누락서류" | "수치불일치" | "표기오류" | "일정"
    where: str                  # 어느 항목/문장에서 발견했는지
    message: str
    suggestion: str = ""


@dataclass
class CompanyProfile:
    """우리 회사 프로필 — 자격 판정과 초안 생성의 기준값.

    화면에서 담당자가 직접 입력·수정하며, data/company_profile.json에 저장된다.
    initial 값은 문제 Pool이 제시한 제공 기업(전자부품 제조업/10인 미만)을 따른다.
    """
    name: str = ""
    biz_no: str = ""                    # 사업자등록번호
    industry: str = ""                  # 업종명 (예: 전자부품 제조업)
    ksic: str = ""                      # 표준산업분류 코드 (예: C262)
    founded: str = ""                   # 설립일 YYYY-MM-DD
    region: str = ""                    # 소재지 시·도 (예: 광주광역시)
    region_detail: str = ""             # 시·군·구
    employees: int = 0                  # 상시근로자 수
    revenue_krw: int = 0                # 직전연도 매출액 (원)
    company_type: str = "중소기업"      # 기업규모 구분
    ceo: str = ""
    docs_on_hand: list[str] = field(default_factory=list)   # 보유 중인 제출서류
    strengths: str = ""                 # 초안에 쓸 회사 소개/강점 문단
    extra: dict = field(default_factory=dict)               # 자유 항목

    # ── 공고가 자주 묻는 확인 항목 ──────────────────────────────────────
    # 업종·업력·지역·규모만으로는 판정이 '확인필요'에서 멈추는 요건이 많다. 실제
    # 공고문이 반복해서 묻는 것들(체납 여부, 고용조정 이력, 인증 보유…)을 프로필에
    # 미리 담아 두면 그만큼 판정이 확정된다.
    #
    # 세 가지 상태를 구분하는 게 핵심이다: True(그렇다) / False(아니다) /
    # None(모름). None은 '확인필요'로 이어진다 — 모르는 것을 아는 척하지 않는다.
    tax_arrears: bool | None = None      # 국세·지방세 체납 중인가
    closed: bool | None = None           # 휴업·폐업 상태인가
    recent_layoffs: bool | None = None   # 최근 1년 이내 고용조정(감원)이 있었나
    root_tech: bool | None = None        # 뿌리기술 활용 기업인가
    women_owned: bool | None = None      # 여성기업 확인서를 보유했나
    smart_factory: bool | None = None    # 스마트공장을 이미 구축했나
    export_usd: int | None = None        # 직전연도 수출액 (미국 달러)
    prior_support: list[str] = field(default_factory=list)  # 최근 3년 수혜 사업명

    @property
    def years(self) -> float | None:
        """업력(년). 설립일이 없거나 형식이 이상하면 None → 관련 요건은 '확인필요'."""
        if not self.founded:
            return None
        try:
            f = date.fromisoformat(self.founded)
        except ValueError:
            return None
        return (date.today() - f).days / 365.25

    def to_dict(self) -> dict:
        d = asdict(self)
        d["years"] = round(self.years, 1) if self.years is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

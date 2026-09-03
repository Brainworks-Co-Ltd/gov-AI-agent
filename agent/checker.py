"""제출 전 점검 — 담당자가 실제로 겪는 사고 세 가지를 겨냥한다.

문제 Pool이 지목한 리스크가 곧 이 모듈의 요구사항이다:
    "서류 누락 · 일정 착오 · 문서 간 수치 불일치"

전부 **규칙으로** 검사한다. LLM을 쓰지 않는 이유가 분명하다. 이 검사는 "빠진 게
없다"는 확답을 주는 게 목적인데, 매번 답이 달라질 수 있는 도구로는 그 확답을 줄 수
없다. 서류 목록 대조와 숫자 비교는 애초에 코드가 더 잘하는 일이기도 하다.
"""
from __future__ import annotations

import re

from agent.schemas import (SEV_ERROR, SEV_INFO, SEV_WARN, CheckIssue, CompanyProfile,
                           Draft, EligibilityReport, Notice)

# 같은 서류를 기관마다 다르게 부른다. 대조 전에 표기를 하나로 맞춘다.
_DOC_SYNONYMS = {
    "사업자등록증": ["사업자등록증", "사업자 등록증", "사업자등록증명원"],
    "법인등기부등본": ["법인등기부등본", "등기사항전부증명서", "법인 등기부 등본"],
    "국세납세증명서": ["국세납세증명서", "국세 납세증명서", "납세증명서(국세)"],
    "지방세납세증명서": ["지방세납세증명서", "지방세 납세증명서", "납세증명서(지방세)"],
    "중소기업확인서": ["중소기업확인서", "중소기업 확인서"],
    "재무제표": ["재무제표", "표준재무제표증명원", "결산서"],
    "4대보험 가입자명부": ["4대보험 가입자명부", "4대 보험 가입자 명부",
                          "국민연금 가입자 명부", "건강보험 가입자명부"],
}
# "국세 및 지방세 납세증명서"처럼 한 줄에 두 서류가 묶여 있는 경우를 위해,
# 표기 → 표준명 역방향 표도 만들어 둔다.
_DOC_LOOKUP = {alias: canon for canon, aliases in _DOC_SYNONYMS.items()
               for alias in aliases}

# 서류 이름처럼 보이지만 실제로는 신청 절차 안내인 조각 — 누락 경고를 내면 소음이다.
_NOT_A_DOC = ("온라인", "홈페이지", "누리집", "방문", "우편", "이메일", "접수처", "문의")

# 정부지원사업이 공통적으로 요구하는 기본 서류.
#
# 기업마당 오픈API에는 제출서류 목록 필드가 없다(reqstMthPapersCn는 이름과 달리
# 접수처·이메일 안내문이다 — 실제 552건을 확인했고, 서류 낱말이 든 것도 27건뿐이다).
# 서류 목록은 첨부된 공고문 파일 안에 있어서 API만으로는 알 수 없다.
#
# 그래서 '이 공고가 요구하는 서류'를 다 맞히려 들지 않는다. 대신 어느 사업에나
# 요구되는 기본 서류를 미리 점검한다. 담당자가 마감 직전에 발급받으러 뛰는 상황을
# 막는 게 이 점검의 실제 값어치다.
_COMMON_DOCS = [
    "사업자등록증", "국세납세증명서", "지방세납세증명서", "중소기업확인서", "재무제표",
]


def _normalize_doc(name: str) -> str:
    """서류 이름을 표준 표기로. 표에 없으면 공백만 정리해서 그대로 쓴다."""
    squeezed = re.sub(r"\s+", "", name)
    for alias, canon in _DOC_LOOKUP.items():
        if re.sub(r"\s+", "", alias) in squeezed:
            return canon
    return name.strip()


def check_documents(report: EligibilityReport, profile: CompanyProfile,
                    notice: Notice | None = None) -> list[CheckIssue]:
    """공고가 요구하는 서류 vs 우리가 가진 서류.

    '신청서'나 '사업계획서'처럼 이번에 새로 쓰는 서류는 보유 목록에 있을 리가 없으니
    누락이 아니라 '작성 대상'으로 안내한다. 이걸 구분 안 하면 매번 똑같은 경고가
    떠서 담당자가 경고 전체를 무시하게 된다.
    """
    issues: list[CheckIssue] = []
    on_hand = {_normalize_doc(d) for d in profile.docs_on_hand}

    # 공고에 신청서 서식이 첨부돼 있으면 그 파일명을 안내에 실어 준다. "양식에 옮겨
    # 담으세요"만 말하고 어느 파일인지 안 알려주면 담당자가 다시 찾아 헤매야 한다.
    forms = notice.forms() if notice else []
    form_hint = (f" 공고 첨부의 '{forms[0]['name']}' 파일을 받아 옮겨 담으세요."
                 if forms else "")

    for raw in report.required_docs:
        if any(mark in raw for mark in _NOT_A_DOC):
            continue
        name = _normalize_doc(raw)
        if name in on_hand:
            continue
        if any(k in name for k in ("신청서", "계획서", "제안서", "동의서", "확약서")):
            issues.append(CheckIssue(
                severity=SEV_INFO, kind="누락서류", where=raw,
                message=f"'{raw}'는 이번에 작성해야 하는 서류입니다.",
                suggestion="초안 탭에서 만든 내용을 공고 양식에 옮겨 담으세요."
                           + form_hint))
            continue
        issues.append(CheckIssue(
            severity=SEV_ERROR, kind="누락서류", where=raw,
            message=f"'{raw}'가 보유 서류 목록에 없습니다.",
            suggestion="발급처에서 미리 준비하거나, 이미 있다면 회사 프로필의 "
                       "보유 서류에 추가하세요."))

    # 공고문에서 서류 목록을 못 건졌을 때 — 대부분의 공고가 여기 해당한다.
    # 침묵하는 대신, 어디를 봐야 하는지 알려주고 기본 서류라도 점검한다.
    if not report.required_docs:
        docs_file = next((a for a in (notice.attachments if notice else [])
                          if a.get("kind") == "공고문"), None)
        issues.append(CheckIssue(
            severity=SEV_INFO, kind="누락서류", where="제출서류 목록",
            message="이 공고의 오픈API 응답에는 제출서류 목록이 없습니다.",
            suggestion=(f"첨부된 공고문 '{docs_file['name']}' 에서 직접 확인하세요."
                        if docs_file else "원문 공고에서 직접 확인하세요.")))

        for doc in _COMMON_DOCS:
            if doc not in on_hand:
                issues.append(CheckIssue(
                    severity=SEV_WARN, kind="누락서류", where=doc,
                    message=f"기본 서류 '{doc}'를 보유 목록에서 찾지 못했습니다.",
                    suggestion="대부분의 지원사업이 요구하는 서류입니다. 미리 발급받아 "
                               "두거나, 이미 있다면 회사 프로필에 추가하세요."))
    return issues


# 본문에서 뽑아낼 수치 — 프로필과 대조할 수 있는 것만 본다.
_NUM_PATTERNS = [
    ("상시근로자", re.compile(r"(?:상시\s*근로자|임직원|직원)\s*(\d[\d,]*)\s*명")),
    ("매출액", re.compile(r"(?:매출액?|연\s*매출)\s*(?:은|이|는)?\s*(?:약\s*)?"
                          r"(\d[\d,.]*)\s*(억원|천만원|백만원|만원)")),
    ("설립연도", re.compile(r"(\d{4})\s*년\s*(?:에\s*)?설립")),
]


def _to_krw(number: str, unit: str) -> float:
    scale = {"억원": 100_000_000, "천만원": 10_000_000,
             "백만원": 1_000_000, "만원": 10_000}[unit]
    return float(number.replace(",", "")) * scale


def check_numbers(draft: Draft, profile: CompanyProfile) -> list[CheckIssue]:
    """초안 본문의 숫자가 회사 프로필과 어긋나는지 본다.

    제공 기업이 지목한 '문서 간 수치 불일치' 리스크를 직접 겨냥한 검사다. 신청서마다
    인원 수가 다르게 적혀 있으면 심사에서 바로 걸린다.
    """
    issues: list[CheckIssue] = []
    for section in draft.sections:
        body = section.body
        for label, pattern in _NUM_PATTERNS:
            for m in pattern.finditer(body):
                if label == "상시근로자":
                    if not profile.employees:
                        continue
                    found = int(m.group(1).replace(",", ""))
                    if found != profile.employees:
                        issues.append(CheckIssue(
                            severity=SEV_ERROR, kind="수치불일치",
                            where=f"{section.title} — \"{m.group(0)}\"",
                            message=f"본문의 상시근로자 {found}명이 회사 프로필"
                                    f"({profile.employees}명)과 다릅니다.",
                            suggestion=f"{profile.employees}명으로 고치거나, 프로필 값을 "
                                       f"최신으로 갱신하세요."))
                elif label == "매출액":
                    if not profile.revenue_krw:
                        continue
                    found = _to_krw(m.group(1), m.group(2))
                    # 억 단위로 반올림해 쓰는 게 관행이라 5% 차이까지는 넘어간다.
                    if abs(found - profile.revenue_krw) / profile.revenue_krw > 0.05:
                        issues.append(CheckIssue(
                            severity=SEV_ERROR, kind="수치불일치",
                            where=f"{section.title} — \"{m.group(0)}\"",
                            message=f"본문의 매출액이 회사 프로필"
                                    f"({profile.revenue_krw / 100_000_000:.1f}억원)과 다릅니다.",
                            suggestion="프로필 값과 같게 맞추세요."))
                elif label == "설립연도":
                    if not profile.founded:
                        continue
                    if m.group(1) != profile.founded[:4]:
                        issues.append(CheckIssue(
                            severity=SEV_ERROR, kind="수치불일치",
                            where=f"{section.title} — \"{m.group(0)}\"",
                            message=f"본문의 설립연도({m.group(1)}년)가 회사 프로필"
                                    f"({profile.founded[:4]}년)과 다릅니다.",
                            suggestion=f"{profile.founded[:4]}년으로 고치세요."))
    return issues


# 표기 점검용 패턴.
_DOUBLE_SPACE_RE = re.compile(r"[가-힣A-Za-z0-9]  +[가-힣A-Za-z0-9]")
_REPEAT_PUNCT_RE = re.compile(r"([,.·])\1+")
_UNCLOSED_RE = re.compile(r"\([^)]*$")


def check_style(draft: Draft, profile: CompanyProfile) -> list[CheckIssue]:
    """표기 오류 — 회사명 표기 흔들림, 중복 공백, 문장부호, 미완성 문장.

    맞춤법 전체를 보지는 않는다(외부 API 없이 한국어 맞춤법을 제대로 보는 건 무리다).
    대신 제출 서류에서 실제로 자주 나오고, 규칙으로 확실히 잡히는 것만 본다.
    """
    issues: list[CheckIssue] = []

    # 회사명을 (주)/㈜/공백 섞어 쓰는 흔들림. 프로필 표기 하나로 통일해야 한다.
    if profile.name:
        base = re.sub(r"\(주\)|㈜|주식회사|\s+", "", profile.name)
        variants: set[str] = set()
        for section in draft.sections:
            for m in re.finditer(rf"(?:\(주\)|㈜|주식회사)?\s*{re.escape(base)}"
                                 rf"\s*(?:\(주\)|㈜|주식회사)?", section.body):
                token = m.group(0).strip()
                if token:
                    variants.add(token)
        if len(variants) > 1:
            issues.append(CheckIssue(
                severity=SEV_WARN, kind="표기오류", where="본문 전체",
                message=f"회사명 표기가 여러 가지로 섞여 있습니다: {', '.join(sorted(variants))}",
                suggestion=f"'{profile.name}' 으로 통일하세요."))

    for section in draft.sections:
        if _DOUBLE_SPACE_RE.search(section.body):
            issues.append(CheckIssue(
                severity=SEV_WARN, kind="표기오류", where=section.title,
                message="문장 안에 공백이 두 칸 이상 연속으로 들어간 곳이 있습니다.",
                suggestion="한 칸으로 정리하세요."))
        m = _REPEAT_PUNCT_RE.search(section.body)
        if m:
            issues.append(CheckIssue(
                severity=SEV_WARN, kind="표기오류", where=section.title,
                message=f"문장부호가 반복됩니다: '{m.group(0)}'",
                suggestion="하나만 남기세요."))
        if _UNCLOSED_RE.search(section.body):
            issues.append(CheckIssue(
                severity=SEV_WARN, kind="표기오류", where=section.title,
                message="괄호가 닫히지 않은 문장이 있습니다.",
                suggestion="괄호 짝을 맞추세요."))

    for key in draft.unresolved:
        issues.append(CheckIssue(
            severity=SEV_ERROR, kind="표기오류", where=f"[확인 필요: {key}]",
            message=f"'{key}' 값을 회사 프로필에서 찾지 못해 자리를 비워 두었습니다.",
            suggestion="프로필에 값을 넣거나 본문에서 직접 채우세요."))
    return issues


def check_schedule(notice: Notice) -> list[CheckIssue]:
    """일정 착오 — 마감 지남 / 임박 / 접수 시작 전."""
    d = notice.d_day
    if d is None:
        return [CheckIssue(
            severity=SEV_WARN, kind="일정", where="접수 마감일",
            message="공고문에서 마감일을 읽어내지 못했습니다.",
            suggestion="원문 공고에서 마감일을 직접 확인하세요.")]
    if d < 0:
        return [CheckIssue(
            severity=SEV_ERROR, kind="일정", where="접수 마감일",
            message=f"이미 마감된 공고입니다 (마감 {abs(d)}일 지남).",
            suggestion="차기 공고를 기다리거나 소관기관에 재공고 일정을 문의하세요.")]
    if d <= 3:
        return [CheckIssue(
            severity=SEV_WARN, kind="일정", where="접수 마감일",
            message=f"마감이 임박했습니다 (D-{d}).",
            suggestion="발급에 시간이 걸리는 서류부터 먼저 준비하세요.")]
    return []


_SEVERITY_ORDER = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2}


def run(notice: Notice, draft: Draft, report: EligibilityReport,
        profile: CompanyProfile) -> list[CheckIssue]:
    """제출 전 점검 전체. 심각한 것부터 정렬해서 돌려준다."""
    issues = (check_schedule(notice)
              + check_documents(report, profile, notice)
              + check_numbers(draft, profile)
              + check_style(draft, profile))
    issues.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 3))
    return issues

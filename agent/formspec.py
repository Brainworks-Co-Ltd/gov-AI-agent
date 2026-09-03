"""신청서 서식 파일에서 '무엇을 써야 하는지'를 읽어낸다.

지금까지 초안의 항목은 공고 본문에서 추측하거나 기본값(사업 개요·필요성·추진 계획·
기대효과)을 썼다. 그런데 실제 항목은 공고 본문이 아니라 **첨부된 서식 파일** 안에 있다.
서식을 못 읽으면 담당자는 초안을 받아 놓고도 서식을 따로 열어 항목을 맞춰 옮겨 적어야
한다. 그 왕복을 없애는 게 이 모듈의 목적이다.

서식에는 성격이 다른 두 종류가 섞여 있다.

    서술 항목  "2. 사업계획", "추진 배경 및 필요성"  → 글을 써야 한다 (초안이 할 일)
    기입란     "회사명", "사업자등록번호", "대표자"    → 값만 채우면 된다 (프로필에 있다)

둘을 갈라내야 초안이 "회사명" 항목에 문단을 쓰는 짓을 안 한다. 가르는 일은 LLM이
잘하고(자유 서식 문서 이해), 그 뒤 처리는 코드가 한다 — 이 프로젝트의 일관된 분업이다.
"""
from __future__ import annotations

import re

from agent.schemas import Notice
from tools import formdoc, hyperclova_api

# 서식 본문을 통째로 넣으면 토큰이 과하고, 항목은 대개 앞부분에 몰려 있다.
_MAX_CHARS = 7000

_SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "write_sections": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "fill_fields": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        "documents": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["write_sections", "fill_fields", "documents"],
}

_SYSTEM = (
    "너는 정부지원사업 첨부파일에서 뽑아낸 글을 읽고, **신청자가 직접 작성해야 하는 "
    "것**만 골라내는 도구다. 표가 글자로만 풀려 있어 지저분하니 문맥으로 판단하라.\n"
    "\n"
    "가장 중요한 구분이 있다. 첨부에는 두 종류의 글이 섞여 있다.\n"
    "  (가) 신청서·사업계획서 **서식** — 신청자가 빈칸을 채우고 글을 쓰는 문서\n"
    "  (나) **공고문** — 기관이 사업을 설명하는 문서\n"
    "write_sections에는 **(가)의 항목만** 넣어라. (나)의 목차는 절대 넣지 마라. "
    "다음은 공고문 목차이지 신청자가 쓰는 항목이 아니다: '사업개요', '지원대상', "
    "'지원내용', '지원규모', '신청자격', '신청방법', '접수기간', '모집대상', "
    "'선정절차', '평가방법', '추진일정', '유의사항', '기타사항', '문의처'.\n"
    "판단 기준: 그 제목 아래에 **신청자가 자기 회사 이야기를 써 넣는가?** 아니라면 빼라.\n"
    "\n"
    "세 가지로 나눠 담아라.\n"
    "1. write_sections — 신청자가 **문단을 써야 하는 항목**의 제목. 예: "
    "'추진 배경 및 필요성', '사업 추진계획', '기대효과', '기업 현황'. 서식에 적힌 제목을 "
    "그대로 쓰되 '1.', '□' 같은 번호·기호는 떼라. 서식에 나온 순서를 지켜라. "
    "첨부가 공고문뿐이라 신청자가 쓸 항목이 없으면 **빈 배열**로 두어라.\n"
    "2. fill_fields — **값만 채우는 칸**의 이름. 예: '회사명', '대표자', "
    "'사업자등록번호', '상시근로자수', '매출액'. 문단을 쓰는 항목은 넣지 마라.\n"
    "3. documents — 함께 내라고 요구하는 **제출서류 이름**. 예: '사업자등록증 사본', "
    "'중소기업 확인서'. 공고문에 적힌 제출서류 목록도 여기에는 넣어도 된다. "
    "없으면 빈 배열.\n"
    "\n"
    "규칙: 글에 실제로 있는 항목만 넣어라. 흔히 있을 법하다는 이유로 지어내지 마라."
)

# 공고문 목차에 나오는 말 — LLM이 지시를 어기고 넣어도 코드가 확정적으로 걷어낸다.
# 담당자가 초안에서 보고 싶은 건 '내가 쓸 것'이지 '기관이 설명한 것'이 아니다.
_ANNOUNCEMENT_HEADINGS = (
    "사업개요", "사업목적", "추진배경", "지원대상", "지원내용", "지원규모", "지원조건",
    "지원분야", "지원기간", "신청자격", "신청방법", "신청기간", "접수기간", "접수방법",
    "제출방법", "모집대상", "모집기간", "모집규모", "모집분야", "선정절차", "선정방법",
    "평가방법", "평가기준", "심사기준", "추진일정", "사업기간", "유의사항", "기타사항",
    "문의처", "문의사항", "담당부서", "근거법령", "공고내용", "붙임", "별첨",
)


def _is_announcement_heading(title: str) -> bool:
    squeezed = re.sub(r"\s+", "", title)
    return any(h in squeezed for h in _ANNOUNCEMENT_HEADINGS)

# LLM 없이 쓰는 규칙 추출 — 번호·기호가 붙은 제목 줄만 건진다.
_HEADING_RE = re.compile(
    r"^\s*(?:[□■○●◇◆▶]\s*|\(?\d{1,2}\s*[.)]\s*|[IVX]{1,4}\s*[.)]\s*|[가-힣]\s*[.)]\s*)"
    r"(?P<title>[가-힣A-Za-z][^\n:：]{1,28})\s*$", re.M)
# 제목처럼 보이지만 항목이 아닌 것 (안내문·주석).
_NOT_HEADING = ("주의", "유의", "참고", "붙임", "별첨", "문의", "접수", "제출처", "안내",
                "작성요령", "예시", "비고")

# 서술형 항목에 흔히 붙는 말 — 규칙 추출에서 '쓰는 항목'을 가려내는 데 쓴다.
_WRITE_MARKS = ("개요", "배경", "필요성", "계획", "목표", "내용", "방안", "효과", "활용",
                "전략", "현황", "역량", "추진", "기대")


def _rule_sections(text: str) -> tuple[list[str], list[str]]:
    """정규식 폴백. 번호가 붙은 제목 줄을 모아 서술 항목만 골라낸다."""
    write, fill = [], []
    for m in _HEADING_RE.finditer(text):
        title = re.sub(r"\s+", " ", m.group("title")).strip(" .·-")
        if not (2 <= len(title) <= 30) or any(w in title for w in _NOT_HEADING):
            continue
        bucket = write if any(w in title for w in _WRITE_MARKS) else fill
        if title not in bucket:
            bucket.append(title)
    return write, fill


def parse(text: str) -> dict:
    """서식 본문 글자 → {write_sections, fill_fields, documents, note}."""
    if not text.strip():
        return {"write_sections": [], "fill_fields": [], "documents": [],
                "note": "서식 파일에서 글자를 읽지 못했습니다."}

    if not hyperclova_api.is_configured():
        write, fill = _rule_sections(text)
        return {"write_sections": write, "fill_fields": fill, "documents": [],
                "note": "AI 미설정 — 서식의 제목 줄만 규칙으로 추렸습니다."}

    try:
        data = hyperclova_api.chat_structured(
            _SYSTEM, f"[신청서 서식에서 뽑은 글]\n{text[:_MAX_CHARS]}",
            _SPEC_SCHEMA, max_tokens=1200)
    except Exception as e:
        print(f"[알림] 서식 분석 LLM 호출 실패 — 규칙 추출로 대체합니다. ({e})")
        write, fill = _rule_sections(text)
        return {"write_sections": write, "fill_fields": fill, "documents": [],
                "note": f"AI 호출에 실패해 규칙으로 추렸습니다. ({e})"}

    def clean(values, limit) -> list[str]:
        out = []
        for v in values or []:
            s = re.sub(r"^\s*(?:[□■○●◇◆▶]|\(?\d{1,2}\s*[.)]|[가-힣]\s*[.)])\s*", "",
                       str(v))
            # 표에서 풀린 글자라 "○○등본 임대 계약서 첨부 시" 처럼 조건절이 붙어 온다.
            # 이름 뒤 군더더기를 잘라야 보유 서류와 대조가 된다.
            s = re.split(r"\s*[※(]|\s+(?:해당|필요|임대|승인)?\s*시\b", s)[0]
            s = re.sub(r"\s+", " ", s).strip(" .·-:：,")
            if 2 <= len(s) <= 40 and s not in out:
                out.append(s)
        return out[:limit]

    sections = [s for s in clean(data.get("write_sections"), 16)
                if not _is_announcement_heading(s)][:12]
    return {"write_sections": sections,
            "fill_fields": clean(data.get("fill_fields"), 20),
            "documents": clean(data.get("documents"), 20),
            "note": ""}


# 파일명으로 '신청서 서식일 가능성'을 점친다. 기관이 붙임을 아무렇게나 이름 붙이고,
# 기업마당의 서식/공고문 구분(fileNm vs printFileNm)도 자주 어긋나기 때문이다
# (실제로 '포스터.pdf', 'FAQ', '신청가이드'가 서식으로 분류돼 있었다).
_FORM_WORDS = ("신청서", "사업계획", "계획서", "신청양식", "제출서류", "서식", "양식",
               "참가신청", "참여신청", "지원신청", "동의서", "확약서", "각서", "신청 서식")
_NOT_FORM_WORDS = ("공고문", "포스터", "faq", "자주묻는", "자주 묻는", "안내문", "가이드",
                   "매뉴얼", "설명회", "리플렛", "홍보", "결과", "명단", "질의응답")


def _candidate_score(attachment: dict) -> int:
    name = attachment.get("name", "").lower()
    score = 2 if attachment.get("kind") == "서식" else 0
    score += 3 * sum(1 for w in _FORM_WORDS if w in name)
    score -= 4 * sum(1 for w in _NOT_FORM_WORDS if w in name)
    return score


def of_notice(notice: Notice) -> dict:
    """공고 첨부를 받아 열고 분석한다.

    **서식 첨부만 보지 않는다.** 기관이 신청 서식을 공고문 붙임에 넣어 두는 일이 흔하고
    (실측: 서식 첨부가 없지만 공고문은 열 수 있는 공고가 114건 중 79건), 기업마당의
    서식/공고문 구분도 자주 어긋난다. 그래서 모든 첨부를 후보로 두고, 파일명으로 매긴
    점수 순서대로 열어 보며 **신청자가 쓸 항목이 나오는 첫 파일**을 채택한다.

    항목이 끝내 안 나와도 제출서류는 건진다 — 공고문에도 제출서류 목록은 흔히 있다.
    """
    openable = [a for a in notice.attachments if formdoc.is_supported(a.get("name", ""))]
    if not openable:
        skipped = [a["name"] for a in notice.attachments]
        note = ("이 공고에는 열 수 있는 첨부가 없습니다." if not skipped else
                f"첨부가 있지만 열 수 없는 형식입니다({', '.join(skipped[:2])}). "
                f"PDF는 아직 읽지 못합니다.")
        return {"write_sections": [], "fill_fields": [], "documents": [],
                "source_file": "", "note": note}

    documents: list[str] = []
    sections: list[str] = []
    fields: list[str] = []
    source = ""
    read_names: list[str] = []
    # 첨부에서 뽑은 공고문 본문. 초안을 쓸 때 재료로 넘긴다 — 오픈API가 주는 요약은
    # 두세 줄뿐이라, 그것만으로는 추진계획·기대효과를 구체적으로 쓸 수 없다.
    notice_text = ""

    def take(spec: dict, name: str) -> None:
        """읽어낸 결과를 모은다. 제출서류는 계속 합치고, 항목은 처음 찾은 것만 쓴다."""
        nonlocal sections, fields, source
        for doc in spec.get("documents", []):
            if doc not in documents:
                documents.append(doc)
        if spec.get("write_sections") and not sections:
            sections = spec["write_sections"]
            fields = spec.get("fill_fields", [])
            source = name

    # ── 1단계: 공고문을 먼저 읽는다 ──────────────────────────────────
    # 제출서류 목록은 거의 항상 공고문에 있고(오픈API 응답에는 없다), 신청서 서식이
    # 공고문 파일 뒤에 붙어 있는 경우도 흔하다. 그래서 공고문 한 번 읽기로 서류와
    # 항목을 같이 노린다.
    for attachment in [a for a in openable if a.get("kind") == "공고문"][:2]:
        text = formdoc.read_attachment(attachment)
        if not text.strip():
            continue
        read_names.append(attachment["name"])
        notice_text = notice_text or text
        take(parse(text), attachment["name"])
        if sections:
            break

    # ── 2단계: 항목을 아직 못 얻었으면 다른 첨부(서식)를 뒤진다 ─────────
    # 여기서 멈추지 않는 게 중요하다. 예전에는 공고문에서 제출서류를 찾으면 거기서
    # 끝내 버려서, 정작 신청서 양식이 따로 붙어 있어도 읽지 않았다.
    if not sections:
        rest = sorted((a for a in openable if a["name"] not in read_names),
                      key=_candidate_score, reverse=True)
        for attachment in rest[:3]:
            text = formdoc.read_attachment(attachment)
            if not text.strip():
                continue
            read_names.append(attachment["name"])
            notice_text = notice_text or text
            take(parse(text), attachment["name"])
            if sections:
                break

    if not read_names:
        return {"write_sections": [], "fill_fields": [], "documents": [],
                "source_file": "", "note": "첨부에서 글자를 읽지 못했습니다."}

    if sections:
        note = f"'{source}' 의 항목에 맞췄습니다."
    else:
        note = (f"첨부 {len(read_names)}개를 읽었지만 신청자가 쓸 항목을 찾지 못했습니다"
                f"(공고문뿐이거나 기입란만 있는 서식입니다).")
    return {"write_sections": sections, "fill_fields": fields,
            "documents": documents, "source_file": source or read_names[0],
            "notice_text": notice_text[:6000], "note": note}

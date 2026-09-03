"""신청서 초안 생성.

담당자가 건당 2~4시간 쓴다는 그 일을, '확인·수정' 수준으로 줄이는 게 목표다.
백지에서 시작하지 않게 하는 것만으로도 대부분의 시간이 사라진다.

    ① 공고문에서 신청서가 요구하는 항목을 뽑는다 (사업 개요·필요성·추진 계획…)
    ② 항목마다 과거 신청서에서 비슷한 문항을 찾아온다 (tools/past_search.py)
    ③ 공고 내용 + 회사 프로필 + 과거 문장을 근거로 초안을 쓴다

**숫자는 AI가 만들지 않는다.**
이 모듈에서 가장 중요한 규칙이다. 매출액·상시근로자 수·설립연도 같은 값을 LLM이
그럴듯하게 지어내면, 제출 직전까지 아무도 못 알아챈다. 그래서 프롬프트에서 숫자를
쓸 자리에는 {{매출액}} 같은 자리표시자만 넣게 하고, 생성이 끝난 뒤 파이썬이 회사
프로필의 실제 값으로 바꿔 넣는다. 프로필에 없는 값은 [확인 필요: …] 로 남겨
담당자가 직접 채우게 한다 — 비어 있는 건 눈에 띄지만, 틀린 숫자는 안 띈다.
"""
from __future__ import annotations

import re

from agent.schemas import CompanyProfile, Draft, DraftSection, Notice
from tools import hyperclova_api, past_search

# 공고문이 항목을 따로 명시하지 않을 때 쓰는 기본 구성. 정부지원사업 신청서가
# 기관을 막론하고 대체로 이 흐름을 요구한다.
DEFAULT_SECTIONS = ["사업 개요", "추진 배경 및 필요성", "추진 계획", "기대효과"]

# 자리표시자 → 회사 프로필에서 가져올 값. 여기 없는 자리표시자는 전부
# "[확인 필요: …]" 로 바뀐다.
_PLACEHOLDERS: dict[str, str] = {
    "회사명": "name",
    "대표자": "ceo",
    "업종": "industry",
    "소재지": "region",
    "설립일": "founded",
    "상시근로자수": "employees",
    "매출액": "revenue_krw",
    "사업자등록번호": "biz_no",
    "기업규모": "company_type",
}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title", "body"],
            },
        }
    },
    "required": ["sections"],
}

_SECTION_SCHEMA_HINT = ", ".join(f"{{{{{k}}}}}" for k in _PLACEHOLDERS)

_DRAFT_SYSTEM = (
    "너는 중소 제조기업의 경영기획 담당자를 돕는 신청서 작성 도우미다. 주어진 공고 "
    "내용과 회사 정보, 그리고 이 회사가 예전에 제출했던 문장을 참고해 신청서 초안을 "
    "쓴다.\n"
    "규칙:\n"
    "1. **숫자를 직접 쓰지 마라.** 매출액·인원 수·설립연도·금액이 필요한 자리에는 "
    f"자리표시자만 넣어라. 쓸 수 있는 자리표시자: {_SECTION_SCHEMA_HINT}. "
    "목록에 없는 값이 필요하면 {{항목이름}} 형태로 새로 만들어 써라. 절대 임의의 "
    "숫자를 지어내지 마라.\n"
    "2. 주어진 [회사 정보]와 [참고: 과거 신청서 문장]에 없는 실적·수상·인증·거래처를 "
    "지어내지 마라. 근거가 없으면 그 내용은 아예 쓰지 마라.\n"
    "3. **분량을 충분히 쓴다.** 사업계획서에 그대로 들어갈 글이므로 한 항목당 최소 "
    "600자 이상, 3~5개 문단으로 쓴다. 한두 문장으로 끝내지 마라.\n"
    "4. 추상적인 각오나 구호로 채우지 마라. 현재 상태 → 문제 → 무엇을 어떻게 → 그래서 "
    "무엇이 달라지는지 순으로, 공정·설비·업무 흐름을 구체적으로 풀어 쓴다. 단계가 있는 "
    "내용은 1단계·2단계처럼 나눠서 쓴다.\n"
    "5. '~한다', '~이다' 같은 문어체 평서형으로 쓴다. 신청서 문체다.\n"
    "6. 요청받은 항목만, 요청받은 제목 그대로 sections에 담아라."
)

# 항목을 하나씩 따로 생성할 때 쓰는 지시. 한 번에 다 쓰게 하면 응답 길이 한도를
# 항목 수로 나눠 쓰게 돼서 항목마다 두세 문장으로 쪼그라든다 — 사업계획서에 넣기엔
# 턱없이 짧다. 항목당 한 번씩 부르면 각 항목이 제 분량을 갖는다.
_SECTION_SYSTEM = (
    "너는 중소 제조기업의 경영기획 담당자를 돕는 신청서 작성 도우미다. 지금은 신청서의 "
    "**한 항목만** 쓴다. 그 항목의 본문만 출력하고, 제목·머리말·맺음말·설명은 쓰지 마라.\n"
    "규칙:\n"
    f"1. **숫자를 직접 쓰지 마라.** 자리표시자만 쓴다: {_SECTION_SCHEMA_HINT}. "
    "목록에 없는 값이 필요하면 {{항목이름}} 형태로 새로 만들어 써라. 임의의 숫자를 "
    "절대 지어내지 마라.\n"
    "2. [회사 정보]와 [참고 문장]에 없는 실적·수상·인증·거래처를 지어내지 마라.\n"
    "3. **600~1200자로 충분히 쓴다.** 3~5개 문단으로 나누고, 사업계획서에 그대로 넣을 수 "
    "있을 만큼 구체적으로 쓴다.\n"
    "4. 구호나 각오로 채우지 마라. 현재 상태 → 문제 → 무엇을 어떻게 → 그래서 무엇이 "
    "달라지는지 순으로, 공정·설비·업무 흐름을 들어 설명한다. 단계가 있으면 1단계·2단계로 "
    "나눠 쓴다.\n"
    "5. **문체를 끝까지 통일한다.** 모든 문장을 '~한다', '~이다', '~하고자 한다' 같은 "
    "문어체 평서형으로 끝낸다. '~합니다', '~입니다', '~하겠습니다' 같은 경어체를 한 문장도 "
    "섞지 마라. 신청서는 보고 문서다.\n"
    "6. 제목을 다시 쓰지 마라. 마크다운(**, #, - )도 쓰지 마라. 본문 문장만 출력한다."
)

# 경어체가 섞여 나올 때 문어체로 되돌리는 표 (긴 어미부터 바꾼다).
# 프롬프트로 지시해도 모델이 항목마다 다른 문체로 흘러서, 한 신청서 안에서 '~합니다'와
# '~한다'가 섞였다. 사람이 읽으면 바로 티가 나므로 코드에서 확정적으로 맞춘다.
_FORMAL_ENDINGS = [
    ("하겠습니다", "하고자 한다"), ("드립니다", "드린다"),
    ("입니다", "이다"), ("합니다", "한다"), ("됩니다", "된다"),
    ("습니다", "다"),          # 있습니다→있다, 하였습니다→하였다 … 를 한꺼번에
]
# 문장 끝에서만 바꾼다. 문장 중간의 인용구까지 건드리면 어색해진다.
_FORMAL_RE = [(re.compile(re.escape(a) + r"(?=[.。]|\s|$)"), b)
              for a, b in _FORMAL_ENDINGS]


def _to_formal(text: str) -> str:
    """경어체 어미를 신청서용 문어체로 되돌린다."""
    for pattern, replacement in _FORMAL_RE:
        text = pattern.sub(replacement, text)
    return text

# ── 튜닝 모델용 평문 경로 ───────────────────────────────────────────────
# 튜닝한 모델은 Structured Outputs를 지원하지 않는다(공식 문서). 그래서 JSON 스키마로
# 강제하지 못하고, 형식을 프롬프트로 정한 뒤 평문을 파싱해야 한다.
#
# 구분자를 '■'로 잡은 이유: 신청서 본문에 거의 안 나오는 글자라 본문과 헷갈리지 않고,
# 튜닝 데이터에도 같은 형식을 쓰면 모델이 이 형식을 그대로 익힌다.
_TUNED_SYSTEM = _DRAFT_SYSTEM + (
    "\n\n출력 형식(반드시 지켜라):\n"
    "항목마다 '■ 항목제목' 한 줄을 먼저 쓰고, 다음 줄부터 그 항목의 문단을 쓴다.\n"
    "항목 사이는 빈 줄로 구분한다. 그 밖의 머리말·맺음말·설명은 쓰지 마라.\n"
    "예시:\n■ 사업 개요\n{{회사명}}은 …\n\n■ 기대효과\n본 사업을 통해 …"
)

_SECTION_MARK_RE = re.compile(r"^\s*[■□▶●]\s*(?P<title>.+?)\s*$", re.M)


def parse_sections(text: str, wanted: list[str]) -> list[tuple[str, str]]:
    """'■ 제목' 형식의 평문을 (제목, 본문) 목록으로 되돌린다.

    구분자를 못 찾으면 통째로 한 덩어리가 되는데, 그때는 요청한 항목 수만큼 쪼개는
    대신 **첫 항목 하나로** 돌려준다. 억지로 나누면 문단이 엉뚱한 항목에 붙어서,
    담당자가 그걸 알아채지 못한 채 제출할 위험이 있다.
    """
    marks = list(_SECTION_MARK_RE.finditer(text or ""))
    if not marks:
        body = (text or "").strip()
        return [(wanted[0] if wanted else "사업 개요", body)] if body else []

    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        title = m.group("title").strip(" .:：-")
        if title and body:
            out.append((title, body))
    return out


# 키가 없을 때 쓰는 뼈대 초안. 백지보다는 낫고, 숫자를 지어내지도 않는다.
_FALLBACK_BODY = {
    "사업 개요": "{{회사명}}은(는) {{소재지}}에 소재한 {{업종}} 기업으로, 본 사업을 통해 "
                 "공고에서 요구하는 지원 내용을 수행하고자 한다. (담당자 확인 후 보완 필요)",
    "추진 배경 및 필요성": "현재 당사는 아래와 같은 현안을 안고 있으며, 자체 역량만으로는 "
                          "해결이 어려워 본 사업의 지원이 필요하다. (담당자 확인 후 보완 필요)",
    "추진 계획": "본 사업 기간 동안 단계별로 과제를 수행하며, 각 단계의 산출물을 확인한 뒤 "
                 "다음 단계로 넘어간다. (담당자 확인 후 보완 필요)",
    "기대효과": "본 사업을 완료하면 당사의 현안이 개선되고, 관련 지표를 근거로 성과를 "
                "확인할 수 있게 된다. (담당자 확인 후 보완 필요)",
}


# ═══════════════════════════════════════════════════════ 항목 뽑기 · 재료 모으기

# 공고문이 "제출서류: 사업계획서(사업 개요, 추진 계획, 기대효과 포함)" 처럼 항목을
# 적어 두는 경우가 있어, 알려진 항목명이 원문에 있으면 그것을 우선한다.
_KNOWN_SECTIONS = [
    "사업 개요", "사업개요", "추진 배경", "추진배경", "필요성", "추진 계획", "추진계획",
    "사업 목표", "사업목표", "기대효과", "기대 효과", "활용 방안", "사업비 집행 계획",
]


def pick_sections(notice: Notice) -> list[str]:
    """이 공고의 신청서가 요구하는 항목 목록."""
    text = notice.full_text()
    found: list[str] = []
    for name in _KNOWN_SECTIONS:
        if name in text:
            canonical = name if " " in name else re.sub(r"(..)(..)", r"\1 \2", name)
            if canonical not in found:
                found.append(canonical)
    return found if len(found) >= 2 else list(DEFAULT_SECTIONS)


def _profile_block(p: CompanyProfile) -> str:
    """LLM에 넘길 회사 정보. 숫자는 여기 있지만, 본문에 그대로 쓰라고는 하지 않는다."""
    lines = [
        f"회사명: {p.name}", f"대표자: {p.ceo}", f"업종: {p.industry} ({p.ksic})",
        f"소재지: {p.region} {p.region_detail}", f"설립일: {p.founded}",
        f"상시근로자: {p.employees}명", f"기업규모: {p.company_type}",
    ]
    if p.strengths:
        lines.append(f"회사 강점·현안: {p.strengths}")
    for k, v in (p.extra or {}).items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _reference_block(section: str, notice: Notice,
                     passages: list) -> tuple[str, list[str]]:
    """항목별 참고 문장 + 어느 파일에서 왔는지."""
    hits = past_search.search(f"{section} {notice.title} {notice.summary}", passages)
    if not hits:
        return "(참고할 과거 문장이 없습니다)", []
    block = "\n\n".join(f"[{p.source} — {p.heading}]\n{p.text[:600]}" for _, p in hits)
    return block, [p.source for _, p in hits]


# ══════════════════════════════════════════════════════════ 자리표시자 채우기

def _format_value(attr: str, p: CompanyProfile) -> str | None:
    value = getattr(p, attr, None)
    if value in (None, "", 0):
        return None
    if attr == "revenue_krw":
        # 억 단위가 신청서에서 읽기 좋다. 딱 떨어지지 않으면 소수 한 자리.
        billions = value / 100_000_000
        return f"{billions:.0f}억원" if billions == int(billions) else f"{billions:.1f}억원"
    if attr == "employees":
        return f"{value}명"
    if attr == "founded":
        y, m, _ = value.split("-")
        return f"{y}년 {int(m)}월"
    return str(value)


def fill_placeholders(text: str, p: CompanyProfile) -> tuple[str, list[str]]:
    """{{자리표시자}}를 프로필 실제 값으로 바꾼다.

    반환: (치환된 본문, 채우지 못한 항목 목록)

    채우지 못한 자리는 지우지 않고 "[확인 필요: 매출액]" 로 남긴다. 조용히 지우면
    문장이 매끄러워져서 담당자가 빠진 걸 못 보고 그대로 제출하게 된다.
    """
    unresolved: list[str] = []

    def replace(m: re.Match) -> str:
        key = m.group(1).strip()
        attr = _PLACEHOLDERS.get(key)
        if attr:
            value = _format_value(attr, p)
            if value is not None:
                return value
        if key not in unresolved:
            unresolved.append(key)
        return f"[확인 필요: {key}]"

    return _PLACEHOLDER_RE.sub(replace, text), unresolved


# 프로필에 없는 숫자를 LLM이 그냥 써 버린 경우를 잡아내기 위한 패턴.
# (자리표시자 규칙을 어긴 경우 — checker.py의 수치 대조와 함께 이중으로 막는다.)
_SUSPICIOUS_NUMBER_RE = re.compile(
    r"\d[\d,]*\s*(?:억원|천만원|백만원|만원|명|억|퍼센트|%)")


def find_generated_numbers(text: str, allowed: set[str]) -> list[str]:
    """본문에 등장하는 숫자 표현 중, 프로필에서 채운 값이 아닌 것.

    프로필에서 치환된 값(allowed)은 정상이고, 그 외 숫자는 LLM이 만들어 넣었을
    가능성이 있어 담당자에게 알린다.
    """
    return [m.group(0) for m in _SUSPICIOUS_NUMBER_RE.finditer(text or "")
            if m.group(0).strip() not in allowed]


# ═══════════════════════════════════════════════════════════════════ 진입점

def _generate_per_section(titles: list[str], notice: Notice, profile: CompanyProfile,
                          reference_blocks: list[str]) -> tuple[list[tuple[str, str]], str]:
    """항목을 **하나씩** 생성한다. 반환: (생성된 (제목, 본문) 목록, 실패 사유)

    한 번의 호출로 모든 항목을 받으면 응답 길이 한도를 항목 수로 나눠 쓰게 돼서 항목마다
    두세 문장으로 쪼그라든다. 사업계획서에 넣을 글로는 턱없이 짧다. 항목당 한 번씩
    부르면 각 항목이 제 분량(600~1200자)을 갖는다.

    대신 호출 수가 항목 수만큼 늘어난다. 항목이 아무리 많아도 8개까지만 생성해
    한 건의 초안이 하염없이 길어지지 않게 한다.
    """
    made: list[tuple[str, str]] = []
    last_error = ""
    common = (
        f"[공고]\n제목: {notice.title}\n소관기관: {notice.agency}\n"
        f"지원분야: {notice.support_field}\n개요: {notice.summary}\n"
        f"지원대상: {notice.target_text}\n\n"
        f"[회사 정보]\n{_profile_block(profile)}\n"
    )
    for i, title in enumerate(titles[:8]):
        reference = reference_blocks[i] if i < len(reference_blocks) else ""
        user = (f"{common}\n[참고: 과거 신청서 문장]\n{reference}\n\n"
                f"[지금 쓸 항목]\n{title}\n\n"
                f"위 항목의 본문만 쓰시오.")
        try:
            body = hyperclova_api.chat(_SECTION_SYSTEM, user,
                                       max_tokens=2000, temperature=0.4).strip()
        except Exception as e:
            last_error = str(e)
            continue
        body = _clean_body(body, title)
        if body:
            made.append((title, body))
    return made, last_error


# 본문에 섞여 나오는 마크다운 강조. 신청서에 그대로 들어가면 안 되는 기호다.
_BOLD_RE = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_BULLET_RE = re.compile(r"^\s*[-*]\s+", re.M)


def _clean_body(body: str, title: str) -> str:
    """생성된 본문을 신청서에 그대로 넣을 수 있게 다듬는다.

    '본문만 쓰라'고 해도 모델이 제목을 다시 쓰고(`**과제 지원의 필요성**`), 마크다운
    강조를 섞는 일이 잦다. 한글 신청서에 `**`가 박혀 있으면 담당자가 일일이 지워야 한다.
    """
    body = _BOLD_RE.sub(r"\1", body)
    body = _BULLET_RE.sub("- ", body)

    # 첫 줄이 제목을 되풀이한 것이면 떼어낸다.
    lines = body.split("\n")
    while lines:
        head = re.sub(r"[^가-힣A-Za-z0-9]", "", lines[0])
        want = re.sub(r"[^가-힣A-Za-z0-9]", "", title)
        if head and want and (head == want or (head in want and len(head) >= 4)):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
            continue
        break
    body = _to_formal("\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def generate(notice: Notice, profile: CompanyProfile,
             sections: list[str] | None = None) -> Draft:
    """공고 1건에 대한 신청서 초안을 만든다."""
    titles = sections or pick_sections(notice)
    passages = past_search.load_passages()

    used_sources: list[str] = []
    reference_blocks: list[str] = []
    for title in titles:
        block, sources = _reference_block(title, notice, passages)
        reference_blocks.append(f"### {title}\n{block}")
        used_sources.append(", ".join(dict.fromkeys(sources)))

    note = ""
    raw_sections: list[tuple[str, str]] = []

    if hyperclova_api.is_configured():
        user = (
            f"[공고]\n제목: {notice.title}\n소관기관: {notice.agency}\n"
            f"지원분야: {notice.support_field}\n개요: {notice.summary}\n"
            f"지원대상: {notice.target_text}\n\n"
            f"[회사 정보]\n{_profile_block(profile)}\n\n"
            f"[참고: 과거 신청서 문장]\n" + "\n\n".join(reference_blocks) + "\n\n"
            f"[작성할 항목]\n" + "\n".join(f"- {t}" for t in titles)
        )

        # 튜닝한 모델이 설정돼 있으면 그쪽을 먼저 쓴다. 실패하면 기본 모델로 내려간다 —
        # 튜닝 작업이 만료·삭제됐거나 학습이 아직 안 끝났을 때 초안 기능이 통째로
        # 죽으면 안 되기 때문이다.
        task_id = hyperclova_api.tuned_task_id()
        if task_id:
            try:
                text = hyperclova_api.chat_tuned(_TUNED_SYSTEM, user, max_tokens=2000)
                raw_sections = parse_sections(text, titles)
                if raw_sections:
                    note = f"튜닝 모델({task_id})로 작성했습니다."
                else:
                    print("[알림] 튜닝 모델 응답에서 항목을 찾지 못해 기본 모델로 넘어갑니다.")
            except Exception as e:
                print(f"[알림] 튜닝 모델 호출 실패 — 기본 모델로 대체합니다. ({e})")
                note = f"튜닝 모델 호출에 실패해 기본 모델로 작성했습니다. ({e})"

        if not raw_sections:
            raw_sections, failed = _generate_per_section(
                titles, notice, profile, reference_blocks)
            if failed and not raw_sections:
                print(f"[알림] 초안 생성 LLM 호출 실패 — 뼈대 초안으로 대체합니다. ({failed})")
                note = f"AI 호출에 실패해 뼈대 초안만 넣었습니다. ({failed})"
            elif failed:
                note = f"일부 항목을 만들지 못했습니다. ({failed})"
    else:
        note = "AI가 설정되지 않아 뼈대 초안만 넣었습니다."

    if not raw_sections:
        raw_sections = [(t, _FALLBACK_BODY.get(t, _FALLBACK_BODY["사업 개요"]))
                        for t in titles]
        note = note or "AI가 항목을 만들지 못해 뼈대 초안으로 대체했습니다."

    filled: list[DraftSection] = []
    unresolved: list[str] = []
    for i, (title, body) in enumerate(raw_sections):
        text, missing = fill_placeholders(body, profile)
        for key in missing:
            if key not in unresolved:
                unresolved.append(key)
        sources = used_sources[i].split(", ") if i < len(used_sources) else []
        filled.append(DraftSection(title=title or titles[min(i, len(titles) - 1)],
                                   body=text,
                                   sources=[s for s in sources if s]))

    return Draft(notice_id=notice.id, sections=filled, unresolved=unresolved, note=note)

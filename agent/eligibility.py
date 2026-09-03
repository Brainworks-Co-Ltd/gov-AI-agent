"""자격요건 자동 판정 — 이 과제의 핵심 난이도 ②.

공고문은 사람이 읽으라고 쓴 자유 서식 문장이고, 회사 프로필은 숫자와 코드다.
둘을 맞대려면 중간에 '요건'이라는 공통 형태가 필요하다. 그래서 두 단계로 나눈다.

    1단계 (LLM)   공고문 → 구조화된 요건 목록
                  {축, 연산자, 값, **공고문 원문 인용**}
    2단계 (파이썬) 요건 목록 + 회사 프로필 → 요건별 가능/불가/확인필요

**왜 LLM에게 최종 판정을 안 맡기는가.**
"업력 7년 미만"과 "설립일 2019-03-11"을 비교하는 건 뺄셈이다. 뺄셈을 LLM에게
시키면 대체로 맞지만 가끔 틀리고, 같은 질문에 매번 같은 답을 준다는 보장이 없다.
지원 자격은 틀리면 담당자가 헛수고를 하거나 기회를 놓치는 문제라, 재현 가능한
쪽이 옳다. LLM은 자유 문장을 구조로 바꾸는 일(사람이 하면 오래 걸리고 LLM이
잘하는 일)만 맡고, 판단은 코드가 한다.

**근거 없는 요건은 버린다.**
LLM이 뽑아낸 요건에는 반드시 공고문 원문 인용(quote)이 붙어야 하고, 그 문장이
실제로 공고문에 있는지 코드가 대조한다. 없으면 지어낸 것으로 보고 폐기한다.
화면의 판정표에 인용이 항상 함께 뜨는 이유이기도 하다 — 담당자가 5초 만에
'이 판정이 맞나'를 직접 확인할 수 있어야 한다.
"""
from __future__ import annotations

import re
from datetime import date

from agent.schemas import (AXES, AXIS_ETC, AXIS_INDUSTRY, AXIS_REGION, AXIS_SIZE,
                           AXIS_YEARS, VERDICT_CHECK, VERDICT_NO, VERDICT_OK,
                           CompanyProfile, EligibilityReport, Notice, Requirement,
                           RequirementVerdict)
from tools import hyperclova_api

# LLM이 '우대사항'으로 표시한 요건. 필수 요건이 아니므로 종합 판정을 떨어뜨리지 않는다.
OP_PREFER = "우대"
OP_EXCLUDE = "제외"


# ═══════════════════════════════════════════════════════════ 1단계: 요건 추출

_REQ_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "axis": {"type": "string", "enum": list(AXES)},
                    "operator": {"type": "string",
                                 "enum": ["포함", "제외", "이상", "이하", "범위",
                                          "우대", "기타"]},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["axis", "operator", "value", "quote"],
            },
            "maxItems": 12,
        }
    },
    "required": ["requirements"],
}

_EXTRACT_SYSTEM = (
    "너는 정부지원사업 공고문에서 '신청 자격요건'만 뽑아내는 도구다. 아래 공고문을 읽고 "
    "신청 자격에 영향을 주는 조건만 requirements 배열에 넣어라.\n"
    "규칙:\n"
    "1. quote에는 반드시 공고문에 **그대로 적혀 있는 문장**을 복사해 넣어라. 요약하거나 "
    "다듬지 말고 원문 그대로여야 한다. 원문에 없는 조건은 아예 만들지 마라.\n"
    "2. axis는 업종·업력·지역·규모·기타 중 하나다. 업력은 창업 후 경과 연수, 규모는 "
    "상시근로자 수나 매출액을 뜻한다.\n"
    "3. '우선 지원', '우대한다' 처럼 필수가 아닌 조건은 operator를 '우대'로 하라.\n"
    "4. '~는 제외한다', '~는 신청할 수 없다' 처럼 배제하는 조건은 operator를 '제외'로 하라.\n"
    "5. value에는 판단 기준을 짧게 쓴다. 예: '제조업', '7년 이내', '광주광역시', "
    "'상시근로자 5인 이상 100인 미만', '국세 체납'.\n"
    "6. 지원 금액, 사업 기간, 문의처처럼 자격과 무관한 내용은 넣지 마라.\n"
    "7. '제조업의 경우 10인 미만' 처럼 예외를 두는 문장이 있으면 그 예외까지 한 요건에 "
    "담아라. quote에는 본 조건 문장과 예외 문장을 **둘 다** 넣어야 한다.\n"
    "8. 자격요건이 명시되지 않았으면 requirements를 빈 배열로 두어라."
)

# 공고문에 자주 나오는 정형 표현 — LLM 없이도 최소한의 판정이 되도록 하는 폴백.
# (정규식, 축, 연산자) 순. value는 매치된 문자열을 그대로 쓴다.
_FALLBACK_PATTERNS = [
    (re.compile(r"(창업\s*(?:후|한지)?\s*\d+\s*년\s*(?:이내|미만|이상|초과))"), AXIS_YEARS, "기타"),
    (re.compile(r"(업력\s*\d+\s*년\s*(?:이내|미만|이상|초과))"), AXIS_YEARS, "기타"),
    (re.compile(r"(\d+\s*년\s*초과\s*\d+\s*년\s*이내)"), AXIS_YEARS, "범위"),
    (re.compile(r"(상시\s*근로자\s*\d+\s*인\s*(?:이상|미만|이하|초과)"
                r"(?:\s*\d+\s*인\s*(?:이상|미만|이하|초과))?)"), AXIS_SIZE, "기타"),
    (re.compile(r"((?:제조업의?\s*경우\s*)?\d+\s*인\s*미만)"), AXIS_SIZE, "이하"),
    (re.compile(r"((?:서울|부산|대구|인천|광주|대전|울산|세종)(?:특별시|광역시)?|"
                r"(?:경기|강원|충북|충남|전북|전남|경북|경남|제주)(?:도|특별자치도)?|"
                r"충청북도|충청남도|전라북도|전라남도|경상북도|경상남도)"
                r"\s*(?:에\s*)?(?:소재|사업장|본사)"), AXIS_REGION, "포함"),
    (re.compile(r"(중소\s*제조기업|제조업|뿌리기술|여성기업|소상공인)"), AXIS_INDUSTRY, "포함"),
]


def _verify_quote(quote: str, source_text: str) -> bool:
    """인용문이 정말 공고문에 있는지 확인한다.

    LLM이 공백·줄바꿈을 흡수해서 옮기는 일이 잦아 완전 일치로는 다 걸러진다.
    그래서 공백을 모두 지운 뒤 부분 문자열로 대조한다. 너무 짧은 인용(8자 미만)은
    '아무 데나 걸리는' 조각이라 근거로 인정하지 않는다.
    """
    if not quote or len(quote.strip()) < 8:
        return False
    squeeze = lambda s: re.sub(r"\s+", "", s)
    return squeeze(quote) in squeeze(source_text)


# 인용을 자를 문장 경계. 마침표뿐 아니라 줄바꿈·슬래시도 경계로 본다 —
# 공고문은 "광주광역시 소재 중소 제조기업 / 상시근로자 5인 이상" 처럼 쓰는 일이 흔하다.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.;\n/]|(?<=다)\s(?=[가-힣])")


def _sentence_around(text: str, start: int, end: int) -> str:
    """매치 지점을 품은 **한 문장**만 잘라낸다.

    앞뒤 몇 글자씩 잘라 쓰면 옆 문장이 딸려 온다. 그러면 근거로도 부정확하고,
    판정에도 해롭다 — "상시근로자 5인 이상 중소기업. 최근 1년 이내 고용조정이 없는
    기업." 을 통째로 인용해 버리면 규모 요건이 고용조정 조건으로 오인된다.
    """
    lo = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text, 0, start):
        lo = m.end()
    hi_match = _SENTENCE_BOUNDARY_RE.search(text, end)
    hi = hi_match.start() if hi_match else len(text)
    return text[lo:hi].strip()


def _fallback_requirements(text: str) -> list[Requirement]:
    """LLM을 못 쓸 때의 정규식 추출 — 정형 표현만 잡는다.

    LLM보다 훨씬 덜 잡지만, 잡은 것은 원문에서 직접 오려낸 것이라 근거가 확실하다.
    키가 없거나 호출이 실패해도 판정 화면이 텅 비지 않게 하는 최소 보장이다.
    """
    found: list[Requirement] = []
    seen: set[str] = set()
    for pattern, axis, operator in _FALLBACK_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(1).strip()
            if value in seen:
                continue
            seen.add(value)
            found.append(Requirement(axis=axis, operator=operator, value=value,
                                     quote=_sentence_around(text, m.start(), m.end())))
    return found


def extract_requirements(text: str,
                         eligibility_text: str = "") -> tuple[list[Requirement], str]:
    """공고문 → 요건 목록. 반환: (요건들, 담당자에게 보여줄 알림).

    text는 공고 전문(LLM에게 넘길 내용), eligibility_text는 지원대상·제외대상 문장만
    담은 것이다. 정규식 폴백은 **eligibility_text만** 훑는다 — 사업 개요까지 훑으면
    "중소 제조기업의 생산 공정에 스마트시스템을 도입하는 비용을 지원한다"(이 사업이
    무엇을 지원하는가)를 자격요건으로 오인한다. LLM은 문맥을 보고 구분할 수 있으니
    전문을 그대로 넘긴다.

    LLM이 뽑은 요건 중 인용 검증을 통과하지 못한 것은 버린다. 통과한 게 하나도
    없으면 정규식 폴백으로 내려간다.
    """
    fallback_text = eligibility_text or text

    if not hyperclova_api.is_configured():
        return (_fallback_requirements(fallback_text),
                "AI 미설정 — 지원대상 문장에서 정형 표현만 규칙으로 추출했습니다.")

    try:
        data = hyperclova_api.chat_structured(
            _EXTRACT_SYSTEM, f"[공고문]\n{text[:6000]}", _REQ_SCHEMA, max_tokens=1400)
    except Exception as e:
        print(f"[알림] 요건 추출 LLM 호출 실패 — 규칙 추출로 대체합니다. ({e})")
        return (_fallback_requirements(fallback_text),
                f"AI 호출에 실패해 규칙으로 추출했습니다. ({e})")

    kept: list[Requirement] = []
    dropped = 0
    for item in data.get("requirements", []):
        quote = str(item.get("quote", ""))
        if not _verify_quote(quote, text):
            dropped += 1        # 공고문에 없는 문장을 근거로 든 요건 → 폐기
            continue
        axis = item.get("axis") if item.get("axis") in AXES else AXIS_ETC
        kept.append(Requirement(axis=axis,
                                operator=str(item.get("operator", "기타")),
                                value=str(item.get("value", "")).strip(),
                                quote=quote.strip()))

    note = ""
    if dropped:
        note = f"공고문에서 근거를 찾지 못한 요건 {dropped}건을 제외했습니다."
    if not kept:
        kept = _fallback_requirements(fallback_text)
        note = (note + " " if note else "") + "AI가 요건을 찾지 못해 규칙 추출로 보완했습니다."
    return kept, note.strip()


# ═══════════════════════════════════════════════════════════ 2단계: 룰 판정

# 시·도 표기 흔들림 흡수. 값은 그 지역을 가리키는 모든 표기.
_REGION_ALIASES = {
    "서울": ["서울특별시", "서울시", "서울"],
    "부산": ["부산광역시", "부산시", "부산"],
    "대구": ["대구광역시", "대구시", "대구"],
    "인천": ["인천광역시", "인천시", "인천"],
    "광주": ["광주광역시", "광주시", "광주"],
    "대전": ["대전광역시", "대전시", "대전"],
    "울산": ["울산광역시", "울산시", "울산"],
    "세종": ["세종특별자치시", "세종시", "세종"],
    "경기": ["경기도", "경기"],
    "강원": ["강원특별자치도", "강원도", "강원"],
    "충북": ["충청북도", "충북"],
    "충남": ["충청남도", "충남"],
    "전북": ["전북특별자치도", "전라북도", "전북"],
    "전남": ["전라남도", "전남"],
    "경북": ["경상북도", "경북"],
    "경남": ["경상남도", "경남"],
    "제주": ["제주특별자치도", "제주도", "제주"],
}
# 긴 표기부터 봐야 "전라남도"를 "전남"으로 오인하지 않는다.
_REGION_LOOKUP = sorted(
    ((alias, code) for code, aliases in _REGION_ALIASES.items() for alias in aliases),
    key=lambda t: -len(t[0]))


def _regions_in(text: str) -> set[str]:
    """문자열 안에 등장하는 시·도 코드 집합. '전국'은 특별값 '*'로 돌려준다."""
    if not text:
        return set()
    if "전국" in text:
        return {"*"}
    found, remaining = set(), text
    for alias, code in _REGION_LOOKUP:
        if alias in remaining:
            found.add(code)
            remaining = remaining.replace(alias, " ")
    return found


# "7년 이내", "5인 이상", "3년 초과 7년 이내" 등에서 (숫자, 방향)을 뽑는다.
_BOUND_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:년|인|명|개월)?\s*(이내|이하|미만|이상|초과)")
_LOWER_KINDS = {"이상", "초과"}


def _bounds(text: str) -> list[tuple[float, str]]:
    return [(float(m.group(1)), m.group(2)) for m in _BOUND_RE.finditer(text or "")]


def _check_bounds(actual: float, bounds: list[tuple[float, str]]) -> bool | None:
    """실제 값이 모든 경계 조건을 만족하는가. 경계가 하나도 없으면 None(판정 불가)."""
    if not bounds:
        return None
    for num, kind in bounds:
        if kind in ("이내", "이하") and actual > num:
            return False
        if kind == "미만" and actual >= num:
            return False
        if kind == "이상" and actual < num:
            return False
        if kind == "초과" and actual <= num:
            return False
    return True


def _judge_years(req: Requirement, p: CompanyProfile,
                 context: str = "") -> tuple[str, str, str]:
    years = p.years
    if years is None:
        return VERDICT_CHECK, "설립일 미입력", "프로필에 설립일이 없어 업력을 계산할 수 없습니다."
    shown = f"업력 {years:.1f}년 (설립 {p.founded})"
    ok = _check_bounds(years, _bounds(req.value) or _bounds(req.quote))
    if ok is None:
        return VERDICT_CHECK, shown, "요건 문장에서 기준 연수를 읽어내지 못했습니다."
    if ok:
        return VERDICT_OK, shown, f"업력 {years:.1f}년이 '{req.value}' 조건을 충족합니다."
    return VERDICT_NO, shown, f"업력 {years:.1f}년이 '{req.value}' 조건을 벗어납니다."


# "제조업의 경우 10인 미만" 처럼 특정 업종에만 다른 기준을 주는 예외 조항.
# 소상공인 대상 공고에서 대단히 흔하고, 이걸 놓치면 신청할 수 있는 공고를 '불가'로
# 잘못 걸러 버린다 — 담당자가 기회를 놓치는, 가장 비싼 방향의 오류다.
_EXCEPTION_RE = re.compile(r"([가-힣]{2,8}업)의?\s*경우[는은]?\s*([^.;\n]*)")


def _size_bounds(req: Requirement, p: CompanyProfile,
                 context: str) -> tuple[list[tuple[float, str]], str]:
    """규모 기준을 뽑는다. 우리 업종에 해당하는 예외 조항이 있으면 그쪽을 쓴다.

    **공고 원문(context)에서는 예외 조항만 찾고, 일반적인 숫자는 절대 가져오지 않는다.**
    공고문에는 "총 사업비의 50% 이내", "최근 3년 이내 수혜기업", "1인당 연 1,200만원"
    처럼 인원과 무관한 숫자가 잔뜩 들어 있어서, 원문 전체에서 경계 조건을 긁어모으면
    엉뚱한 숫자로 판정이 뒤집힌다(실제로 그렇게 틀렸다). 기준은 요건 문장에서만 읽고,
    원문은 '제조업의 경우 …' 같은 예외 조항을 찾는 데만 쓴다.

    반환: (경계 조건들, 판정 이유에 덧붙일 부연)
    """
    for source in (f"{req.value} {req.quote}", context):
        for m in _EXCEPTION_RE.finditer(source or ""):
            word, clause = m.group(1), m.group(2)
            if p.industry and word in p.industry:
                found = _bounds(clause)
                if found:
                    return found, f" ('{word}의 경우' 예외 조항 적용)"

    # 우리 얘기가 아닌 예외 조항은 지운 뒤 본 조건만 읽는다.
    for text in (req.value, req.quote):
        found = _bounds(_EXCEPTION_RE.sub(" ", text or "")) or _bounds(text or "")
        if found:
            return found, ""
    return [], ""


def _judge_size(req: Requirement, p: CompanyProfile,
                context: str = "") -> tuple[str, str, str]:
    """규모 요건. 상시근로자 수를 기본으로 보고, 매출 기준이면 매출로 본다."""
    text = f"{req.value} {req.quote}"

    # 숫자 기준 없이 '중소기업/소상공인' 같은 기업규모 구분만 말하는 요건.
    # 긴 이름부터 봐야 "중소기업"을 "소기업"으로 오인하지 않는다.
    if not _bounds(text):
        for kind in ("소상공인", "중견기업", "중소기업", "소기업"):
            if kind not in text:
                continue
            shown = p.company_type or "기업규모 미입력"
            if not p.company_type:
                return VERDICT_CHECK, shown, "프로필에 기업규모 구분이 없습니다."
            if kind in p.company_type or p.company_type in kind:
                return VERDICT_OK, shown, f"기업규모({p.company_type})가 '{kind}'에 해당합니다."
            return VERDICT_CHECK, shown, f"'{kind}' 해당 여부는 담당자 확인이 필요합니다."

    if ("매출" in text or "억원" in text) and "근로자" not in text:
        if not p.revenue_krw:
            return VERDICT_CHECK, "매출액 미입력", "프로필에 매출액이 없어 판단할 수 없습니다."
        billions = p.revenue_krw / 100_000_000
        shown = f"매출 {billions:.1f}억원"
        ok = _check_bounds(billions, _bounds(req.value) or _bounds(req.quote))
        if ok is None:
            return VERDICT_CHECK, shown, "요건 문장에서 매출 기준을 읽어내지 못했습니다."
        return ((VERDICT_OK, shown, f"매출 {billions:.1f}억원이 '{req.value}' 조건을 충족합니다.")
                if ok else
                (VERDICT_NO, shown, f"매출 {billions:.1f}억원이 '{req.value}' 조건을 벗어납니다."))

    if not p.employees:
        return VERDICT_CHECK, "상시근로자 미입력", "프로필에 상시근로자 수가 없어 판단할 수 없습니다."
    shown = f"상시근로자 {p.employees}명"
    bounds, note = _size_bounds(req, p, context)
    ok = _check_bounds(p.employees, bounds)
    if ok is None:
        return VERDICT_CHECK, shown, "요건 문장에서 인원 기준을 읽어내지 못했습니다."
    if ok:
        return VERDICT_OK, shown, f"상시근로자 {p.employees}명이 '{req.value}' 조건을 충족합니다.{note}"
    return VERDICT_NO, shown, f"상시근로자 {p.employees}명이 '{req.value}' 조건을 벗어납니다.{note}"


def _judge_region(req: Requirement, p: CompanyProfile,
                  context: str = "") -> tuple[str, str, str]:
    if not p.region:
        return VERDICT_CHECK, "소재지 미입력", "프로필에 소재지가 없어 판단할 수 없습니다."
    ours = _regions_in(p.region)
    theirs = _regions_in(f"{req.value} {req.quote}")
    shown = p.region
    if not theirs:
        return VERDICT_CHECK, shown, "요건 문장에서 대상 지역을 특정하지 못했습니다."
    if "*" in theirs:
        return VERDICT_OK, shown, "전국이 대상이라 소재지 제한이 없습니다."
    if ours & theirs:
        return VERDICT_OK, shown, f"소재지({p.region})가 대상 지역에 포함됩니다."
    names = ", ".join(sorted(theirs))
    return VERDICT_NO, shown, f"대상 지역({names})에 소재지({p.region})가 포함되지 않습니다."


# 업종 요건에서 '제한 없음'을 뜻하는 표현.
_NO_LIMIT_MARKS = ("제한 없음", "제한없음", "무관", "전 업종", "전업종")


def _judge_industry(req: Requirement, p: CompanyProfile,
                    context: str = "") -> tuple[str, str, str]:
    text = f"{req.value} {req.quote}"
    if any(m in text for m in _NO_LIMIT_MARKS):
        return VERDICT_OK, p.industry or "-", "업종 제한이 없는 공고입니다."
    if not p.industry:
        return VERDICT_CHECK, "업종 미입력", "프로필에 업종이 없어 판단할 수 없습니다."

    shown = f"{p.industry}{f' ({p.ksic})' if p.ksic else ''}"
    # '제조업' 같은 상위 개념이 우리 업종명("전자부품 제조업")에 들어 있으면 충족.
    for word in re.findall(r"[가-힣]{2,}업", req.value) or []:
        if word in p.industry:
            return VERDICT_OK, shown, f"우리 업종({p.industry})이 '{word}'에 해당합니다."
    if p.ksic and p.ksic.upper().startswith("C") and "제조" in req.value:
        return VERDICT_OK, shown, f"표준산업분류 {p.ksic}(제조업)에 해당합니다."
    return VERDICT_CHECK, shown, f"'{req.value}'에 해당하는지 담당자 확인이 필요합니다."


# ── '기타' 축 판정 규칙표 ────────────────────────────────────────────────
# (요건 문장에서 찾을 키워드들, 프로필 속성명, 그 값이 True일 때의 판정)
#
# 자유 문장을 통째로 LLM에게 다시 판단시키지 않고 이 표로 처리하는 이유는 요건별
# 판정이 항상 같은 결과를 내야 하기 때문이다. 표에 없는 요건은 솔직하게
# '확인필요'로 남긴다 — 담당자가 직접 볼 몫이다.
_FLAG_RULES: list[tuple[tuple[str, ...], str, bool, str]] = [
    # 키워드, 프로필 속성, 기본적으로 요구되는 값, 사람이 읽을 항목 이름
    (("체납",), "tax_arrears", False, "국세·지방세 체납"),
    (("고용조정", "감원", "인위적 구조조정"), "recent_layoffs", False, "최근 고용조정"),
    (("휴업", "폐업"), "closed", False, "휴·폐업"),
    (("뿌리기술", "뿌리기업"), "root_tech", True, "뿌리기술 활용"),
    (("여성기업",), "women_owned", True, "여성기업 확인서"),
    (("스마트공장", "스마트시스템"), "smart_factory", True, "스마트공장 구축"),
]

# 요건 문장이 '없어야 한다'는 쪽을 말할 때 붙는 표현.
# 예: "스마트공장 미구축 기업" 은 스마트공장이 **없어야** 한다는 뜻이다.
_NEGATION_MARKS = ("미구축", "미도입", "미보유", "미신청", "미수혜", "받지 않은",
                   "없는 기업", "아닌 기업")


def _flag_verdict(req: Requirement, p: CompanyProfile) -> tuple[str, str, str] | None:
    """프로필의 확인 항목(체납 여부, 인증 보유…)으로 판정할 수 있으면 판정한다.

    LLM이 이런 조건을 '기타'가 아니라 '업종'으로 분류해 버리는 일이 잦다
    (예: "뿌리기술 활용 중소 제조기업"). 그러면 업종 판정이 '제조업'만 보고 통과시켜
    정작 중요한 뿌리기술 조건이 사라진다. 그래서 축과 무관하게 이 검사를 **먼저**
    돌린다 — 구체적인 확인 항목이 축 판정보다 우선한다.

    값(value)과 인용(quote)을 함께 본다. LLM이 "뿌리기술을 활용하는 중소 제조기업"을
    value에는 '중소 제조기업'만 남기고 인용에만 '뿌리기술'을 남기는 일이 있어서다.
    인용이 **한 문장**이라는 전제가 여기서 중요하다 — 인용이 두 문장에 걸치면 옆 문장의
    단어에 잘못 걸린다(그래서 _sentence_around()로 문장 단위로만 인용을 만든다).

    반환: 판정 3종 튜플, 또는 해당하는 확인 항목이 없으면 None.
    """
    text = f"{req.value} {req.quote}"
    for keywords, attr, want_default, label in _FLAG_RULES:
        if not any(k in text for k in keywords):
            continue
        value = getattr(p, attr, None)
        if value is None:
            return VERDICT_CHECK, f"{label} 미확인", f"프로필에 '{label}' 항목이 비어 있습니다."

        # 요구되는 방향을 정한다.
        #
        # 결격 사유형 항목(체납·고용조정·휴폐업)은 want_default가 이미 "없어야 한다"다.
        # 공고는 이걸 거의 항상 "체납 중인 기업은 제외한다"처럼 배제 문장으로 쓰므로,
        # 여기에 '제외' 연산자나 부정 표현을 또 반영하면 이중 부정이 되어 결과가
        # 뒤집힌다(실제로 그렇게 틀렸다). 그래서 뒤집기는 '보유해야 한다'가 기본인
        # 항목(뿌리기술·여성기업·스마트공장)에만 적용한다.
        want = want_default
        if want_default and any(m in text for m in _NEGATION_MARKS):
            want = False

        shown = f"{label}: {'예' if value else '아니오'}"
        if value == want:
            return VERDICT_OK, shown, f"'{label}' 조건({'해당' if want else '해당 없음'})을 충족합니다."
        return VERDICT_NO, shown, f"'{label}' 조건({'해당' if want else '해당 없음'})을 충족하지 않습니다."
    return None


def _judge_etc(req: Requirement, p: CompanyProfile,
               context: str = "") -> tuple[str, str, str]:
    """업종·업력·지역·규모로 환원되지 않는 요건.

    확인 항목과 매칭되면 확정 판정하고, 없으면 '확인필요'로 남긴다.
    """
    text = f"{req.value} {req.quote}"

    # 수출 실적처럼 숫자 비교가 되는 항목은 따로 처리한다.
    if "수출" in text and ("불" in text or "달러" in text or "USD" in text.upper()):
        if p.export_usd is None:
            return VERDICT_CHECK, "수출액 미입력", "프로필에 직전연도 수출액이 없습니다."
        # 공고는 보통 "100만불 미만"처럼 만 단위로 쓴다.
        m = re.search(r"(\d+(?:\.\d+)?)\s*만\s*(?:불|달러)", text)
        limit = float(m.group(1)) * 10_000 if m else None
        shown = f"직전연도 수출액 {p.export_usd:,}달러"
        if limit is None:
            return VERDICT_CHECK, shown, "요건 문장에서 수출액 기준을 읽어내지 못했습니다."
        under = "미만" in text or "이하" in text
        ok = p.export_usd < limit if under else p.export_usd >= limit
        return ((VERDICT_OK, shown, f"수출액이 '{req.value}' 조건을 충족합니다.")
                if ok else
                (VERDICT_NO, shown, f"수출액이 '{req.value}' 조건을 벗어납니다."))

    if "수혜" in text or "지원받은" in text:
        if not p.prior_support:
            return VERDICT_OK, "최근 수혜 이력 없음", "최근 동일 사업 수혜 이력이 없습니다."
        return VERDICT_CHECK, ", ".join(p.prior_support), "과거 수혜 이력과 대조가 필요합니다."

    return VERDICT_CHECK, "-", "프로필에 대응하는 항목이 없어 담당자 확인이 필요합니다."


_JUDGES = {
    AXIS_YEARS: _judge_years,
    AXIS_SIZE: _judge_size,
    AXIS_REGION: _judge_region,
    AXIS_INDUSTRY: _judge_industry,
    AXIS_ETC: _judge_etc,
}

_SIZE_WORDS = ("소상공인", "소기업", "중소기업", "중견기업", "근로자", "매출")


def _effective_axis(req: Requirement) -> str:
    """LLM이 붙인 축을 그대로 믿기 전에, 요건 문장에 그 축의 신호가 있는지 본다.

    실제 공고에서 LLM이 "일반유흥주점업 등 창업에서 제외되는 업종"을 '지역' 축으로
    분류하는 일이 있었다. 그러면 지역 판정기가 "대상 지역을 특정하지 못했습니다"라는,
    맞지도 않고 도움도 안 되는 이유를 내놓는다. 판정 결과(확인필요)는 같아도 담당자가
    읽는 이유가 엉뚱하면 도구를 못 믿게 된다.

    신호가 없으면 '기타'로 낮춰서, 최소한 정직한 이유가 나가게 한다.
    """
    text = f"{req.value} {req.quote}"
    if req.axis == AXIS_REGION and not _regions_in(text):
        return AXIS_ETC
    if req.axis == AXIS_YEARS and not _bounds(text) and "년" not in text:
        return AXIS_ETC
    if req.axis == AXIS_SIZE and not _bounds(text) \
            and not any(w in text for w in _SIZE_WORDS):
        return AXIS_ETC
    return req.axis


def _is_preference(req: Requirement) -> bool:
    """필수 요건이 아니라 '우대·가점' 사항인가.

    LLM이 operator를 '우대'로 붙여 주면 그대로 믿고, 놓쳤더라도 원문에 우대 표현이
    있으면 우대로 본다. 우대사항을 필수로 잘못 처리하면 신청할 수 있는 공고가
    화면에서 통째로 사라진다 — 이 도구가 저지를 수 있는 가장 나쁜 실수다.
    """
    if req.operator == OP_PREFER:
        return True
    text = f"{req.value} {req.quote}"
    return any(m in text for m in ("우선 지원", "우선지원", "우선 선정", "우대", "가점"))


def judge(req: Requirement, profile: CompanyProfile,
          context: str = "") -> RequirementVerdict:
    """요건 1개를 판정한다.

    순서가 중요하다:
      ① 우대사항이면 판정하지 않고 정보로만 남긴다
      ② 제한을 두지 않는 문장이면 그대로 통과시킨다
      ③ **업종·기타** 축이면 프로필의 확인 항목(체납·인증 보유…)을 먼저 본다
      ④ **지역·업력·규모** 축이면 축 판정을 먼저 본다. 판정이 안 될 때만 확인 항목을 본다

    ③과 ④를 나눈 이유가 이 함수의 핵심이다. 업종·기타는 자유 문장이라 "뿌리기술을
    활용하는 중소 제조기업"이 업종 판정에서 '제조업'만 걸려 통과해 버린다 — 확인 항목이
    더 정확하다. 반대로 지역·업력·규모는 숫자·코드 비교라 축 판정이 확정적인데, 인용
    문장에 우연히 '스마트시스템' 같은 단어가 섞여 있으면 확인 항목이 멀쩡한 판정을
    뒤집어 버린다(실제로 그렇게 틀렸다).
    """
    if _is_preference(req):
        return RequirementVerdict(req, VERDICT_OK, "-",
                                  "우대사항입니다 (필수 요건이 아니라 판정에 반영하지 않음).")

    # "업종 제한 없음"처럼 아예 제한이 아닌 문장은 그대로 통과시킨다. LLM이 이런
    # 문장에 '제외' 연산자를 붙이는 일이 있는데, 아래 제외 뒤집기를 그대로 태우면
    # 제한이 없다는 말이 '불가'로 둔갑한다.
    if any(m in f"{req.value} {req.quote}" for m in _NO_LIMIT_MARKS):
        return RequirementVerdict(req, VERDICT_OK, "-", "제한을 두지 않는 항목입니다.")

    axis = _effective_axis(req)
    free_text_axis = axis in (AXIS_INDUSTRY, AXIS_ETC)
    if free_text_axis:
        by_flag = _flag_verdict(req, profile)
        if by_flag is not None:
            return RequirementVerdict(req, *by_flag)

    verdict, company_value, reason = _JUDGES.get(axis, _judge_etc)(req, profile, context)

    # 숫자 축이 판정을 못 냈을 때만 확인 항목에 한 번 더 기회를 준다.
    if not free_text_axis and verdict == VERDICT_CHECK:
        by_flag = _flag_verdict(req, profile)
        if by_flag is not None:
            return RequirementVerdict(req, *by_flag)

    # '제외' 요건은 조건을 만족할수록 오히려 신청할 수 없다는 뜻이라 뒤집는다.
    # 예: "타 지역 소재 기업은 신청할 수 없습니다" + 우리가 그 지역이면 → 불가.
    # (기타 축은 _judge_etc가 방향을 스스로 따지므로 여기서 또 뒤집지 않는다 —
    #  축이 '기타'로 낮춰진 요건도 마찬가지라 req.axis가 아니라 axis를 본다.)
    if req.operator == OP_EXCLUDE and axis != AXIS_ETC:
        if verdict == VERDICT_OK:
            verdict, reason = VERDICT_NO, f"제외 대상에 해당합니다. ({reason})"
        elif verdict == VERDICT_NO:
            verdict, reason = VERDICT_OK, f"제외 대상이 아닙니다. ({reason})"

    return RequirementVerdict(req, verdict, company_value, reason)


def combine(rows: list[RequirementVerdict]) -> str:
    """요건별 판정 → 종합 판정.

    불가가 하나라도 있으면 불가, 없고 확인필요가 있으면 확인필요, 전부 통과면 가능.
    요건을 하나도 못 뽑았으면 '가능'이라고 단정할 근거가 없으므로 확인필요다.
    """
    if not rows:
        return VERDICT_CHECK
    verdicts = [r.verdict for r in rows]
    if VERDICT_NO in verdicts:
        return VERDICT_NO
    if VERDICT_CHECK in verdicts:
        return VERDICT_CHECK
    return VERDICT_OK


# ═════════════════════════════════════════════════════ 제출서류 · 일정 정리

# 공고문의 서류 나열은 쉼표·가운뎃점·번호 등 온갖 방식으로 구분된다.
_DOC_SPLIT_RE = re.compile(r"[,\n·•]|\d+\s*[.)]\s*|[①-⑳]")

# 서류 이름이 끝나는 말. **포함이 아니라 '끝나야' 한다.**
#
# 처음에는 '증·서·원·부' 같은 글자가 들어 있기만 하면 서류로 봤는데, 실제 공고에
# 넣어 보니 "9층(용인시청 기업지원과)"('원'), "로 접수여부 확인"('확인') 같은 주소·
# 문장 조각이 줄줄이 서류로 잡혔다. 담당자에게 "9층(용인시청 기업지원과)를 준비
# 하세요"라고 말하는 도구는 신뢰를 잃는다. 그래서 어미로 좁혔다.
_DOC_ENDINGS = (
    "증명서", "증명원", "확인서", "신청서", "계획서", "제안서", "동의서", "확약서",
    "각서", "서약서", "위임장", "등본", "초본", "명부", "현황", "내역서", "명세서",
    "실적서", "성적서", "산출서", "요약서", "소개서", "이력서", "사본", "통장",
    "사업자등록증", "재무제표", "결산서", "정관", "서류",
)

# 서류 이름일 리 없는 조각을 걸러내는 신호.
_DOC_REJECT_RE = re.compile(
    r"^\d"                       # "9층…" 처럼 숫자로 시작
    r"|[※☞▶◆■□]"                # 안내문 기호가 섞임
    r"|https?://"                # URL
    r"|\d{2,4}-\d{3,4}-\d{4}"    # 전화번호
    r"|[은는이가을를에서로의]\s"   # 조사 뒤 공백 → 문장 조각
    r"|^(및|또는|기타|해당|관련|위|아래|참고)\b"
)


def parse_required_docs(notice: Notice) -> list[str]:
    """공고문의 신청방법·제출서류 문장에서 서류 이름만 뽑는다.

    기업마당의 reqstMthPapersCn는 이름과 달리 '제출서류 목록'이 아니라 **신청방법
    안내문**인 경우가 많다("온라인 접수(인천R&D관리시스템)"). 그래서 완전한 목록을
    기대하지 않고, 확실히 서류인 것만 보수적으로 건진다. 없으면 빈 목록이 낫다 —
    엉뚱한 항목이 섞이면 점검 결과 전체를 담당자가 무시하게 된다.
    """
    docs, seen = [], set()
    for piece in _DOC_SPLIT_RE.split(notice.docs_text or ""):
        name = piece.strip(" -–—:·")
        # "재무제표(직전 2개년)"처럼 괄호가 든 이름이 흔한데, 쉼표로 자르면 닫는
        # 괄호만 남거나 짝이 안 맞는 경우가 생긴다. 짝 안 맞는 괄호만 정리한다.
        if name.endswith(")") and "(" not in name:
            name = name[:-1]
        if name.count("(") > name.count(")"):
            name += ")"
        # 괄호 안 부연은 이름 판정에서 빼고 본다 ("재무제표(직전 2개년)" → "재무제표")
        head = re.sub(r"\([^)]*\)\s*$", "", name).strip()
        if not (2 <= len(head) <= 30):
            continue
        if _DOC_REJECT_RE.search(head):
            continue
        if not head.endswith(_DOC_ENDINGS):
            continue
        if name in seen:
            continue
        seen.add(name)
        docs.append(name)
    return docs


# "○○ 확인서를 보유한 기업" 류의 요건. 공고문에 매우 흔하고, 우리 프로필에 대응
# 항목이 없으면 정직하게 '확인필요'가 되어야 하는 조건이다.
_CERT_RE = re.compile(
    r"([가-힣A-Za-z0-9]+(?:\s*[가-힣A-Za-z0-9]+){0,2}\s*"
    r"(?:확인서|인증서|지정서|확인·지정서))\s*(?:를|을)?\s*(?:보유|소지|취득|받은)")


def find_uncovered(text: str, rows: list[RequirementVerdict]) -> list[Requirement]:
    """판정표가 놓친 결정적 요건을 원문에서 보충한다.

    LLM이 "벤처기업 확인서를 보유한 중소기업"을 '규모: 중소기업' 하나로 묶거나,
    "뿌리기술을 활용하는 중소 제조기업. 업력 3년 이상."에서 업력만 뽑고 뿌리기술을
    빠뜨리는 일이 실제로 있다. 그러면 신청할 수 없는 공고가 '가능'으로 떠서 담당자가
    헛수고를 한다 — 이 도구에서 가장 비싼 실수다. 그래서 한 번 더 훑는다.

    **text로는 지원대상·제외대상 문장만 넘겨야 한다.** 사업 개요까지 넣으면
    "스마트시스템 도입 비용을 지원한다"(= 이 사업이 무엇을 지원하는가)를 자격요건
    (= 우리가 무엇을 갖춰야 하는가)으로 오인한다.
    """
    covered = " ".join(f"{r.requirement.value} {r.requirement.quote}" for r in rows)
    extra: list[Requirement] = []
    seen: set[str] = set()

    def add(value: str, start: int, end: int) -> None:
        if value in seen or value in covered:
            return
        seen.add(value)
        extra.append(Requirement(axis=AXIS_ETC, operator="포함", value=value,
                                 quote=_sentence_around(text, start, end)))

    # ① "○○ 확인서를 보유한 기업" 류
    for m in _CERT_RE.finditer(text or ""):
        add(f"{re.sub(r'\\s+', ' ', m.group(1)).strip()} 보유", m.start(), m.end())

    # ② 프로필의 확인 항목에 대응하는 말(체납·뿌리기술·여성기업…)이 자격 문장에
    #    있는데 판정표에 없는 경우
    for keywords, _attr, _want, label in _FLAG_RULES:
        for keyword in keywords:
            idx = (text or "").find(keyword)
            if idx >= 0:
                add(label, idx, idx + len(keyword))
                break
    return extra


def build_schedule(notice: Notice) -> dict:
    d = notice.d_day
    return {
        "begin": notice.apply_begin.isoformat() if notice.apply_begin else None,
        "end": notice.apply_end.isoformat() if notice.apply_end else None,
        "d_day": d,
        "label": ("마감일 미상" if d is None else
                  "오늘 마감" if d == 0 else
                  f"마감 {abs(d)}일 지남" if d < 0 else f"D-{d}"),
        "today": date.today().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════ 진입점

def evaluate(notice: Notice, profile: CompanyProfile,
             also: list[Notice] | None = None) -> EligibilityReport:
    """공고 1건을 판정한다.

    also에는 같은 사업으로 통합된 다른 기관 공고를 넘긴다. 중복 제거의 실익이
    여기서 나온다 — 기업마당 공고에는 없는 '신청 제외대상'이 K-Startup 쪽에만
    적혀 있는 경우가 흔한데, 둘을 합쳐서 읽으면 그 결격 사유까지 함께 걸러진다.
    """
    everything = [notice] + list(also or [])
    combined = "\n\n".join(n.full_text() for n in everything)

    # 보충 검사에는 '지원대상·제외대상' 문장만 쓴다. 사업 개요까지 넣으면 이 사업이
    # 무엇을 지원하는지를 우리가 무엇을 갖춰야 하는지로 오인한다.
    eligibility_text = "\n".join(
        t for n in everything for t in (n.target_text, n.exclude_text) if t)

    requirements, note = extract_requirements(combined, eligibility_text)
    rows = [judge(r, profile, combined) for r in requirements]

    # 놓친 요건을 보충한다 (빠뜨리면 신청 불가 공고가 '가능'으로 떠서 가장 비싸다).
    for req in find_uncovered(eligibility_text, rows):
        rows.append(judge(req, profile, combined))

    docs = parse_required_docs(notice)
    for n in (also or []):
        for d in parse_required_docs(n):
            if d not in docs:
                docs.append(d)

    return EligibilityReport(
        notice_id=notice.id,
        overall=combine(rows),
        rows=rows,
        required_docs=docs,
        schedule=build_schedule(notice),
        note=note,
    )

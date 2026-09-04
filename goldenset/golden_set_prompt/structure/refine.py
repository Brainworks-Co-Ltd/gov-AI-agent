"""보조 추출 — 2차(본문) 호출이 놓치기 쉬운 필드를 필드 단위로 다시 뽑는다.

2차 호출은 문서 전체를 한 번에 스키마로 정리하다 보니 아래를 자주 놓친다:
  - 지원대상: 개요의 '지원대상:' 한 줄을 골라 상세 '신청자격' 섹션을 빠뜨림
  - 신청제외대상: 여러 항목을 병합하거나 일부만 잡음
  - 판단유형: 2차 스키마에 아예 없음 (코드북 §3)

각 함수는 `NoticeStructurer`(레이트리밋·재시도 처리 재사용)를 받아 CLOVA를 부른다.
프롬프트는 `prompts.py`, 결정적 후처리는 `postprocess.py`.
"""
from __future__ import annotations

import json
import re

from app.pipeline.structure import prompts
from app.pipeline.structure.postprocess import explode_numbered_items

# 이 키워드가 원문에 하나도 없으면 '신청 자격 제외' 조항 자체가 없는 것으로 본다.
_EXCL_KEYWORDS = (
    "제외", "제한", "결격", "부적격", "배제", "신청할 수 없", "참여할 수 없", "불가한 자",
)


def has_exclusion_section(full_text: str) -> bool:
    """원문에 '신청 자격 제외' 관련 조항이 있을 만한 신호가 있는지."""
    return any(kw in full_text for kw in _EXCL_KEYWORDS)


def _strip_fence(raw: str) -> str:
    raw = raw.strip().strip("`").strip()
    return raw[4:].strip() if raw.lower().startswith("json") else raw


def _parse_items(raw: str) -> list[str] | None:
    """{"items": [...]} 파싱. 성공 시 항목 리스트, 명백히 실패(빈/비배열 items)면 None
    을 돌려줘 호출부가 재시도하게 한다. JSON 구조만 깨진 경우는 폴백으로 건진다."""
    text = _strip_fence(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 그룹마다 배열을 따로 닫는 등 구조만 깨진 경우 — 큰따옴표 문자열을 긁어낸다.
        cand = re.findall(r'"([^"]{4,})"', text)
        cand = [c.strip() for c in cand if c.strip().lower() not in ("items", "eligibility", "지원대상")]
        return cand or None
    items = parsed.get("items", []) if isinstance(parsed, dict) else parsed
    # items 를 배열 대신 문자열로 주는 경우가 잦다 — 줄바꿈·불릿으로 나뉘어 있으므로
    # 통째로 넘겨 explode_numbered_items 가 쪼개게 한다(쉼표로는 안 쪼갬 → 한 항목 안전).
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return None
    items = [str(x).strip() for x in items if str(x).strip()]
    return items or None


def extract_eligibility_detail(structurer, full_text: str) -> str:
    """'신청자격/지원요건' 상세 섹션을 원문 마커 그대로 뽑는다. [코드북 §2 지원대상]

    상세 섹션을 못 찾으면 빈 문자열을 돌려준다(호출부에서 2차 호출 값으로 폴백).
    """
    raw = structurer.chat_raw(prompts.ELIGIBILITY_DETAIL_PROMPT, full_text, max_tokens=1500)
    text = _strip_fence(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        val = parsed.get("eligibility") or parsed.get("지원대상") or ""
    elif isinstance(parsed, list):
        val = " / ".join(str(x) for x in parsed)
    else:
        val = ""
    return str(val).strip()


def extract_exclusions_strict(structurer, full_text: str, attempts: int = 3) -> list[str]:
    """신청제외대상을 항목 단위로 뽑는다(병합·누락 방지). [코드북 §2 신청제외대상_항목]

    원문에 제외 관련 키워드가 없으면 빈 리스트(제외 조항 자체가 없음).
    키워드가 있는데 빈 응답/파싱 실패면 몇 번 재시도한다.
    """
    if not has_exclusion_section(full_text):
        return []
    best: list[str] = []
    for attempt in range(1, attempts + 1):
        raw = structurer.chat_raw(prompts.EXCLUSIONS_STRICT_PROMPT, full_text, max_tokens=2500)
        items = _parse_items(raw)
        exploded = explode_numbered_items(items) if items else []
        if len(exploded) > len(best):
            best = exploded
        if len(best) >= 2:
            return best
        if attempt < attempts:
            print(f"  [재시도] 신청제외대상 추출 실패/부족 ({attempt}/{attempts})")
    return best


def classify_judgment_types(structurer, conditions: list[str]) -> list[str]:
    """지원대상·신청제외대상 조건마다 판단유형(①~⑤)을 매긴다. [코드북 §3]

    입력과 같은 길이의 리스트를 돌려준다(분류 실패 항목은 빈 문자열).
    """
    if not conditions:
        return []
    user = "다음 조건들을 순서대로 분류해라:\n" + "\n".join(
        f"{i + 1}. {c}" for i, c in enumerate(conditions)
    )
    raw = structurer.chat_raw(prompts.JUDGMENT_TYPE_PROMPT, user, max_tokens=700)
    text = _strip_fence(raw)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ["" for _ in conditions]
    if isinstance(parsed, dict):
        codes = parsed.get("codes") or parsed.get("판단유형") or []
    elif isinstance(parsed, list):
        codes = parsed
    else:
        codes = []
    codes = [str(c).strip() for c in codes]
    if len(codes) < len(conditions):
        codes += [""] * (len(conditions) - len(codes))
    return codes[: len(conditions)]

"""코드북 규칙 중 LLM 출력에 결정적으로(규칙 기반) 적용해야 하는 후처리.

프롬프트로 지시해도 HCX가 자주 어기는 규칙들을, 추출 결과에 코드로 다시 강제한다.
각 함수의 `[코드북 §…]`가 근거 조항이다.
"""
from __future__ import annotations

import re

from app.pipeline.structure.schema import NoticeExtraction

# [코드북 §2 신청기간] 날짜의 요일 표기 제거. '(월)'·'( 화 )'·'(월요일)' 모두.
_WEEKDAY_RE = re.compile(r"\s*\(\s*[월화수목금토일](?:요일)?\s*\)")

# [코드북 §2 신청제외대상·제출 서류] 한 문자열에 여러 항목이 뭉쳐 나온 경우 분리용.
_BULLET_RE = re.compile(r"^\s*(?:[-–—•▪◦○●・·‧∙*]|[①-⑳]|\d+[.)]|[가-힣][.)]|[※☞▸➤])\s*")
_PREFIXED_BULLET_RE = re.compile(r"^(\([^)]{1,20}\)\s*)[-–—•▪◦○●・·‧∙*※]\s*")


def strip_weekday(period: str | None) -> str:
    """'2026. 8. 31.(월) ~ …' -> '2026. 8. 31. ~ …'. [코드북 §2 신청기간]"""
    if not period:
        return period or ""
    return _WEEKDAY_RE.sub("", period).strip()


def _split_top_level_commas(s: str) -> list[str]:
    """괄호 밖의 쉼표에서만 나눈다. '회사 및 제품소개서(브로셔, 카달로그)'는 안 나뉜다."""
    parts, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{（":
            depth += 1
        elif ch in ")]}）":
            depth = max(0, depth - 1)
        if ch in ",、" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def explode_numbered_items(items: list[str], *, split_commas: bool = False) -> list[str]:
    """한 원소에 여러 항목이 줄바꿈/불릿/' / '로 뭉쳐 있으면 항목 단위로 분리한다.
    첫 줄이 '(중복지원 제외)' 같은 소제목이면 이후 항목에 접두어로 붙인다.
    split_commas=True 면 괄호 밖 쉼표에서도 나눈다(제출 서류 전용 — 서류명엔 내부 쉼표가
    거의 없다. 신청제외대상은 '부도, 국세, 지방세 등…'처럼 한 항목에 쉼표가 있어 False).
    [코드북 §2 신청제외대상_항목·제출 서류: 번호/불릿 단위로 행을 나눈다]
    """
    out: list[str] = []
    for item in items:
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", str(item)) if ln.strip()]
        if not lines:
            continue
        prefix = ""
        first = lines[0]
        if (re.fullmatch(r"\(.*?\)", first) or (first.startswith("(") and first.endswith(")"))) and len(lines) > 1:
            prefix = first + " "
            lines = lines[1:]
        for ln in lines:
            ln = _BULLET_RE.sub("", ln).strip()
            ln = _PREFIXED_BULLET_RE.sub(r"\1", ln).strip()
            segments = re.split(r"\s+/\s+", ln)
            if split_commas:
                segments = [seg for s in segments for seg in _split_top_level_commas(s)]
            for part in segments:
                part = part.strip()
                if part:
                    out.append((prefix + part).strip())
    seen: set[str] = set()
    dedup: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def normalize_company_contribution(value: str | None) -> str:
    """빈 값이면 '명시 없음'으로. [코드북 §2 기업 부담금: 빈 문자열 금지]

    '없음'(원문에 '자기부담금 없음'류 문구가 있었음)과 '명시 없음'(문구 자체가 없음)은
    구분해야 하므로, 여기서는 '빈 값 -> 명시 없음'만 처리한다. '없음'으로 단정하는 것은
    프롬프트(지침 7) 또는 사람 검수가 판단한다.
    """
    v = (value or "").strip()
    return v or "명시 없음"


def apply_codebook_rules(extraction: NoticeExtraction) -> NoticeExtraction:
    """NoticeExtraction 에 결정적 코드북 규칙을 적용한 새 객체를 돌려준다."""
    data = extraction.model_dump(by_alias=True)
    data["신청기간"] = strip_weekday(data.get("신청기간"))
    data["신청제외대상_항목"] = explode_numbered_items(data.get("신청제외대상_항목") or [])
    data["제출 서류"] = explode_numbered_items(data.get("제출 서류") or [], split_commas=True)
    data["기업 부담금"] = normalize_company_contribution(data.get("기업 부담금"))
    return NoticeExtraction.model_validate(data)

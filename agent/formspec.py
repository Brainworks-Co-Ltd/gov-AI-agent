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
    "너는 정부지원사업 신청서 '서식 파일'에서 뽑아낸 글을 읽고, 신청자가 무엇을 해야 "
    "하는지 정리하는 도구다. 표가 글자로만 풀려 있어 지저분하니 문맥으로 판단하라.\n"
    "세 가지로 나눠 담아라.\n"
    "1. write_sections — **문단을 써야 하는 항목**의 제목. 예: '사업 개요', "
    "'추진 배경 및 필요성', '사업 추진계획', '기대효과'. 서식에 적힌 제목을 그대로 쓰되 "
    "'1.', '□' 같은 번호·기호는 떼라. 서식에 나온 순서를 지켜라.\n"
    "2. fill_fields — **값만 채우는 칸**의 이름. 예: '회사명', '대표자', "
    "'사업자등록번호', '상시근로자수', '매출액'. 문단을 쓰는 항목은 넣지 마라.\n"
    "3. documents — 서식이 함께 내라고 요구하는 **제출서류 이름**. 예: "
    "'사업자등록증 사본', '중소기업 확인서'. 없으면 빈 배열.\n"
    "규칙: 서식에 실제로 있는 항목만 넣어라. 흔히 있을 법하다는 이유로 지어내지 마라. "
    "판단이 안 서면 빈 배열로 두어라."
)

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

    return {"write_sections": clean(data.get("write_sections"), 12),
            "fill_fields": clean(data.get("fill_fields"), 20),
            "documents": clean(data.get("documents"), 20),
            "note": ""}


def of_notice(notice: Notice) -> dict:
    """공고의 서식 첨부를 받아 열고 분석한다.

    서식이 여러 개면 **열 수 있는 것 중 글자가 가장 많은 파일**을 쓴다. 서식 묶음에는
    '개인정보 동의서'처럼 짧은 부속 서식이 섞여 있는데, 그런 걸 고르면 항목이 엉뚱해진다.
    """
    candidates = [a for a in notice.forms() if formdoc.is_supported(a.get("name", ""))]
    if not candidates:
        skipped = [a["name"] for a in notice.forms()]
        note = ("이 공고에는 신청서 서식 첨부가 없습니다." if not skipped else
                f"서식이 있지만 열 수 없는 형식입니다({', '.join(skipped[:2])}). "
                f"PDF 서식은 아직 읽지 못합니다.")
        return {"write_sections": [], "fill_fields": [], "documents": [],
                "source_file": "", "note": note}

    best_text, best_name = "", ""
    for a in candidates:
        text = formdoc.read_attachment(a)
        if len(text) > len(best_text):
            best_text, best_name = text, a["name"]

    spec = parse(best_text)
    spec["source_file"] = best_name
    if best_name and not spec["note"]:
        spec["note"] = f"서식 '{best_name}' 의 항목에 맞췄습니다."
    return spec

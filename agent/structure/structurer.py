"""5단계: 구조화 매핑
====================
공고문 텍스트를 NAVER HyperCLOVA X를 호출해서 schema.py의
NoticeExtraction 형태에 맞게 정리한다.

원본 structurer.py(응아/golden_set_prompt/structure/)와의 차이점:
  - httpx 직접 호출 → tools/hyperclova_api 래퍼 사용
    (레이트리밋·재시도 처리를 기존 래퍼와 공유)
  - app.config 의존 제거

라벨링 규칙은 `docs/golden_set_codebook.md`(골든셋 코드북)가 원천이다:
  - 프롬프트 텍스트           -> agent/structure/prompts.py  (지침마다 코드북 §표시)
  - LLM이 자주 어기는 규칙    -> agent/structure/postprocess.py (결정적 후처리)
  - 필드 단위 보조 재추출     -> agent/structure/refine.py
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from agent.structure import prompts
from agent.structure.postprocess import apply_codebook_rules
from agent.structure.schema import FieldPageLocations, NoticeExtraction
from tools import hyperclova_api

_SYSTEM_PROMPT = prompts.MAIN_SYSTEM_PROMPT
_LOCATE_SYSTEM_PROMPT = prompts.LOCATE_SYSTEM_PROMPT

# CLOVA Studio chat-completions v3의 maxTokens 상한.
_MAX_OUTPUT_TOKENS = 4096
_LOCATE_MAX_OUTPUT_TOKENS = 4096

# 이 페이지 수 이하 문서는 1차(위치 탐색) 호출 없이 바로 전체를 넣는다.
MIN_PAGES_FOR_TWO_PASS = 8
# 위치 탐색 결과와 무관하게 항상 2차 호출에 포함하는 문서 처음/끝 페이지 수.
ALWAYS_INCLUDE_HEAD = 2
ALWAYS_INCLUDE_TAIL = 2

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# CLOVA가 JSON 문자열 값 안에 유효하지 않은 이스케이프를 넣는 경우 처리.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _sanitize_json_escapes(text: str) -> str:
    return _INVALID_JSON_ESCAPE_RE.sub("", text)


class StructuringError(RuntimeError):
    pass


def render_pages_for_llm(pages: list[dict]) -> str:
    """페이지 목록을 LLM에 넣을 하나의 텍스트로 합친다.

    pages는 [{"page_number": int, "text": str, "tables": list}] 형태.
    표가 있는 페이지는 grid를 '|'로 구분한 행 단위 텍스트로 풀어서 표 구조가
    LLM에게 보이게 한다.
    """
    parts: list[str] = []
    for page in pages:
        page_num = page.get("page_number", 0)
        parts.append(f"--- {page_num}페이지 ---")
        text = page.get("text", "")
        tables = page.get("tables", [])
        if tables:
            parts.append(text)
            for i, table in enumerate(tables, start=1):
                parts.append(f"[표 {i}]")
                if isinstance(table, list):
                    for row in table:
                        if isinstance(row, list):
                            parts.append(" | ".join(str(cell) for cell in row))
                        else:
                            parts.append(str(row))
                elif hasattr(table, "to_grid"):
                    for row in table.to_grid():
                        parts.append(" | ".join(row))
        else:
            parts.append(text)
    return "\n".join(parts)


def extract_json_from_response(raw_text: str) -> str:
    """LLM 응답 텍스트에서 JSON을 추출한다.

    응답이 지시를 무시하고 ```json ... ``` 코드펜스로 감싸져 오는 경우가 흔해서
    벗겨낸다.
    """
    return _CODE_FENCE_RE.sub("", raw_text.strip()).strip()


class NoticeStructurer:
    """공고문 텍스트를 NoticeExtraction으로 구조화하는 오케스트레이터.

    tools/hyperclova_api 래퍼를 사용해 CLOVA Studio를 호출한다.
    """

    def __init__(self, model: str | None = None):
        # 구조화용 모델 — 기본은 hyperclova_api.MODEL(HCX-DASH-002)
        self._model = model

    def _chat_json(self, system_prompt: str, user_content: str,
                   max_tokens: int) -> str:
        """시스템+유저 메시지로 CLOVA를 부르고, 코드펜스를 벗긴 텍스트를 돌려준다."""
        raw = hyperclova_api.chat(
            system_prompt, user_content,
            max_tokens=max_tokens,
            model=self._model,
        )
        return extract_json_from_response(raw)

    def chat_raw(self, system_prompt: str, user_content: str,
                 max_tokens: int = 1500) -> str:
        """보조 호출용 — 코드펜스만 벗긴 응답 텍스트를 그대로 돌려준다.
        refine.py 의 필드 단위 재추출이 이 메서드로 CLOVA를 부른다."""
        return self._chat_json(system_prompt, user_content, max_tokens)

    def structure(self, full_text: str, _attempts: int = 3) -> NoticeExtraction:
        """텍스트 → NoticeExtraction. 파싱/검증 실패 시 재시도."""
        last_exc: Exception | None = None
        for attempt in range(1, _attempts + 1):
            json_text = self._chat_json(_SYSTEM_PROMPT, full_text, _MAX_OUTPUT_TOKENS)
            try:
                data = json.loads(_sanitize_json_escapes(json_text))
                return NoticeExtraction.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_exc = exc
                if attempt < _attempts:
                    print(
                        f"[재시도] CLOVA 응답 파싱 실패 ({attempt}/{_attempts}) — 다시 요청: {exc}"
                    )
        raise StructuringError(
            f"CLOVA Studio 응답을 NoticeExtraction으로 파싱하지 못했습니다: {last_exc}"
        ) from last_exc

    def locate_field_pages(self, pages: list[dict]) -> FieldPageLocations:
        """1차 호출: 각 필드가 몇 페이지에 있는지만 찾는다."""
        json_text = self._chat_json(
            _LOCATE_SYSTEM_PROMPT, render_pages_for_llm(pages), _LOCATE_MAX_OUTPUT_TOKENS
        )
        try:
            data = json.loads(_sanitize_json_escapes(json_text))
            return FieldPageLocations.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuringError(
                f"CLOVA Studio 응답을 FieldPageLocations로 파싱하지 못했습니다: {exc}"
            ) from exc

    def _select_pages(self, pages: list[dict],
                      locations: FieldPageLocations) -> list[dict]:
        """1차 결과를 바탕으로 2차에 넣을 페이지를 추린다."""
        page_by_number = {p.get("page_number", 0): p for p in pages}
        n = len(pages)
        always_include = set(range(1, ALWAYS_INCLUDE_HEAD + 1)) | set(
            range(max(1, n - ALWAYS_INCLUDE_TAIL + 1), n + 1)
        )
        selected_numbers = sorted(
            (locations.all_pages() | always_include) & page_by_number.keys()
        )
        return [page_by_number[num] for num in selected_numbers]

    def structure_document(
        self,
        full_text: str,
        pages: list[dict] | None = None,
        *,
        apply_rules: bool = True,
        refine: bool = False,
    ) -> NoticeExtraction:
        """공고문 텍스트 → NoticeExtraction.

        pages가 주어지면 페이지 단위 처리(긴 문서의 2단계 호출)를 시도한다.
        pages가 없으면 full_text를 바로 구조화한다.

        apply_rules=True : postprocess.apply_codebook_rules 적용
            (신청기간 요일 제거, 신청제외/제출서류 항목 분리, 기업 부담금 '명시 없음').
        refine=True      : 지원대상 상세 섹션·신청제외대상 항목을 CLOVA로 한 번 더 뽑아
            2차 호출 결과에 덮어쓴다(API 호출 2회 추가). 정확도↑, 비용↑.
        """
        if pages and len(pages) > MIN_PAGES_FOR_TWO_PASS:
            locations = self.locate_field_pages(pages)
            selected = self._select_pages(pages, locations)
            extraction = self.structure(render_pages_for_llm(selected))
        else:
            extraction = self.structure(full_text)

        if refine:
            extraction = self._refine(extraction, full_text)
        if apply_rules:
            extraction = apply_codebook_rules(extraction)
        return extraction

    def _refine(self, extraction: NoticeExtraction,
                full_text: str) -> NoticeExtraction:
        """지원대상·신청제외대상을 필드 단위로 다시 뽑아 결과를 보강한다."""
        from agent.structure import refine as _refine_mod

        data = extraction.model_dump(by_alias=True)
        elig = _refine_mod.extract_eligibility_detail(self, full_text)
        if elig:
            data["지원대상"] = elig
        excl = _refine_mod.extract_exclusions_strict(self, full_text)
        if excl:
            data["신청제외대상_항목"] = excl
        elif not _refine_mod.has_exclusion_section(full_text):
            # 원문에 제외 조항 신호 자체가 없음 -> 2차 호출이 넣은 사후 조항 등을 비운다.
            data["신청제외대상_항목"] = []
        return NoticeExtraction.model_validate(data)

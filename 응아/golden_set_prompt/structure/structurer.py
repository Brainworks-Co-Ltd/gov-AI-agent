"""
5단계: 구조화 매핑
====================
4단계(파싱)에서 나온 페이지별 텍스트/표를 하나의 텍스트로 합친 다음,
NAVER HyperCLOVA X(CLOVA Studio v3 chat-completions)를 호출해서 schema.py의
NoticeExtraction 형태에 맞게 정리한다.

라벨링 규칙은 `docs/golden_set_codebook.md`(골든셋 코드북)가 원천이다:
  - 프롬프트 텍스트           -> app/pipeline/structure/prompts.py  (지침마다 코드북 §표시)
  - LLM이 자주 어기는 규칙    -> app/pipeline/structure/postprocess.py (결정적 후처리)
  - 필드 단위 보조 재추출     -> app/pipeline/structure/refine.py
  - 규칙 ↔ 코드 매핑 요약     -> docs/pipeline_map.md

CLOVA Studio에는 OpenAI의 response_format=스키마 같은 강제 구조화 출력 기능이 없어서
(공식 SDK도 없음 - httpx로 직접 호출), 프롬프트로 "JSON만 출력하라"고 지시하고
응답 텍스트를 직접 파싱/검증하는 방식을 쓴다.

긴 문서는 2단계로 나눠서 처리한다:
  1차 호출: 문서 전체를 주고 "각 필드가 몇 페이지에 있는지"만 찾게 함 (FieldPageLocations)
  2차 호출: 1차에서 찾은 페이지 + 항상 포함하는 처음/끝 페이지만 추려서 실제 값을 채움
짧은 문서(MIN_PAGES_FOR_TWO_PASS 이하)는 애초에 놓칠 위험이 적어서 1차 호출 없이
바로 전체를 넣는다.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from pydantic import ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.pipeline.extract.parser import DocumentExtractionResult, PageExtractionResult
from app.pipeline.structure import prompts
from app.pipeline.structure.postprocess import apply_codebook_rules
from app.pipeline.structure.schema import FieldPageLocations, NoticeExtraction

_SYSTEM_PROMPT = prompts.MAIN_SYSTEM_PROMPT
_LOCATE_SYSTEM_PROMPT = prompts.LOCATE_SYSTEM_PROMPT

# CLOVA Studio chat-completions v3의 maxTokens 상한은 모델 공통 4096 (공식 문서 및
# 실제 호출로 확인됨 - 8192로 보냈더니 "Invalid parameter: maxTokens" 400 에러 발생).
_MAX_OUTPUT_TOKENS = 4096
_LOCATE_MAX_OUTPUT_TOKENS = 4096

# 이 페이지 수 이하 문서는 애초에 놓칠 위험이 적어서 1차(위치 탐색) 호출 없이 바로 전체를 넣는다.
MIN_PAGES_FOR_TWO_PASS = 8
# 위치 탐색 결과와 무관하게 항상 2차 호출에 포함하는 문서 처음/끝 페이지 수.
ALWAYS_INCLUDE_HEAD = 2
ALWAYS_INCLUDE_TAIL = 2

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

# CLOVA가 JSON 문자열 값 안에 "~" 같은 평범한 문자 앞에도 이유 없이 백슬래시를
# 붙여서 낼 때가 있다(2026-08-28, 공고문003 신청기간 값에서 "\~"로 재현 -
# JSON에 없는 이스케이프라 json.loads가 바로 죽는다). 유효한 JSON 이스케이프
# (\" \\ \/ \b \f \n \r \t \uXXXX)가 아닌 백슬래시는 파싱 전에 제거한다.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _sanitize_json_escapes(text: str) -> str:
    return _INVALID_JSON_ESCAPE_RE.sub("", text)


class StructuringError(RuntimeError):
    pass


# CLOVA Studio는 분당 요청수(x-ratelimit-*-requests)와 분당 토큰수(x-ratelimit-*-tokens)
# 두 종류의 레이트리밋을 같이 건다. 40페이지가 넘는 대형 문서는 1차(위치탐색)+2차(본문추출)
# 호출을 합치면 분당 토큰 한도(계정 기준 60,000)를 구조적으로 넘겨서, 기존의 지수 백오프
# (최대 8초)로는 리셋 창(60초)을 못 기다리고 계속 429가 난다. 응답 헤더가 "60s"처럼 정확한
# 리셋 시간을 알려주므로, 그 시간만큼만 정확히 기다렸다가 재시도한다.
_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 60.0


def _parse_reset_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().lower()
    try:
        if value.endswith("ms"):
            return float(value[:-2]) / 1000
        if value.endswith("s"):
            return float(value[:-1])
        return float(value)
    except ValueError:
        return None


def render_extraction_for_llm(pages: list[PageExtractionResult]) -> str:
    """페이지 목록을 LLM에 넣을 하나의 텍스트로 합친다.
    표가 있는 페이지는 grid를 '|'로 구분한 행 단위 텍스트로 풀어서 표 구조가
    LLM에게 보이게 한다 (표를 그냥 이어붙이면 행/열 정보가 사라짐)."""
    parts: list[str] = []
    for page in pages:
        parts.append(f"--- {page.page_number}페이지 ---")
        if page.tables:
            parts.append(page.text)  # OCR이 함께 인식한 표 밖 텍스트(제목 등)
            for i, table in enumerate(page.tables, start=1):
                parts.append(f"[표 {i}]")
                for row in table.to_grid():
                    parts.append(" | ".join(row))
        else:
            parts.append(page.text)
    return "\n".join(parts)


def extract_json_content(raw: dict) -> str:
    """CLOVA Studio v3 chat-completions 원본 응답 -> 어시스턴트 답변 JSON 문자열.

    응답이 지시를 무시하고 ```json ... ``` 코드펜스로 감싸져 오는 경우가 흔해서
    벗겨낸다. API 호출과 분리된 순수 함수라 저장된 응답 예시만으로도 테스트할 수 있다.
    """
    try:
        content: str = raw["result"]["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise StructuringError(f"CLOVA Studio 응답 형식이 예상과 다릅니다: {raw!r}") from exc

    finish_reason = raw.get("result", {}).get("stopReason") or raw.get("result", {}).get("finishReason")
    if finish_reason == "length":
        print("[경고] CLOVA Studio 응답이 maxTokens 한도에서 잘렸습니다.")

    return _CODE_FENCE_RE.sub("", content.strip()).strip()


class NoticeStructurer:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or settings.clova_studio_api_key
        self._model = model or settings.clova_studio_model
        self._api_url = settings.clova_studio_api_url

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
    def _call_clova(self, messages: list[dict], max_tokens: int) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = {"messages": messages, "maxTokens": max_tokens, "temperature": 0.1}
        for attempt in range(1, _RATE_LIMIT_MAX_RETRIES + 1):
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(f"{self._api_url}/{self._model}", headers=headers, json=body)
            if resp.status_code != 429:
                break
            # 요청수/토큰수 두 리셋 시간 중 더 긴 쪽만큼 기다린다 - 어느 쪽 한도에 걸렸는지
            # 헤더만으로는 항상 명확하지 않아서, 더 긴 쪽에 맞추면 항상 안전하다.
            wait_s = max(
                _parse_reset_seconds(resp.headers.get("x-ratelimit-reset-requests")) or 0.0,
                _parse_reset_seconds(resp.headers.get("x-ratelimit-reset-tokens")) or 0.0,
            ) or _RATE_LIMIT_DEFAULT_WAIT_SECONDS
            if attempt == _RATE_LIMIT_MAX_RETRIES:
                break
            print(
                f"[대기] CLOVA Studio 레이트리밋(429) - {wait_s:.0f}초 대기 후 재시도 "
                f"({attempt}/{_RATE_LIMIT_MAX_RETRIES})"
            )
            time.sleep(wait_s + 1)
        if resp.status_code >= 400:
            raise StructuringError(f"CLOVA Studio 호출 실패 ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _chat_json(self, system_prompt: str, user_content: str, max_tokens: int) -> str:
        raw = self._call_clova(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
        )
        return extract_json_content(raw)

    def chat_raw(self, system_prompt: str, user_content: str, max_tokens: int = 1500) -> str:
        """보조 호출용 — 코드펜스만 벗긴 응답 텍스트를 그대로 돌려준다(파싱은 호출부에서).
        refine.py 의 필드 단위 재추출이 이 메서드로 CLOVA를 부른다(레이트리밋 처리 공유)."""
        return self._chat_json(system_prompt, user_content, max_tokens)

    def structure(self, full_text: str, _attempts: int = 3) -> NoticeExtraction:
        # LLM 이 가끔 깨진 JSON(따옴표 누락 등)을 내놓는다 — 새로 샘플링하면 대부분 풀리므로
        # 파싱/검증 실패 시 몇 번 재시도한다.
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

    def locate_field_pages(self, pages: list[PageExtractionResult]) -> FieldPageLocations:
        """1차 호출: 각 필드가 몇 페이지에 있는지만 찾는다 (실제 값은 아직 안 채움)."""
        json_text = self._chat_json(
            _LOCATE_SYSTEM_PROMPT, render_extraction_for_llm(pages), _LOCATE_MAX_OUTPUT_TOKENS
        )
        try:
            data = json.loads(_sanitize_json_escapes(json_text))
            return FieldPageLocations.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuringError(f"CLOVA Studio 응답을 FieldPageLocations로 파싱하지 못했습니다: {exc}") from exc

    def _select_pages_for_extraction(
        self, pages: list[PageExtractionResult], locations: FieldPageLocations
    ) -> list[PageExtractionResult]:
        page_by_number = {p.page_number: p for p in pages}
        n = len(pages)
        always_include = set(range(1, ALWAYS_INCLUDE_HEAD + 1)) | set(
            range(max(1, n - ALWAYS_INCLUDE_TAIL + 1), n + 1)
        )
        selected_numbers = sorted((locations.all_pages() | always_include) & page_by_number.keys())
        return [page_by_number[num] for num in selected_numbers]

    def structure_document(
        self,
        doc: DocumentExtractionResult,
        *,
        apply_rules: bool = True,
        refine: bool = False,
    ) -> NoticeExtraction:
        """문서 하나 -> NoticeExtraction.

        apply_rules=True : postprocess.apply_codebook_rules 적용
            (신청기간 요일 제거, 신청제외/제출서류 항목 분리, 기업 부담금 '명시 없음').
        refine=True      : 지원대상 상세 섹션·신청제외대상 항목을 CLOVA로 한 번 더 뽑아
            2차 호출 결과에 덮어쓴다(API 호출 2회 추가). 정확도↑, 비용↑.
        """
        pages = doc.pages
        if len(pages) <= MIN_PAGES_FOR_TWO_PASS:
            extraction = self.structure(render_extraction_for_llm(pages))
        else:
            locations = self.locate_field_pages(pages)
            selected_pages = self._select_pages_for_extraction(pages, locations)
            extraction = self.structure(render_extraction_for_llm(selected_pages))

        if refine:
            extraction = self._refine(extraction, doc.full_text)
        if apply_rules:
            extraction = apply_codebook_rules(extraction)
        return extraction

    def _refine(self, extraction: NoticeExtraction, full_text: str) -> NoticeExtraction:
        from app.pipeline.structure import refine as _refine_mod

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

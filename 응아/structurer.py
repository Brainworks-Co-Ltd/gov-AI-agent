"""
5단계: 구조화 매핑
====================
4단계(파싱)에서 나온 페이지별 텍스트/표를 하나의 텍스트로 합친 다음,
NAVER HyperCLOVA X(CLOVA Studio v3 chat-completions)를 호출해서 schema.py의
NoticeExtraction 형태에 맞게 정리한다.

CLOVA Studio에는 OpenAI의 response_format=스키마 같은 강제 구조화 출력 기능이 없어서
(공식 SDK도 없음 - httpx로 직접 호출), 프롬프트로 "JSON만 출력하라"고 지시하고
응답 텍스트를 직접 파싱/검증하는 방식을 쓴다.

긴 문서는 2단계로 나눠서 처리한다 (docs/structuring_prompt_improvement.md의
실험 기록 참고 - 키워드 기반 필터링(방법 ②)은 오히려 품질이 떨어져서,
LLM이 직접 페이지 위치를 찾게 하는 방법(방법 ①)으로 교체함):
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
from app.pipeline.structure.schema import FieldPageLocations, NoticeExtraction

_SYSTEM_PROMPT = (
    "너는 한국 정부지원사업 공고문을 분석해서 정해진 스키마로 정리하는 어시스턴트다. "
    "아래 지침을 반드시 지켜라.\n"
    "\n"
    "1. 문서는 여러 페이지로 나뉘어 있고 각 페이지는 '--- N페이지 ---'로 표시되어 있다. "
    "평가, 유의 사항, 신청제외대상 항목 같은 필드는 문서 뒷부분(마지막 페이지들)에만 "
    "등장하는 경우가 많다. 앞부분 몇 페이지만 보고 판단하지 말고, 반드시 마지막 페이지까지 "
    "전부 확인한 뒤에 답해라.\n"
    "\n"
    "2. '평가'는 평가 항목을 하나씩 나눈 리스트다. 원소는 {\"항목명\", \"배점\", \"세부내용\"} "
    "객체이고, 항목마다 다음 규칙으로 채워라:\n"
    "  - 표에 '평가항목/세부평가/평가내용/배점' 같은 열이 있으면 표의 행 하나를 리스트의 원소 "
    "하나로 그대로 옮겨라 (항목명 열 -> 항목명, 배점 열 -> 배점, 나머지 설명 열 -> 세부내용). "
    "표의 모든 행을 빠짐없이 반영해라.\n"
    "  - 표가 없고 '서류심사 → 발표평가'처럼 절차·단계 나열형이면, 단계 하나 = 원소 하나로 "
    "만들고 항목명에 그 단계 이름을 적어라. 배점 정보가 없으면 배점은 null로 둬라.\n"
    "  - 평가 항목의 실제 세부 설명은 표가 아니라 '□평가지표'/'□평가방법' 같은 제목 아래 "
    "'◯'·'①②③' 불릿이 붙은 서술형 문단으로 적혀 있는 경우가 훨씬 흔하다 (예: '◯ OOO에 대한 "
    "이해도 및 전문성, 실현가능성 등을 종합적으로 평가'). 이런 문단을 발견하면 해당하는 항목의 "
    "세부내용에 그 문장을 그대로 옮겨 적어라. 세부내용 값에 '[표'라는 글자를 절대 넣지 마라 - "
    "'[표 N] 참조'처럼 표를 가리키기만 하고 실제 문장을 옮기지 않는 답은 명백히 틀린 답이다.\n"
    "  - 컷오프 점수('60점 미만은 제외'), 평가 방법/구성 인원, '배점표는 공고문에 미기재' 같이 "
    "특정 항목 하나에 속하지 않는 잔여 정보는 '평가 비고'에 적어라. '평가' 리스트 원소 안에 "
    "억지로 끼워넣지 마라.\n"
    "\n"
    "3. 신청제외대상_항목과 제출 서류는 문서 전체에서 언급된 항목을 빠짐없이 모아라. "
    "①②③ 같은 번호가 붙어 있으면 번호 순서대로 각각을 리스트의 별도 원소로 만들고, "
    "일부만 찾고 멈추지 말고 문서 끝까지 확인해서 전부 모아라. 제출 서류가 "
    "'필수 제출'/'해당 시 제출' 같은 그룹으로 나뉘어 있어도 구분 표시 없이 "
    "서류명만 모아서 하나의 리스트로 합쳐라.\n"
    "\n"
    "4. 공고문에 명시되지 않은 항목은 빈 문자열이나 null로 남기고 지어내지 마라. "
    "다만 '해당사항 없음'처럼 '없다'는 내용이 명시적으로 나와있으면 그 내용을 그대로 적어라.\n"
    "\n"
    "5. '[표 N]'으로 표시된 구간은 표를 '|'로 구분한 행 단위 텍스트로 옮긴 것이니 행/열 구조를 "
    "참고해서 읽어라.\n"
    "\n"
    "6. 아래 본문은 문서 전체가 아니라, 관련성이 높다고 판단된 페이지만 미리 추려서 발췌한 "
    "것일 수 있다. 그래서 페이지 번호가 1, 2, 7, 8처럼 연속되지 않고 건너뛸 수 있는데, "
    "이건 정상이니 신경 쓰지 말고 주어진 내용만으로 판단해라.\n"
    "\n"
    "7. 반드시 아래 JSON 형식으로만 답하고, 그 외 설명·인사말·코드펜스(```) 없이 순수 JSON "
    "객체 하나만 출력해라. 키 이름과 순서를 정확히 지켜라:\n"
    "{\n"
    '  "사업명": "string",\n'
    '  "주관기관": "string 또는 null",\n'
    '  "신청기간": "string",\n'
    '  "지원대상": "string",\n'
    '  "신청제외대상_항목": ["string", ...],\n'
    '  "지원금액": "string",\n'
    '  "기업 부담금": "string",\n'
    '  "제출 서류": ["string", ...],\n'
    '  "평가": [{"항목명": "string", "배점": "string 또는 null", "세부내용": "string 또는 null"}, ...],\n'
    '  "평가 비고": "string 또는 null",\n'
    '  "유의 사항": "string 또는 null"\n'
    "}"
)
# 실험: '유의 사항'을 빠짐없이 나열하라는 지침(7번)을 추가했다가 되돌렸다.
# 회수율은 늘었지만 다른 섹션(평가 기준 등) 내용이 섞여 들어와서 오히려 정확도가
# 떨어졌다 (docs/structuring_prompt_improvement.md 12절 참고).

# CLOVA Studio chat-completions v3의 maxTokens 상한은 모델 공통 4096 (공식 문서 및
# 실제 호출로 확인됨 - 8192로 보냈더니 "Invalid parameter: maxTokens" 400 에러 발생).
_MAX_OUTPUT_TOKENS = 4096

_LOCATE_SYSTEM_PROMPT = (
    "너는 공고문에서 특정 정보가 어느 페이지에 있는지 찾아주는 어시스턴트다. "
    "문서는 페이지가 '--- N페이지 ---'로 표시되어 있다. "
    "아래 각 항목이 언급되거나 관련된 내용이 있는 모든 페이지 번호를 최대한 빠짐없이 나열해라. "
    "한 항목이 여러 페이지에 걸쳐 나오면 전부 포함해라. 애매하면 관련 가능성이 있는 페이지도 "
    "포함해서 관대하게 판단해라 (페이지를 놓치는 것보다 여러 개 포함하는 게 낫다). "
    "'[표 N]'은 표를 '|'로 구분한 텍스트로 옮긴 것이다.\n"
    "\n"
    "반드시 아래 JSON 형식으로만 답하고, 그 외 설명·인사말·코드펜스(```) 없이 순수 JSON "
    "객체 하나만 출력해라. 값은 정수 페이지 번호로 이루어진 배열이다 (없으면 빈 배열):\n"
    "{\n"
    '  "사업명": [int, ...],\n'
    '  "신청기간": [int, ...],\n'
    '  "지원대상": [int, ...],\n'
    '  "신청제외대상_항목": [int, ...],\n'
    '  "지원금액": [int, ...],\n'
    '  "기업 부담금": [int, ...],\n'
    '  "제출 서류": [int, ...],\n'
    '  "평가": [int, ...],\n'
    '  "유의 사항": [int, ...]\n'
    "}"
)
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

    def structure(self, full_text: str) -> NoticeExtraction:
        json_text = self._chat_json(_SYSTEM_PROMPT, full_text, _MAX_OUTPUT_TOKENS)
        try:
            data = json.loads(_sanitize_json_escapes(json_text))
            return NoticeExtraction.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuringError(f"CLOVA Studio 응답을 NoticeExtraction으로 파싱하지 못했습니다: {exc}") from exc

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

    def structure_document(self, doc: DocumentExtractionResult) -> NoticeExtraction:
        pages = doc.pages
        if len(pages) <= MIN_PAGES_FOR_TWO_PASS:
            return self.structure(render_extraction_for_llm(pages))

        locations = self.locate_field_pages(pages)
        selected_pages = self._select_pages_for_extraction(pages, locations)
        return self.structure(render_extraction_for_llm(selected_pages))

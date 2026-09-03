"""필드 값 채점 규칙 - difflib 기반 유사도 계산.

이 프로젝트의 파이프라인(app.pipeline.*)이나 도메인 스키마(사업명/신청기간 같은
필드 이름)를 전혀 모른다. 문자열/리스트 두 값을 비교해서 점수를 매기는 순수
로직만 담아서, 다른 프로젝트의 골든셋 채점에도 클래스 그대로 재사용할 수 있게
분리했다.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

# "없음"류 표현 채점 문제: 골든셋/AI 답변이 "없음", "명시 없음", "해당사항 없음"처럼
# 의미는 같지만 글자가 다른 "정보 없음" 표현을 쓰는 경우가 많다 (실측: 기업 부담금 필드
# 5건 중 4건이 이런 케이스). 글자 단위 유사도(difflib)로 비교하면 의미가 같은데도
# 낮은 점수가 나와서, 둘 다 "정보 없음" 표현으로 판단되면 미리 일치 처리한다.
_NO_INFO_MARKERS = ("없음", "미기재", "언급없음", "언급 없음")


def normalize_text(s: object) -> str:
    if not s:
        return ""
    return " ".join(str(s).split())


@dataclass(frozen=True)
class ScoringThresholds:
    """판정 경계값. 더 엄격하거나 느슨한 채점이 필요하면 이 값만 바꿔서 주입한다."""

    match: float = 0.6
    partial: float = 0.3
    list_item_match: float = 0.5


@dataclass(frozen=True)
class FieldScore:
    field: str
    golden: str
    predicted: str
    similarity: float
    exact_match: bool
    verdict: str  # 일치 / 부분일치 / 불일치


@dataclass(frozen=True)
class ListFieldScore:
    field: str
    recall: float | None
    precision: float | None


class FieldScorer:
    """텍스트/리스트 필드 하나를 비교해 점수를 매기는 채점기.

    difflib 기반 유사도 규칙만 알고 있고, 어떤 필드가 있는지·값이 어디서
    왔는지는 모른다 - 그건 호출하는 쪽(GoldenSetValidator)의 책임이다.
    """

    def __init__(self, thresholds: ScoringThresholds | None = None):
        self._thresholds = thresholds or ScoringThresholds()

    def _is_no_info_answer(self, normalized_text: str) -> bool:
        # 판단 기준: "없음"/"미기재"/"언급 없음" 같은 표현이 있고, 구체적인 숫자
        # (금액 등)가 없으면 "정보 없음" 답변으로 본다 - 숫자가 있으면 실제 내용
        # (예: "1억원 중 자부담 없음" 같은 구체적 서술)일 가능성이 높아서 제외한다.
        if not normalized_text:
            return False
        has_marker = any(marker in normalized_text for marker in _NO_INFO_MARKERS)
        has_digit = any(ch.isdigit() for ch in normalized_text)
        return has_marker and not has_digit

    def similarity(self, a: object, b: object) -> float:
        a, b = normalize_text(a), normalize_text(b)
        if not a and not b:
            return 1.0  # 둘 다 비어있으면(정답도 없고 예측도 없음) 일치로 본다
        if not a or not b:
            return 0.0
        if self._is_no_info_answer(a) and self._is_no_info_answer(b):
            return 1.0  # 표현은 달라도 둘 다 "정보 없음"이면 일치로 본다
        return SequenceMatcher(None, a, b).ratio()

    def verdict_for(self, score: float) -> str:
        if score >= self._thresholds.match:
            return "일치"
        if score >= self._thresholds.partial:
            return "부분일치"
        return "불일치"

    def score_text_field(self, field_name: str, golden: object, predicted: object) -> FieldScore:
        golden_value = golden or ""
        predicted_value = predicted or ""
        sim = round(self.similarity(golden_value, predicted_value), 3)
        return FieldScore(
            field=field_name,
            golden=golden_value,
            predicted=predicted_value,
            similarity=sim,
            exact_match=normalize_text(golden_value) == normalize_text(predicted_value),
            verdict=self.verdict_for(sim),
        )

    def score_list_field(
        self, field_name: str, golden_items: list[str], predicted_items: list[str]
    ) -> ListFieldScore:
        if not golden_items:
            return ListFieldScore(field=field_name, recall=None, precision=None)

        threshold = self._thresholds.list_item_match
        matched_golden = sum(
            1
            for g in golden_items
            if max((self.similarity(g, p) for p in predicted_items), default=0.0) >= threshold
        )
        recall = matched_golden / len(golden_items)
        if not predicted_items:
            return ListFieldScore(field=field_name, recall=recall, precision=0.0)

        matched_pred = sum(
            1
            for p in predicted_items
            if max((self.similarity(p, g) for g in golden_items), default=0.0) >= threshold
        )
        precision = matched_pred / len(predicted_items)
        return ListFieldScore(field=field_name, recall=recall, precision=precision)

"""골든셋 케이스를 실제로 채점하는 오케스트레이터.

추출은 Extractor에, 채점 규칙은 FieldScorer에, 어떤 필드가 있는지는 FieldSpec에
위임한다. 이 클래스 자체는 "필드별 점수를 어떤 순서로 DocResult에 담을지"라는
흐름만 알고, app.pipeline이나 CLOVA API 같은 구체 구현은 전혀 모른다
(pipeline_adapter.py에만 그 지식이 있다).
"""

from __future__ import annotations

from .models import DocResult, Extractor, FieldSpec, GoldenSetCase
from .scoring import FieldScorer


class GoldenSetValidator:
    def __init__(self, extractor: Extractor, scorer: FieldScorer, field_spec: FieldSpec):
        self._extractor = extractor
        self._scorer = scorer
        self._fields = field_spec

    def run_case(self, case: GoldenSetCase) -> DocResult:
        result = DocResult(doc_id=case.doc_id)

        try:
            predicted, router_check = self._extractor.extract(
                case.source_path, case.expect_table_or_scan_pages
            )
        except Exception as e:  # 정규화/OCR/구조화 어느 단계든 실패하면 문서 전체를 실패로 기록
            result.error = f"{type(e).__name__}: {e}"
            return result

        result.router_ok = router_check.ok
        result.router_note = router_check.note

        for field_name in self._fields.text_fields:
            result.field_scores.append(
                self._scorer.score_text_field(
                    field_name, case.fields.get(field_name), predicted.get(field_name)
                )
            )

        for field_name in self._fields.list_fields:
            result.list_field_scores.append(
                self._scorer.score_list_field(
                    field_name, case.fields.get(field_name, []), predicted.get(field_name, [])
                )
            )

        for field_name in self._fields.structured_list_fields:
            golden_items = [self._fields.flatten_structured_item(i) for i in case.fields.get(field_name, [])]
            predicted_items = [
                self._fields.flatten_structured_item(i) for i in predicted.get(field_name, [])
            ]
            result.list_field_scores.append(
                self._scorer.score_list_field(field_name, golden_items, predicted_items)
            )

        return result

    def run_all(self, cases: list[GoldenSetCase]) -> list[DocResult]:
        return [self.run_case(c) for c in cases]

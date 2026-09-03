"""공고문 → NoticeExtraction 구조화.

규칙 원천은 docs/golden_set_codebook.md. 코드 매핑은 docs/pipeline_map.md.
  prompts.py     : CLOVA 시스템 프롬프트 (지침마다 코드북 §표시)
  postprocess.py : LLM이 자주 어기는 규칙의 결정적 후처리
  refine.py      : 필드 단위 보조 재추출 (지원대상 상세 / 신청제외 / 판단유형)
  structurer.py  : 오케스트레이션 (locate → structure → refine → postprocess)
"""
from agent.structure import postprocess, prompts, refine
from agent.structure.schema import (
    EvaluationItem,
    FieldPageLocations,
    NoticeExtraction,
)
from agent.structure.structurer import NoticeStructurer, StructuringError

__all__ = [
    "EvaluationItem",
    "FieldPageLocations",
    "NoticeExtraction",
    "NoticeStructurer",
    "StructuringError",
    "prompts",
    "postprocess",
    "refine",
]

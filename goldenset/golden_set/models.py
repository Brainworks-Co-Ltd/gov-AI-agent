"""골든셋 채점에 쓰는 값 객체들.

FieldSpec/GoldenSetCase/DocResult 모두 app.pipeline이나 CLOVA API 같은 구체
구현을 참조하지 않는다 - 이 모듈은 "채점이 다루는 데이터의 모양"만 정의한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .scoring import FieldScore, ListFieldScore

ANSWER_FILE_NAME = "answer.json"


@dataclass(frozen=True)
class RouterCheck:
    ok: bool
    note: str


class Extractor(Protocol):
    """GoldenSetValidator가 실제로 의존하는 유일한 인터페이스.

    이 계약(추출을 어떻게 하든 (필드값 dict, RouterCheck)를 돌려준다)만 여기
    models.py에 두고, 실제 구현(app.pipeline.* 호출)은 pipeline_adapter.py에
    분리해뒀다. validator.py가 pipeline_adapter.py를 직접 import하면 "구체
    구현은 모른다"는 말과 달리 app.pipeline까지 import 체인이 이어지므로,
    계약은 반드시 의존성이 없는 이 파일에 있어야 한다.
    """

    def extract(self, source_path: Path, expect_table_or_scan_pages: bool) -> tuple[dict, RouterCheck]: ...


@dataclass(frozen=True)
class FieldSpec:
    """이 도메인에서 어떤 필드를 어떤 방식으로 채점할지 정의.

    FieldScorer/GoldenSetValidator는 필드 이름을 전혀 모른다 - 다른 스키마를
    쓰는 프로젝트에 채점기를 옮길 때는 이 스펙만 새로 만들어서 주입하면 된다.
    """

    text_fields: tuple[str, ...]
    list_fields: tuple[str, ...]
    structured_list_fields: tuple[str, ...] = ()
    # "평가"처럼 {항목명, 배점, 세부내용} 같은 객체 리스트인 필드를 문장 하나로
    # 펼칠 때 사용할 키 순서.
    structured_item_keys: tuple[str, ...] = ("항목명", "배점", "세부내용")

    def flatten_structured_item(self, item: dict) -> str:
        return " ".join(str(item.get(k) or "") for k in self.structured_item_keys).strip()


@dataclass
class GoldenSetCase:
    """answer.json 하나를 감싸는 값 객체."""

    case_dir: Path
    doc_id: str
    source_path: Path
    expect_table_or_scan_pages: bool
    fields: dict

    @classmethod
    def load(cls, case_dir: Path) -> "GoldenSetCase":
        answer = json.loads((case_dir / ANSWER_FILE_NAME).read_text(encoding="utf-8"))
        return cls(
            case_dir=case_dir,
            doc_id=answer["doc_id"],
            source_path=case_dir / answer["source_file"],
            expect_table_or_scan_pages=answer["expect_table_or_scan_pages"],
            fields=answer["fields"],
        )

    @staticmethod
    def has_answer(case_dir: Path) -> bool:
        return (case_dir / ANSWER_FILE_NAME).exists()


@dataclass
class DocResult:
    doc_id: str
    error: str | None = None
    field_scores: list[FieldScore] = field(default_factory=list)
    list_field_scores: list[ListFieldScore] = field(default_factory=list)
    router_ok: bool | None = None
    router_note: str = ""

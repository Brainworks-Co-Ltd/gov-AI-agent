"""문서 추출 파이프라인의 각 단계(정규화 -> 라우팅 -> 파싱/OCR -> 구조화)를
나타내는 인터페이스.

NoticePipelineExtractor(pipeline_adapter.py)는 이 네 단계를 조합해서 models.py의
Extractor 계약 하나를 구현한다. 단계별로 인터페이스를 따로 뗀 이유:
  - 단계 하나만 바꾸고 싶을 때(예: Clova OCR -> 다른 OCR 서비스, HWP -> 다른
    포맷 정규화, 다른 LLM으로 구조화) NoticePipelineExtractor 전체가 아니라
    그 단계의 구현체 하나만 새로 만들어 갈아 끼우면 된다.
  - 테스트에서 단계 하나만 가짜(fake)로 바꿔치기하기 쉽다 - 예를 들어 과금되는
    Router/ContentExtractor는 고정 응답을 주는 가짜로 두고 Structurer 구현만
    실제로 검증하는 식.

Router와 ContentExtractor 사이에 오가는 route_token은 일부러 Any로 둔다. 이
프로젝트의 app.pipeline.router/extract.parser처럼 두 단계가 원래 한 몸으로
설계되는 경우가 많아서, 그 사이 값의 세부 스키마까지 여기서 다시 규정하면
실제 구현의 내부 표현을 이 인터페이스 파일이 그대로 베껴 쓰는 꼴이 된다.
대신 바깥(GoldenSetValidator)이 실제로 필요로 하는 값 - "표/스캔 대상 페이지가
몇 개였는가" - 만 RouteSummary로 명시해서 그 경계는 구체적으로 지킨다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class Normalizer(Protocol):
    """원본 문서(HWP 등)를 파싱 가능한 PDF로 바꾸는 단계. 이미 PDF면 그대로 통과."""

    def normalize(self, source_path: Path, workdir: Path) -> Path: ...


@dataclass(frozen=True)
class RouteSummary:
    """라우팅 단계 결과 중 채점기가 실제로 쓰는 값만 뽑은 요약 (라우터 정확도 판정용)."""

    table_or_scan_page_count: int
    total_page_count: int


class Router(Protocol):
    """PDF를 훑어서 표/스캔 처리가 필요한 페이지를 찾아내는 단계.

    반환하는 route_token은 이 Router와 짝을 이루는 ContentExtractor만 아는
    내부 핸드오프 값이다 - 어떤 페이지가 표/스캔인지의 세부 스키마는 그 둘
    사이의 계약이지, 이 인터페이스나 GoldenSetValidator가 알 필요는 없다.
    """

    def route(self, pdf_path: Path) -> tuple[Any, RouteSummary]: ...


class ContentExtractor(Protocol):
    """PDF + 라우팅 결과(route_token)를 받아 텍스트/표(필요시 OCR)를 뽑아내는 단계."""

    def extract(self, pdf_path: Path, route_token: Any) -> Any: ...


class Structurer(Protocol):
    """추출된 원시 내용을 채점 가능한 필드 dict(한글 alias 키)로 구조화하는 단계."""

    def structure(self, extraction: Any) -> dict: ...

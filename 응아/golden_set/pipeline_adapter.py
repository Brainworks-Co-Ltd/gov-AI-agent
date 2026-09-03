"""이 프로젝트의 app.pipeline.* 구현을 stages.py의 네 단계 인터페이스
(Normalizer/Router/ContentExtractor/Structurer)로 감싸는 어댑터들, 그리고
그 넷을 조합해 models.py의 Extractor 계약을 구현하는 NoticePipelineExtractor.

app.pipeline을 import하는 건 이 파일 하나뿐이다. 다른 프로젝트/다른 추출
파이프라인에 채점기를 옮길 때 새로 써야 하는 부분도 이 파일로 국한된다 -
그리고 이 파일 안에서도 단계별 어댑터가 각자 app.pipeline의 모듈 하나씩만
알기 때문에, 예를 들어 OCR만 다른 걸로 바꾸고 싶으면 ClovaContentExtractor
하나만 새로 만들면 되고 나머지 셋(정규화/라우팅/구조화)은 그대로 재사용된다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.pipeline.extract.clova_ocr import ClovaOcrProvider
from app.pipeline.extract.ocr_base import OCRProvider
from app.pipeline.extract.parser import DocumentExtractionResult, extract_document
from app.pipeline.normalize.hwp_converter import HwpConverter
from app.pipeline.router import DocumentRouteResult, route_document
from app.pipeline.structure.schema import NoticeExtraction
from app.pipeline.structure.structurer import NoticeStructurer

from .models import RouterCheck
from .stages import ContentExtractor, Normalizer, Router, RouteSummary, Structurer


class HwpNormalizer:
    """정규화 단계: HWP -> PDF (이미 PDF면 그대로 통과). Normalizer 구현체."""

    def __init__(self, hwp_converter: HwpConverter | None = None):
        self._hwp_converter = hwp_converter or HwpConverter()

    def normalize(self, source_path: Path, workdir: Path) -> Path:
        if source_path.suffix.lower() == ".pdf":
            return source_path

        # 이 개발 PC 한정 이슈 우회용 캐시: Python subprocess로 soffice를 띄우면
        # H2Orestart 로딩이 실패하는 원인 불명의 환경 문제가 있다(자세한 조사 내역은
        # app/pipeline/normalize/hwp_converter.py 상단 주석 참고). 셸에서 직접
        # 변환한 결과를 문서 폴더 옆에 미리 caching해두면, 정규화 단계 자체는
        # 이 환경에서 못 돌리더라도 라우터/파싱/구조화 검증은 계속할 수 있다.
        # 실제 배포 환경(보통 Linux/Docker)이나 이슈가 해결되면 이 캐시 없이도
        # 정상적으로 HwpConverter가 호출된다 - 우선순위는 항상 실제 변환이 먼저다.
        cache_path = source_path.parent / "converted_cache.pdf"
        try:
            return self._hwp_converter.convert(source_path, workdir)
        except Exception:
            if cache_path.exists():
                print(f"  [경고] {source_path.name} 정규화 실패 -> 캐시된 변환 결과 사용: {cache_path}")
                return cache_path
            raise


class PageClassifierRouter:
    """라우팅 단계: 표/스캔 페이지 탐지 (app.pipeline.router). Router 구현체.

    반환하는 route_token(DocumentRouteResult)은 ClovaContentExtractor만 이해하면
    되고, RouteSummary가 stages.Router 계약이 실제로 요구하는 값이다.
    """

    def route(self, pdf_path: Path) -> tuple[DocumentRouteResult, RouteSummary]:
        route_result = route_document(pdf_path)
        summary = RouteSummary(
            table_or_scan_page_count=len(route_result.table_or_scan_pages),
            total_page_count=route_result.page_count,
        )
        return route_result, summary


class ClovaContentExtractor:
    """파싱/OCR 단계: 표/스캔 페이지는 Clova OCR로, 나머지는 텍스트로 추출.
    ContentExtractor 구현체. route_token은 PageClassifierRouter가 만든 것을 그대로 받는다.
    """

    def __init__(self, ocr_provider: OCRProvider | None = None):
        self._ocr_provider = ocr_provider or ClovaOcrProvider()

    def extract(self, pdf_path: Path, route_token: DocumentRouteResult) -> DocumentExtractionResult:
        return extract_document(pdf_path, ocr_provider=self._ocr_provider, route_result=route_token)


class NoticeStructurerAdapter:
    """구조화 단계: 원시 추출 결과 -> 채점 가능한 필드 dict(한글 alias 키).
    Structurer 구현체.
    """

    def __init__(self, structurer: NoticeStructurer | None = None):
        self._structurer = structurer or NoticeStructurer()

    def structure(self, extraction: DocumentExtractionResult) -> dict:
        predicted: NoticeExtraction = self._structurer.structure_document(extraction)
        return predicted.model_dump(by_alias=True)


class NoticePipelineExtractor:
    """네 단계(Normalizer/Router/ContentExtractor/Structurer)를 순서대로 실행해
    models.py의 Extractor 계약을 구현하는 합성 어댑터.

    각 단계를 생성자에서 주입받으므로, 이 클래스 자체를 고치지 않고도 단계 하나만
    다른 구현(다른 OCR, 다른 구조화 로직, 테스트용 가짜 등)으로 바꿔치기할 수 있다.
    """

    def __init__(
        self,
        normalizer: Normalizer | None = None,
        router: Router | None = None,
        content_extractor: ContentExtractor | None = None,
        structurer: Structurer | None = None,
    ):
        self._normalizer = normalizer or HwpNormalizer()
        self._router = router or PageClassifierRouter()
        self._content_extractor = content_extractor or ClovaContentExtractor()
        self._structurer = structurer or NoticeStructurerAdapter()

    def extract(self, source_path: Path, expect_table_or_scan_pages: bool) -> tuple[dict, RouterCheck]:
        with tempfile.TemporaryDirectory(prefix=f"golden-{source_path.stem}-") as tmp:
            pdf_path = self._normalizer.normalize(source_path, Path(tmp))
            route_token, route_summary = self._router.route(pdf_path)

            has_table_pages = route_summary.table_or_scan_page_count > 0
            router_check = RouterCheck(
                ok=has_table_pages == expect_table_or_scan_pages,
                note=(
                    f"실제 table_or_scan 페이지 {route_summary.table_or_scan_page_count}개 "
                    f"(전체 {route_summary.total_page_count}p), 기대: has_table={expect_table_or_scan_pages}"
                ),
            )

            extraction = self._content_extractor.extract(pdf_path, route_token)
            predicted = self._structurer.structure(extraction)
            return predicted, router_check

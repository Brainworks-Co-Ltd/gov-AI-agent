"""
골든셋 기반 파싱 품질 검증 스크립트
====================================
tests/golden_set/cases/ 아래 각 문서를 실제 파이프라인
(정규화 -> 라우터 -> 파싱 -> 구조화)에 통과시켜서 answer.json과 비교한다.

실행:
    python -m scripts.validate_golden_set
    python -m scripts.validate_golden_set --case 공고문005      # 특정 문서만
    python -m scripts.validate_golden_set --csv score_report.csv  # CSV로도 저장

주의: 문서당 실제 CLOVA Studio(+표/스캔 페이지가 있으면 Clova OCR) API 호출이 발생해서
과금됩니다. 커밋마다 자동 실행하지 말고 필요할 때 수동으로 돌리세요
(pytest 쪽은 tests/test_golden_set_quality.py에서 @pytest.mark.golden으로 분리).

채점 기준 (기존 scoring.py 프로토타입과 동일한 관례):
  - 유사도(difflib) >= 0.6  -> 일치
  - 0.3 ~ 0.6                -> 부분일치 (사람이 재확인 필요)
  - 0.3 미만                 -> 불일치
  - exact_match은 공백/개행만 정규화한 뒤 완전히 같은지 (더 엄격한 기준)
신청제외대상_항목/제출 서류는 리스트라서 재현율(recall)/정밀도(precision)로 따로 계산.
"""

from __future__ import annotations

import argparse
import csv
import json
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from app.pipeline.extract.clova_ocr import ClovaOcrProvider
from app.pipeline.extract.ocr_base import OCRProvider
from app.pipeline.extract.parser import extract_document
from app.pipeline.normalize.hwp_converter import HwpConverter
from app.pipeline.router import route_document
from app.pipeline.structure.structurer import NoticeStructurer

GOLDEN_SET_DIR = Path(__file__).resolve().parent.parent / "tests" / "golden_set" / "cases"

FUZZY_MATCH_THRESHOLD = 0.6
PARTIAL_MATCH_THRESHOLD = 0.3

TEXT_FIELDS = [
    "사업명",
    "신청기간",
    "지원대상",
    "지원금액",
    "기업 부담금",
    "평가 비고",
    "유의 사항",
]

# 항목별로 여러 행으로 라벨링된 리스트 필드 -> 재현율/정밀도로 채점.
LIST_FIELDS = ["신청제외대상_항목", "제출 서류"]

# "평가"는 [{"항목명","배점","세부내용"}, ...] 구조라 문자열 리스트가 아니다.
# 각 원소를 "항목명 배점 세부내용" 한 문자열로 펼친 뒤 LIST_FIELDS와 같은
# 재현율/정밀도 방식(score_list_field)으로 채점한다.
STRUCTURED_LIST_FIELDS = ["평가"]


def _flatten_evaluation_item(item: dict) -> str:
    return " ".join(str(item.get(k) or "") for k in ("항목명", "배점", "세부내용")).strip()


@dataclass
class FieldScore:
    field: str
    golden: str
    predicted: str
    similarity: float
    exact_match: bool
    verdict: str  # 일치 / 부분일치 / 불일치


@dataclass
class ListFieldScore:
    field: str
    recall: float | None
    precision: float | None


@dataclass
class DocResult:
    doc_id: str
    error: str | None = None
    field_scores: list[FieldScore] = field(default_factory=list)
    list_field_scores: list[ListFieldScore] = field(default_factory=list)
    router_ok: bool | None = None
    router_note: str = ""


def normalize_text(s) -> str:
    if not s:
        return ""
    return " ".join(str(s).split())


# "없음"류 표현 채점 문제: 골든셋/AI 답변이 "없음", "명시 없음", "해당사항 없음"처럼
# 의미는 같지만 글자가 다른 "정보 없음" 표현을 쓰는 경우가 많다 (실측: 기업 부담금 필드
# 5건 중 4건이 이런 케이스). 글자 단위 유사도(difflib)로 비교하면 의미가 같은데도
# 낮은 점수가 나와서, 둘 다 "정보 없음" 표현으로 판단되면 미리 일치 처리한다.
# 판단 기준: "없음"/"미기재"/"언급 없음" 같은 표현이 있고, 구체적인 숫자(금액 등)가
# 없으면 "정보 없음" 답변으로 본다 - 숫자가 있으면 실제 내용(예: "1억원 중 자부담 없음"
# 같은 구체적 서술)일 가능성이 높아서 제외한다.
_NO_INFO_MARKERS = ("없음", "미기재", "언급없음", "언급 없음")


def _is_no_info_answer(normalized_text: str) -> bool:
    if not normalized_text:
        return False
    has_marker = any(marker in normalized_text for marker in _NO_INFO_MARKERS)
    has_digit = any(ch.isdigit() for ch in normalized_text)
    return has_marker and not has_digit


def similarity(a, b) -> float:
    a, b = normalize_text(a), normalize_text(b)
    if not a and not b:
        return 1.0  # 둘 다 비어있으면(정답도 없고 예측도 없음) 일치로 본다
    if not a or not b:
        return 0.0
    if _is_no_info_answer(a) and _is_no_info_answer(b):
        return 1.0  # 표현은 달라도 둘 다 "정보 없음"이면 일치로 본다
    return SequenceMatcher(None, a, b).ratio()


def verdict_for(score: float) -> str:
    if score >= FUZZY_MATCH_THRESHOLD:
        return "일치"
    if score >= PARTIAL_MATCH_THRESHOLD:
        return "부분일치"
    return "불일치"


def score_list_field(
    golden_items: list[str], predicted_items: list[str], threshold: float = 0.5
) -> tuple[float | None, float | None]:
    if not golden_items:
        return None, None
    matched_golden = sum(
        1 for g in golden_items if max((similarity(g, p) for p in predicted_items), default=0.0) >= threshold
    )
    recall = matched_golden / len(golden_items)
    if not predicted_items:
        return recall, 0.0
    matched_pred = sum(
        1 for p in predicted_items if max((similarity(p, g) for g in golden_items), default=0.0) >= threshold
    )
    precision = matched_pred / len(predicted_items)
    return recall, precision


def normalize_source_to_pdf(source_path: Path, workdir: Path) -> Path:
    if source_path.suffix.lower() == ".pdf":
        return source_path

    # 이 개발 PC 한정 이슈 우회용 캐시: Python subprocess로 soffice를 띄우면
    # H2Orestart 로딩이 실패하는 원인 불명의 환경 문제가 있다(자세한 조사 내역은
    # app/pipeline/normalize/hwp_converter.py 상단 주석 참고). 셸에서 직접
    # 변환한 결과를 문서 폴더 옆에 미리 caching해두면, 정규화 단계 자체는
    # 이 환경에서 못 돌리더라도 3~5단계(라우터/파싱/구조화) 검증은 계속할 수 있다.
    # 실제 배포 환경(보통 Linux/Docker)이나 이슈가 해결되면 이 캐시 없이도
    # 정상적으로 HwpConverter가 호출된다 - 우선순위는 항상 실제 변환이 먼저다.
    cache_path = source_path.parent / "converted_cache.pdf"
    try:
        return HwpConverter().convert(source_path, workdir)
    except Exception:
        if cache_path.exists():
            print(f"  [경고] {source_path.name} 정규화 실패 -> 캐시된 변환 결과 사용: {cache_path}")
            return cache_path
        raise


def run_case(case_dir: Path, ocr_provider: OCRProvider, structurer: NoticeStructurer) -> DocResult:
    answer = json.loads((case_dir / "answer.json").read_text(encoding="utf-8"))
    doc_id = answer["doc_id"]
    result = DocResult(doc_id=doc_id)

    source_path = case_dir / answer["source_file"]
    try:
        with tempfile.TemporaryDirectory(prefix=f"golden-{doc_id}-") as tmp:
            pdf_path = normalize_source_to_pdf(source_path, Path(tmp))
            route_result = route_document(pdf_path)

            has_table_pages = len(route_result.table_or_scan_pages) > 0
            expected = answer["expect_table_or_scan_pages"]
            result.router_ok = has_table_pages == expected
            result.router_note = (
                f"실제 table_or_scan 페이지 {len(route_result.table_or_scan_pages)}개 "
                f"(전체 {route_result.page_count}p), 기대: has_table={expected}"
            )

            extraction = extract_document(pdf_path, ocr_provider=ocr_provider, route_result=route_result)
            predicted = structurer.structure_document(extraction)
    except Exception as e:  # 정규화/OCR/구조화 어느 단계든 실패하면 문서 전체를 실패로 기록
        result.error = f"{type(e).__name__}: {e}"
        return result

    predicted_dict = predicted.model_dump(by_alias=True)
    golden_fields = answer["fields"]

    for field_name in TEXT_FIELDS:
        golden_value = golden_fields.get(field_name) or ""
        predicted_value = predicted_dict.get(field_name) or ""
        sim = round(similarity(golden_value, predicted_value), 3)
        result.field_scores.append(
            FieldScore(
                field=field_name,
                golden=golden_value,
                predicted=predicted_value,
                similarity=sim,
                exact_match=normalize_text(golden_value) == normalize_text(predicted_value),
                verdict=verdict_for(sim),
            )
        )

    for field_name in LIST_FIELDS:
        recall, precision = score_list_field(
            golden_fields.get(field_name, []), predicted_dict.get(field_name, [])
        )
        result.list_field_scores.append(ListFieldScore(field=field_name, recall=recall, precision=precision))

    for field_name in STRUCTURED_LIST_FIELDS:
        golden_items = [_flatten_evaluation_item(i) for i in golden_fields.get(field_name, [])]
        predicted_items = [_flatten_evaluation_item(i) for i in predicted_dict.get(field_name, [])]
        recall, precision = score_list_field(golden_items, predicted_items)
        result.list_field_scores.append(ListFieldScore(field=field_name, recall=recall, precision=precision))

    return result


def print_console_report(results: list[DocResult]) -> None:
    print("\n=== 문서별 결과 ===")
    for r in results:
        if r.error:
            print(f"[{r.doc_id}] 파이프라인 실패: {r.error}")
            continue
        router_mark = "OK" if r.router_ok else "MISMATCH"
        print(f"\n[{r.doc_id}] 라우터: {router_mark} ({r.router_note})")
        for fs in r.field_scores:
            mark = "O" if fs.exact_match else " "
            print(f"  [{mark}] {fs.field:8s} 유사도={fs.similarity:.2f} 판정={fs.verdict}")
        for lfs in r.list_field_scores:
            if lfs.recall is not None:
                print(f"  {lfs.field}: 재현율={lfs.recall:.0%} 정밀도={lfs.precision:.0%}")

    print("\n=== 필드별 평균 (실패 문서 제외) ===")
    ok_results = [r for r in results if not r.error]
    field_sims: dict[str, list[float]] = {}
    for r in ok_results:
        for fs in r.field_scores:
            field_sims.setdefault(fs.field, []).append(fs.similarity)
    for field_name in TEXT_FIELDS:
        sims = field_sims.get(field_name, [])
        if sims:
            avg = sum(sims) / len(sims)
            exact_count = sum(
                1 for r in ok_results for fs in r.field_scores if fs.field == field_name and fs.exact_match
            )
            print(f"  {field_name:8s} 평균 유사도={avg:.2f}  정확일치={exact_count}/{len(sims)}")

    router_ok_count = sum(1 for r in results if r.router_ok)
    router_total = sum(1 for r in results if r.router_ok is not None)
    error_count = sum(1 for r in results if r.error)
    print(f"\n라우터 정확도: {router_ok_count}/{router_total}")
    print(f"파이프라인 실패 문서: {error_count}/{len(results)}")


def write_csv_report(results: list[DocResult], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["문서ID", "필드명", "골든셋_정답", "파이프라인_결과", "유사도", "정확일치", "판정"])
        for r in results:
            if r.error:
                w.writerow([r.doc_id, "_전체", "", "", 0.0, False, f"파이프라인 실패: {r.error}"])
                continue
            for fs in r.field_scores:
                w.writerow([r.doc_id, fs.field, fs.golden, fs.predicted, fs.similarity, fs.exact_match, fs.verdict])
            for lfs in r.list_field_scores:
                if lfs.recall is None:
                    continue
                w.writerow(
                    [
                        r.doc_id,
                        lfs.field,
                        "",
                        "",
                        round(lfs.recall, 3),
                        "",
                        f"재현율 {lfs.recall:.0%} / 정밀도 {lfs.precision:.0%}",
                    ]
                )
            w.writerow([r.doc_id, "_라우터", "", "", "", r.router_ok, r.router_note])
    print(f"\nCSV 리포트 저장: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="골든셋 기반 파싱 품질 검증")
    parser.add_argument("--case", help="특정 문서ID만 실행 (예: 공고문005)")
    parser.add_argument("--csv", help="CSV 리포트 저장 경로")
    args = parser.parse_args()

    # answer.json이 없는 케이스 디렉터리(예: 아직 라벨링/빌드가 안 끝난 신규 문서의
    # 빈 폴더)는 건너뛴다 - build_golden_set_from_xlsx.py가 SOURCE_EXTENSIONS에
    # 등록 안 된 문서는 answer.json을 안 만들고 [SKIP]하기 때문에 실제로 생긴다.
    case_dirs = sorted(
        p for p in GOLDEN_SET_DIR.iterdir() if p.is_dir() and (p / "answer.json").exists()
    )
    skipped = sorted(
        p.name for p in GOLDEN_SET_DIR.iterdir() if p.is_dir() and not (p / "answer.json").exists()
    )
    if skipped:
        print(f"[SKIP] answer.json 없음: {', '.join(skipped)}")
    if args.case:
        case_dirs = [d for d in case_dirs if d.name == args.case]
    if not case_dirs:
        print("실행할 골든셋 케이스가 없습니다.")
        return

    ocr_provider = ClovaOcrProvider()
    structurer = NoticeStructurer()

    results = [run_case(d, ocr_provider, structurer) for d in case_dirs]
    print_console_report(results)
    if args.csv:
        write_csv_report(results, Path(args.csv))


if __name__ == "__main__":
    main()

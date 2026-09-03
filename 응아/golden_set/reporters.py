"""채점 결과(DocResult)를 사람이 볼 수 있는 형태로 내보내는 리포터들.

GoldenSetValidator는 이 파일을 몰라도 된다 - 콘솔/CSV 외에 다른 출력 형식이
필요해지면 Reporter 인터페이스만 지켜서 새 클래스를 추가하면 된다.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

from .models import DocResult, FieldSpec


class Reporter(Protocol):
    def report(self, results: list[DocResult]) -> None: ...


class ConsoleReporter:
    def __init__(self, field_spec: FieldSpec):
        self._fields = field_spec

    def report(self, results: list[DocResult]) -> None:
        self._print_per_doc(results)
        self._print_field_averages(results)
        self._print_summary(results)

    def _print_per_doc(self, results: list[DocResult]) -> None:
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

    def _print_field_averages(self, results: list[DocResult]) -> None:
        print("\n=== 필드별 평균 (실패 문서 제외) ===")
        ok_results = [r for r in results if not r.error]
        field_sims: dict[str, list[float]] = {}
        for r in ok_results:
            for fs in r.field_scores:
                field_sims.setdefault(fs.field, []).append(fs.similarity)
        for field_name in self._fields.text_fields:
            sims = field_sims.get(field_name, [])
            if not sims:
                continue
            avg = sum(sims) / len(sims)
            exact_count = sum(
                1 for r in ok_results for fs in r.field_scores if fs.field == field_name and fs.exact_match
            )
            print(f"  {field_name:8s} 평균 유사도={avg:.2f}  정확일치={exact_count}/{len(sims)}")

    def _print_summary(self, results: list[DocResult]) -> None:
        router_ok_count = sum(1 for r in results if r.router_ok)
        router_total = sum(1 for r in results if r.router_ok is not None)
        error_count = sum(1 for r in results if r.error)
        print(f"\n라우터 정확도: {router_ok_count}/{router_total}")
        print(f"파이프라인 실패 문서: {error_count}/{len(results)}")


class CsvReporter:
    def __init__(self, path: Path):
        self._path = path

    def report(self, results: list[DocResult]) -> None:
        with open(self._path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["문서ID", "필드명", "골든셋_정답", "파이프라인_결과", "유사도", "정확일치", "판정"])
            for r in results:
                self._write_doc(w, r)
        print(f"\nCSV 리포트 저장: {self._path}")

    def _write_doc(self, w, r: DocResult) -> None:
        if r.error:
            w.writerow([r.doc_id, "_전체", "", "", 0.0, False, f"파이프라인 실패: {r.error}"])
            return
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

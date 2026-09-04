"""공고 수집과 판정 결과 사전 갱신.

웹 버튼은 :class:`RefreshCoordinator`로 이 파이프라인을 백그라운드에서 실행하고,
운영체제 작업 스케줄러는 같은 코드를 CLI로 실행한다.

    python -m tools.refresh
    python -m tools.refresh --collect
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agent import orchestrator
from agent.schemas import CompanyProfile, Notice
from tools import ingest, profile_store, store


Progress = Callable[[dict[str, Any]], None]
Evaluator = Callable[[Any, Notice, CompanyProfile], Any]


def _ignore_progress(update: dict[str, Any]) -> None:
    """CLI가 진행 콜백을 지정하지 않았을 때 사용하는 빈 콜백."""


def run(*, collect_first: bool = False, use_llm: bool = True,
        offline: bool = False, profile: CompanyProfile | None = None,
        evaluate: Evaluator | None = None,
        progress: Progress | None = None,
        should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """공식 API를 선택적으로 수집하고, 유효하지 않은 판정만 다시 계산한다."""
    notify = progress or _ignore_progress
    ingest_result = None
    if collect_first:
        notify({"phase": "collecting", "done": 0, "total": 0})
        ingest_result = ingest.run(use_llm=use_llm, offline=offline)

    conn = store.connect()
    selected_profile = profile or profile_store.load()
    evaluator = evaluate or orchestrator.eligibility_of
    errors: list[str] = []
    try:
        notices = store.open_businesses(conn)
        stale: list[Notice] = []
        cached = 0
        for notice in notices:
            if orchestrator.has_valid_verdict(conn, notice, selected_profile):
                cached += 1
            else:
                stale.append(notice)

        total = len(notices)
        refreshed = 0
        stopped = False
        notify({"phase": "judging", "total": total, "done": cached,
                "cached": cached, "refreshed": 0, "failed": 0})
        for notice in stale:
            # 한 건이 끝날 때마다 중단 요청을 본다. 판정 하나는 3초대라 누른 뒤
            # 그 안에 멈춘다. 이미 끝난 판정은 저장돼 있어 다시 눌러도 이어서 돈다.
            if should_stop is not None and should_stop():
                stopped = True
                break
            try:
                evaluator(conn, notice, selected_profile)
                refreshed += 1
            except Exception as exc:
                errors.append(f"{notice.id}: {exc}")
            notify({"phase": "judging", "total": total,
                    "done": cached + refreshed + len(errors), "cached": cached,
                    "refreshed": refreshed, "failed": len(errors)})
    finally:
        conn.close()

    return {
        "total": total,
        "cached": cached,
        "refreshed": refreshed,
        "failed": len(errors),
        "errors": errors,
        "ingest": ingest_result,
        "stopped": stopped,
    }


class RefreshCoordinator:
    """한 서버 프로세스에서 갱신 작업을 하나만 실행하고 상태를 제공한다."""

    def __init__(self, runner: Callable[..., dict[str, Any]] = run):
        self._runner = runner
        self._lock = threading.Lock()
        # 진행 중인 작업에 "그만"을 전하는 신호. 작업마다 새로 만든다.
        self._stop = threading.Event()
        self._status: dict[str, Any] = {
            "state": "idle", "phase": "", "total": 0, "done": 0,
            "cached": 0, "refreshed": 0, "failed": 0, "errors": [],
            "started_at": None, "finished_at": None,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._status)

    def start(self, **options: Any) -> dict[str, Any]:
        """작업을 시작하고 기다리지 않은 채 현재 상태를 돌려준다."""
        with self._lock:
            if self._status["state"] == "running":
                return {"started": False, **copy.deepcopy(self._status)}
            self._stop = threading.Event()
            self._status = {
                "state": "running", "phase": "starting", "total": 0, "done": 0,
                "cached": 0, "refreshed": 0, "failed": 0, "errors": [],
                "stopped": False, "started_at": _now(), "finished_at": None,
            }
            response = {"started": True, **copy.deepcopy(self._status)}

        thread = threading.Thread(target=self._run, args=(options,), daemon=True,
                                  name="verdict-refresh")
        thread.start()
        return response

    def stop(self) -> dict[str, Any]:
        """진행 중인 작업에 중단을 요청한다. 판정 한 건이 끝나는 대로 멈춘다."""
        with self._lock:
            running = self._status["state"] == "running"
            if running:
                self._stop.set()
            return {"stopping": running, **copy.deepcopy(self._status)}

    def _progress(self, update: dict[str, Any]) -> None:
        with self._lock:
            if self._status["state"] == "running":
                self._status.update(update)

    def _run(self, options: dict[str, Any]) -> None:
        try:
            result = self._runner(progress=self._progress,
                                  should_stop=self._stop.is_set, **options)
        except Exception as exc:
            with self._lock:
                self._status.update({
                    "state": "failed", "phase": "", "failed": 1,
                    "errors": [str(exc)], "finished_at": _now(),
                })
            return

        with self._lock:
            self._status.update(result)
            # 중간에 멈춘 것을 '완료'로 알리면 남은 건수가 사라진 것처럼 보인다.
            self._status.update({"state": "succeeded", "phase": "",
                                 "done": (result.get("cached", 0) + result.get("refreshed", 0)
                                          if result.get("stopped") else result.get("total", 0)),
                                 "finished_at": _now()})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="공고와 판정 결과를 미리 갱신합니다.")
    parser.add_argument("--collect", action="store_true",
                        help="판정 전에 공식 API 공고 수집과 중복 통합도 실행")
    parser.add_argument("--offline", action="store_true",
                        help="공식 API 대신 저장된 공고 캐시 사용")
    parser.add_argument("--no-llm", action="store_true",
                        help="중복 통합의 LLM 경계 판정 비활성화")
    args = parser.parse_args(argv)
    result = run(collect_first=args.collect, offline=args.offline,
                 use_llm=not args.no_llm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

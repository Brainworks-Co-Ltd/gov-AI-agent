"""판정 캐시 유효성·백그라운드 갱신 회귀 테스트.

실제 ``data/app.db``와 외부 API는 사용하지 않는다. 임시 SQLite 파일과 결정적인
가짜 판정기만 사용하므로 다음 명령으로 반복 실행할 수 있다.

    python -X utf8 tests_cache_refresh.py
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent import orchestrator
from agent.schemas import CompanyProfile, Notice
from tools import store


def _notice(text: str = "광주 소재 중소기업") -> Notice:
    return Notice(source="테스트", source_id="1", title="지원사업", target_text=text)


def _profile(region: str = "광주광역시") -> CompanyProfile:
    profile = CompanyProfile()
    profile.region = region
    return profile


def test_notice_change_invalidates_cache_key() -> None:
    before = orchestrator.verdict_input_hash(_notice("광주 기업"), _profile(), [], "v1")
    after = orchestrator.verdict_input_hash(_notice("전남 기업"), _profile(), [], "v1")
    assert before != after


def test_profile_change_invalidates_cache_key() -> None:
    notice = _notice()
    before = orchestrator.verdict_input_hash(notice, _profile("광주광역시"), [], "v1")
    after = orchestrator.verdict_input_hash(notice, _profile("전라남도"), [], "v1")
    assert before != after


def test_sibling_change_invalidates_cache_key() -> None:
    notice = _notice()
    without_sibling = orchestrator.verdict_input_hash(notice, _profile(), [], "v1")
    sibling = Notice(source="다른기관", source_id="2", title="같은 사업",
                     exclude_text="휴업 기업 제외")
    with_sibling = orchestrator.verdict_input_hash(notice, _profile(), [sibling], "v1")
    assert without_sibling != with_sibling


def test_version_change_invalidates_cache_key() -> None:
    notice = _notice()
    before = orchestrator.verdict_input_hash(notice, _profile(), [], "v1")
    after = orchestrator.verdict_input_hash(notice, _profile(), [], "v2")
    assert before != after


def test_connect_migrates_verdict_hash_and_enables_wal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "app.db"
        # 예전 DB처럼 input_hash 없는 verdicts 표를 먼저 만든다.
        legacy = sqlite3.connect(db_path)
        legacy.execute(
            "CREATE TABLE verdicts (notice_id TEXT PRIMARY KEY, overall TEXT NOT NULL, "
            "report_json TEXT NOT NULL, profile_hash TEXT NOT NULL, created_at TEXT)"
        )
        legacy.close()

        with patch.object(store, "DB_PATH", str(db_path)):
            conn = store.connect()
            try:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(verdicts)")}
                assert "input_hash" in columns
                assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
                assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000
            finally:
                conn.close()


def test_refresh_skips_valid_cache() -> None:
    from tools import refresh

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "app.db"
        profile = _profile()
        first = _notice("광주 기업")
        second = Notice(source="테스트", source_id="2", title="두 번째 사업",
                        target_text="전남 기업")
        with patch.object(store, "DB_PATH", str(db_path)):
            conn = store.connect()
            try:
                store.upsert_notices(conn, [first, second])
                input_hash = orchestrator.verdict_input_hash(first, profile, [])
                store.save_verdict(
                    conn, first.id, "가능",
                    {"notice_id": first.id, "overall": "가능", "rows": []},
                    orchestrator.profile_store.hash_of(profile), input_hash)
            finally:
                conn.close()

            evaluated: list[str] = []

            def evaluate(conn, notice, selected_profile):
                evaluated.append(notice.id)

            result = refresh.run(profile=profile, evaluate=evaluate)

        assert evaluated == [second.id]
        assert result["total"] == 2
        assert result["cached"] == 1
        assert result["refreshed"] == 1
        assert result["failed"] == 0


def test_coordinator_rejects_second_running_job() -> None:
    from tools.refresh import RefreshCoordinator

    release = threading.Event()

    def runner(*, progress, **options):
        progress({"phase": "judging", "total": 1, "done": 0})
        release.wait(timeout=2)
        return {"total": 1, "cached": 0, "refreshed": 1, "failed": 0,
                "errors": []}

    coordinator = RefreshCoordinator(runner=runner)
    assert coordinator.start()["started"] is True
    assert coordinator.start()["started"] is False
    release.set()

    deadline = time.monotonic() + 2
    while coordinator.status()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    status = coordinator.status()
    assert status["state"] == "succeeded"
    assert status["refreshed"] == 1


def test_list_verdicts_excludes_stale_input_hash() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "app.db"
        profile = _profile()
        notice = _notice()
        profile_hash = orchestrator.profile_store.hash_of(profile)
        with patch.object(store, "DB_PATH", str(db_path)):
            conn = store.connect()
            try:
                store.upsert_notices(conn, [notice])
                store.save_verdict(
                    conn, notice.id, "가능",
                    {"notice_id": notice.id, "overall": "가능", "rows": []},
                    profile_hash, "예전-입력-해시")
                verdicts = store.all_verdicts(
                    conn, profile_hash, {notice.id: "현재-입력-해시"})
                assert verdicts == {}
            finally:
                conn.close()


def test_web_uses_background_refresh_status() -> None:
    root = Path(__file__).parent
    app = (root / "web" / "app.js").read_text(encoding="utf-8")
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    server = (root / "serve.py").read_text(encoding="utf-8")

    assert "AUTO_JUDGE" not in app
    assert "while (judging" not in app
    assert 'api("/api/refresh")' in app
    assert 'id="btn-judge-stop"' not in index
    assert 'path == "/api/refresh"' in server


def test_http_refresh_request_returns_before_job_finishes() -> None:
    import serve
    from tools import refresh

    release = threading.Event()

    def runner(*, progress, **options):
        progress({"phase": "judging", "total": 1, "done": 0})
        release.wait(timeout=2)
        return {"total": 1, "cached": 0, "refreshed": 1, "failed": 0,
                "errors": [], "ingest": None}

    coordinator = refresh.RefreshCoordinator(runner=runner)
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            base + "/api/judge", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"})
        started_at = time.monotonic()
        with patch.object(serve, "_REFRESH", coordinator):
            with urllib.request.urlopen(request, timeout=1) as response:
                payload = json.load(response)
                assert response.status == 202
            elapsed = time.monotonic() - started_at
            assert payload["started"] is True
            assert elapsed < 0.5
            assert coordinator.status()["state"] == "running"
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_데모모드는_수집도_판정도_부르지_않는다() -> None:
    """데모(demo>0)에서는 오픈API·LLM 을 건드리지 않고 DB 값만 훑어 보여 준다."""
    from tools import refresh

    seen = []

    def 판정하면_안_됨(*args, **kwargs):
        raise AssertionError("데모에서 판정을 불렀다")

    started = time.monotonic()
    result = refresh.run(collect_first=True, demo=0.5,
                         evaluate=판정하면_안_됨,
                         progress=seen.append)
    걸린시간 = time.monotonic() - started

    assert result["demo"] is True
    assert result["refreshed"] == 0
    assert result["cached"] == result["total"], "판정 안 된 건이 남아 있다"
    # 수집 몫 1/3 + 판정 몫 1 = 0.5 초의 4/3 남짓
    assert 0.5 <= 걸린시간 < 2.0, 걸린시간
    assert [u["phase"] for u in seen][0] == "collecting"
    진행 = [u["done"] for u in seen if u["phase"] == "judging"]
    assert 진행 == sorted(진행) and len(진행) == 10, 진행


def test_데모모드도_중지를_듣는다() -> None:
    """촬영 중 잘못 눌러도 중지 버튼이 그대로 먹어야 한다."""
    from tools import refresh

    멈춤 = [False]
    본_것 = []

    def 진행(update):
        본_것.append(update)
        if len(본_것) >= 3:
            멈춤[0] = True

    result = refresh.run(demo=0.5, progress=진행, should_stop=lambda: 멈춤[0])
    assert result["stopped"] is True
    assert len(본_것) < 10, len(본_것)


def demo() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"모두 통과 ({len(tests)}개)")


if __name__ == "__main__":
    demo()

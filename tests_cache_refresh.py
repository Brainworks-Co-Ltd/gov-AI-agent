"""판정 캐시 유효성·백그라운드 갱신 회귀 테스트.

실제 ``data/app.db``와 외부 API는 사용하지 않는다. 임시 SQLite 파일과 결정적인
가짜 판정기만 사용하므로 다음 명령으로 반복 실행할 수 있다.

    python -X utf8 tests_cache_refresh.py
"""
from __future__ import annotations

import sqlite3
import tempfile
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


def demo() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"모두 통과 ({len(tests)}개)")


if __name__ == "__main__":
    demo()

"""SQLite 저장소 — 표준 라이브러리 sqlite3만 사용 (추가 설치 없음).

왜 파일(JSON)이 아니라 DB인가:
매일 수집이 돌면 공고가 쌓이고, 같은 공고가 다시 들어온다. "이미 본 공고인가"를
매번 전체 파일을 뒤져서 판단하면 느리고, 판정 결과·초안까지 함께 관리하기 어렵다.
(source, source_id) UNIQUE 하나로 재수집 시 자동 갱신(upsert)되게 하는 게 핵심.

테이블 4개:
    notices   수집한 공고 (원본 JSON 통째로 보관)
    clusters  중복 통합 결과 (대표 공고 + 통합 근거)
    verdicts  공고별 자격 판정 결과 (캐시 — LLM 재호출 비용을 아낀다)
    drafts    신청서 초안 + 점검 결과
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date

from agent.schemas import Notice

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(_DATA_DIR, "app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id           TEXT PRIMARY KEY,          -- "기업마당:PBLN_000..."
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    agency       TEXT,
    url          TEXT,
    summary      TEXT,
    target_text  TEXT,
    exclude_text TEXT,
    support_field TEXT,
    region_text  TEXT,
    apply_begin  TEXT,                      -- ISO 날짜 문자열 (미상이면 NULL)
    apply_end    TEXT,
    docs_text    TEXT,
    attachments  TEXT,                      -- 첨부파일 목록 JSON
    raw          TEXT,                      -- 원본 JSON 문자열
    cluster_id   TEXT,
    first_seen   TEXT,                      -- 처음 수집한 날 (신규 공고 알림용)
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_notices_end ON notices(apply_end);
CREATE INDEX IF NOT EXISTS idx_notices_cluster ON notices(cluster_id);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id      TEXT PRIMARY KEY,
    representative  TEXT NOT NULL,          -- notices.id
    member_ids      TEXT NOT NULL,          -- JSON 배열
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    notice_id    TEXT PRIMARY KEY,
    overall      TEXT NOT NULL,
    report_json  TEXT NOT NULL,
    profile_hash TEXT NOT NULL,             -- 프로필이 바뀌면 캐시를 버려야 한다
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS form_specs (
    notice_id   TEXT PRIMARY KEY,
    spec_json   TEXT NOT NULL,           -- 첨부 서식에서 읽어낸 항목·기입란·제출서류
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS doc_checks (
    notice_id   TEXT PRIMARY KEY,
    checks_json TEXT NOT NULL,          -- {"서류명": true/false} — 담당자가 체크한 상태
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    notice_id    TEXT PRIMARY KEY,
    draft_json   TEXT NOT NULL,
    issues_json  TEXT,
    created_at   TEXT
);
"""


# 나중에 추가된 컬럼들. CREATE TABLE IF NOT EXISTS 는 **이미 있는 표를 고치지 않으므로**,
# 예전에 만들어 둔 app.db 를 쓰는 사람에게는 이 컬럼이 없다. 그대로 두면 INSERT 가
# "no such column" 으로 죽는다 — DB를 지우라고 안내하는 대신 조용히 채워 넣는다.
_ADDED_COLUMNS = {"notices": {"attachments": "TEXT"}}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


# ---------------------------------------------------------------- notices

def upsert_notices(conn: sqlite3.Connection, notices: list[Notice]) -> tuple[int, int]:
    """공고를 저장한다. 반환: (신규 건수, 갱신 건수).

    이미 있는 공고는 내용만 덮어쓰고 first_seen은 유지한다 — '언제 처음 뜬 공고인가'는
    담당자에게 의미 있는 정보라 재수집 때마다 초기화하면 안 된다.
    """
    today = date.today().isoformat()
    existing = {r["id"] for r in conn.execute("SELECT id FROM notices")}
    new_cnt = updated_cnt = 0
    for n in notices:
        is_new = n.id not in existing
        conn.execute(
            """INSERT INTO notices
                 (id, source, source_id, title, agency, url, summary, target_text,
                  exclude_text, support_field, region_text, apply_begin, apply_end, docs_text,
                  attachments, raw, cluster_id, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, agency=excluded.agency, url=excluded.url,
                 summary=excluded.summary, target_text=excluded.target_text,
                 exclude_text=excluded.exclude_text,
                 support_field=excluded.support_field,
                 region_text=excluded.region_text, apply_begin=excluded.apply_begin,
                 apply_end=excluded.apply_end, docs_text=excluded.docs_text,
                 attachments=excluded.attachments, raw=excluded.raw""",
            (n.id, n.source, n.source_id, n.title, n.agency, n.url, n.summary,
             n.target_text, n.exclude_text, n.support_field, n.region_text,
             n.apply_begin.isoformat() if n.apply_begin else None,
             n.apply_end.isoformat() if n.apply_end else None,
             n.docs_text, json.dumps(n.attachments, ensure_ascii=False),
             json.dumps(n.raw, ensure_ascii=False), n.cluster_id, today),
        )
        new_cnt += is_new
        updated_cnt += not is_new
    conn.commit()
    return new_cnt, updated_cnt


def _row_to_notice(row: sqlite3.Row) -> Notice:
    return Notice.from_dict(dict(row))


def all_notices(conn: sqlite3.Connection) -> list[Notice]:
    return [_row_to_notice(r) for r in conn.execute("SELECT * FROM notices")]


def get_notice(conn: sqlite3.Connection, notice_id: str) -> Notice | None:
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    return _row_to_notice(row) if row else None


def open_notices(conn: sqlite3.Connection, include_closed: bool = False) -> list[Notice]:
    """마감 D-day 오름차순 공고 목록.

    마감일 미상(apply_end NULL) 공고는 버리지 않고 맨 뒤로 보낸다 — 기관이 날짜를
    자유 서식으로 써서 파싱이 안 되는 경우가 실제로 있고, 그런 공고를 화면에서
    통째로 감추면 담당자가 기회를 놓친다.
    """
    today = date.today().isoformat()
    if include_closed:
        rows = conn.execute("SELECT * FROM notices")
    else:
        rows = conn.execute(
            "SELECT * FROM notices WHERE apply_end IS NULL OR apply_end >= ?", (today,))
    notices = [_row_to_notice(r) for r in rows]
    notices.sort(key=lambda n: (n.d_day is None, n.d_day if n.d_day is not None else 0))
    return notices


def open_businesses(conn: sqlite3.Connection,
                    include_closed: bool = False) -> list[Notice]:
    """**사업 단위** 목록 — 중복 통합된 공고는 대표 1건만 남긴다.

    화면에 뿌릴 목록은 반드시 이 함수를 쓴다. open_notices()를 그대로 쓰면 기업마당
    공고와 K-Startup 공고가 나란히 뜨는데, 그러면 중복을 제거한 의미가 없다 —
    담당자는 여전히 같은 사업을 두 번 검토하게 된다.
    """
    reps = {r["cluster_id"]: r["representative"]
            for r in conn.execute("SELECT cluster_id, representative FROM clusters")}
    picked: list[Notice] = []
    seen_clusters: set[str] = set()
    for n in open_notices(conn, include_closed):
        if not n.cluster_id:                 # 아직 통합을 안 돌린 공고
            picked.append(n)
            continue
        if n.cluster_id in seen_clusters:
            continue
        rep_id = reps.get(n.cluster_id)
        # 대표가 마감돼 목록에서 빠졌다면, 살아 있는 이 공고를 대표로 쓴다.
        rep = get_notice(conn, rep_id) if rep_id else None
        chosen = rep if (rep is not None and (include_closed or
                                              rep.d_day is None or rep.d_day >= 0)) else n
        seen_clusters.add(n.cluster_id)
        picked.append(chosen)
    picked.sort(key=lambda n: (n.d_day is None, n.d_day if n.d_day is not None else 0))
    return picked


# --------------------------------------------------------------- clusters

def save_clusters(conn: sqlite3.Connection, clusters) -> None:
    """중복 통합 결과를 저장하고, 각 공고 행에 cluster_id를 다시 써넣는다."""
    conn.execute("DELETE FROM clusters")
    for c in clusters:
        member_ids = [m.id for m in c.members]
        conn.execute(
            "INSERT INTO clusters (cluster_id, representative, member_ids, reason) "
            "VALUES (?,?,?,?)",
            (c.cluster_id, c.representative.id,
             json.dumps(member_ids, ensure_ascii=False), c.reason))
        for mid in member_ids:
            conn.execute("UPDATE notices SET cluster_id = ? WHERE id = ?",
                         (c.cluster_id, mid))
    conn.commit()


def cluster_of(conn: sqlite3.Connection, notice_id: str) -> dict | None:
    row = conn.execute(
        "SELECT c.* FROM clusters c JOIN notices n ON n.cluster_id = c.cluster_id "
        "WHERE n.id = ?", (notice_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["member_ids"] = json.loads(d["member_ids"])
    return d


# --------------------------------------------------------------- verdicts

def save_verdict(conn: sqlite3.Connection, notice_id: str, overall: str,
                 report: dict, profile_hash: str) -> None:
    conn.execute(
        "INSERT INTO verdicts (notice_id, overall, report_json, profile_hash, created_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(notice_id) DO UPDATE SET "
        "overall=excluded.overall, report_json=excluded.report_json, "
        "profile_hash=excluded.profile_hash, created_at=excluded.created_at",
        (notice_id, overall, json.dumps(report, ensure_ascii=False), profile_hash,
         date.today().isoformat()))
    conn.commit()


def get_verdict(conn: sqlite3.Connection, notice_id: str,
                profile_hash: str) -> dict | None:
    """캐시된 판정을 돌려준다. 회사 프로필이 바뀌었으면(해시 불일치) None.

    프로필이 판정의 절반이므로, 담당자가 업력·인원을 고치면 이전 판정은 무효다.
    해시로 이걸 자동 처리해서 '고쳤는데 결과가 그대로'인 혼란을 막는다.
    """
    row = conn.execute("SELECT * FROM verdicts WHERE notice_id = ?",
                       (notice_id,)).fetchone()
    if not row or row["profile_hash"] != profile_hash:
        return None
    return json.loads(row["report_json"])


def all_verdicts(conn: sqlite3.Connection, profile_hash: str) -> dict[str, str]:
    """공고 목록 화면의 뱃지용 — {notice_id: 판정}. 프로필이 바뀐 건 제외."""
    return {r["notice_id"]: r["overall"]
            for r in conn.execute("SELECT notice_id, overall, profile_hash FROM verdicts")
            if r["profile_hash"] == profile_hash}


# ------------------------------------------------------------- form_specs

def save_form_spec(conn: sqlite3.Connection, notice_id: str, spec: dict) -> None:
    conn.execute(
        "INSERT INTO form_specs (notice_id, spec_json, created_at) VALUES (?,?,?) "
        "ON CONFLICT(notice_id) DO UPDATE SET spec_json=excluded.spec_json, "
        "created_at=excluded.created_at",
        (notice_id, json.dumps(spec, ensure_ascii=False), date.today().isoformat()))
    conn.commit()


def get_form_spec(conn: sqlite3.Connection, notice_id: str) -> dict | None:
    """캐시된 서식 분석 결과.

    서식 파일은 공고가 끝날 때까지 바뀌지 않으므로 기한을 두지 않는다. 첨부를 내려받아
    열고 LLM까지 부르는 비싼 작업이라, 같은 공고를 다시 열 때마다 반복하면 안 된다.
    """
    row = conn.execute("SELECT spec_json FROM form_specs WHERE notice_id = ?",
                       (notice_id,)).fetchone()
    return json.loads(row["spec_json"]) if row else None


# ------------------------------------------------------------- doc_checks

def get_doc_checks(conn: sqlite3.Connection, notice_id: str) -> dict:
    """담당자가 체크해 둔 제출서류 상태 {서류명: True/False}.

    서류를 실제로 뗐는지는 사람만 아는 정보라, 화면을 닫아도 남아야 한다.
    """
    row = conn.execute("SELECT checks_json FROM doc_checks WHERE notice_id = ?",
                       (notice_id,)).fetchone()
    return json.loads(row["checks_json"]) if row else {}


def save_doc_checks(conn: sqlite3.Connection, notice_id: str, checks: dict) -> None:
    conn.execute(
        "INSERT INTO doc_checks (notice_id, checks_json, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(notice_id) DO UPDATE SET checks_json=excluded.checks_json, "
        "updated_at=excluded.updated_at",
        (notice_id, json.dumps(checks, ensure_ascii=False), date.today().isoformat()))
    conn.commit()


# ----------------------------------------------------------------- drafts

def save_draft(conn: sqlite3.Connection, notice_id: str, draft: dict,
               issues: list | None = None) -> None:
    conn.execute(
        "INSERT INTO drafts (notice_id, draft_json, issues_json, created_at) "
        "VALUES (?,?,?,?) ON CONFLICT(notice_id) DO UPDATE SET "
        "draft_json=excluded.draft_json, issues_json=excluded.issues_json, "
        "created_at=excluded.created_at",
        (notice_id, json.dumps(draft, ensure_ascii=False),
         json.dumps(issues or [], ensure_ascii=False), date.today().isoformat()))
    conn.commit()


def get_draft(conn: sqlite3.Connection, notice_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM drafts WHERE notice_id = ?",
                       (notice_id,)).fetchone()
    if not row:
        return None
    return {"draft": json.loads(row["draft_json"]),
            "issues": json.loads(row["issues_json"] or "[]")}

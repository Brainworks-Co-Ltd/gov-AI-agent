"""수집 잡 — 두 기관 오픈API 호출 → 중복 통합 → DB 저장.

실행:
    python -m tools.ingest              # 실제 수집 + 중복 통합 + 저장
    python -m tools.ingest --dry-run    # 응답 원본 필드명만 확인 (저장 안 함)
    python -m tools.ingest --no-llm     # 중복 판정에서 LLM 경계 판정 끄기 (빠름)
    python -m tools.ingest --offline    # API를 부르지 않고 캐시만 사용

설계 원칙 — **수집이 실패해도 서비스는 살아 있어야 한다.**
한 기관이 죽어도 다른 기관 것은 저장하고, 둘 다 실패하면 마지막 성공분 캐시
(data/notices_cache.json)로 폴백한다. 본선 현장에서 네트워크나 인증키 문제가
생겨도 데모가 멈추지 않게 하려는 안전망이다.
"""
from __future__ import annotations

import json
import os
import sys

from agent.schemas import Notice
from tools import bizinfo_client, dedupe, kstartup_client, store

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

# 마지막 실수집 결과. 수집이 성공할 때마다 갱신된다.
CACHE_PATH = os.path.join(_DATA_DIR, "notices_cache.json")
# 연습용 더미 공고(정답셋과 짝을 이룸). **실수집으로 덮어쓰지 않는다** — 여기가
# 덮어써지면 score.py가 채점할 대상을 잃어 회귀 방지 장치가 통째로 사라진다.
GOLDEN_PATH = os.path.join(_DATA_DIR, "golden_notices.json")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _read(path: str) -> list[Notice]:
    try:
        with open(path, encoding="utf-8") as f:
            return [Notice.from_dict(d) for d in json.load(f)]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def load_golden() -> list[Notice]:
    """연습용 더미 공고 — score.py 채점 대상."""
    return _read(GOLDEN_PATH)


def load_cache() -> list[Notice]:
    """오프라인 폴백용 공고. 실수집 캐시가 없으면 더미로 내려간다.

    이 순서 덕분에 처음 받아 본 사람도 키 없이 바로 데모할 수 있고, 한 번이라도
    실수집에 성공한 뒤에는 진짜 공고로 폴백한다.
    """
    return _read(CACHE_PATH) or load_golden()


def save_cache(notices: list[Notice]) -> None:
    """실수집 결과만 저장한다 (더미 파일은 건드리지 않는다)."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([n.to_dict() for n in notices], f, ensure_ascii=False, indent=1)


def collect(offline: bool = False) -> tuple[list[Notice], list[str]]:
    """두 기관에서 공고를 모은다. 반환: (공고 목록, 사람에게 보여줄 알림 메시지들)."""
    notes: list[str] = []
    notices: list[Notice] = []

    if offline:
        cached = load_cache()
        notes.append(f"오프라인 모드 — 캐시에서 {len(cached)}건을 불러왔습니다.")
        return cached, notes

    for name, fetcher in (("기업마당", bizinfo_client.fetch),
                          ("K-Startup", kstartup_client.fetch)):
        try:
            got = fetcher()
            notices.extend(got)
            notes.append(f"{name}: {len(got)}건 수집")
        except Exception as e:
            notes.append(f"{name}: 수집 실패 — {e}")

    if not notices:
        cached = load_cache()
        if cached:
            notes.append(f"두 기관 모두 실패해 캐시 {len(cached)}건으로 대체했습니다.")
            return cached, notes
        notes.append("수집된 공고가 없고 캐시도 비어 있습니다.")
    return notices, notes


def run(use_llm: bool = True, offline: bool = False) -> dict:
    """수집 → 중복 통합 → 저장까지 한 번에. 반환값은 API 응답으로 그대로 쓴다."""
    notices, notes = collect(offline=offline)
    if not notices:
        return {"ok": False, "notes": notes, "new": 0, "updated": 0,
                "total": 0, "db_total": 0, "clusters": 0, "merged": 0}

    clusters = dedupe.build_clusters(notices, use_llm=use_llm)
    for c in clusters:                       # 저장 전에 각 공고에 소속 클러스터를 적어둔다
        for m in c.members:
            m.cluster_id = c.cluster_id

    conn = store.connect()
    try:
        new_cnt, updated_cnt = store.upsert_notices(conn, notices)
        store.save_clusters(conn, clusters)
        # 오픈API는 최근 공고만 돌려주므로, DB에는 지난 수집분이 함께 쌓여 있다.
        # '이번에 받은 건수'와 '쌓인 건수'는 다른 숫자라 따로 보여준다.
        db_total = len(store.all_notices(conn))
    finally:
        conn.close()

    if not offline:
        save_cache(notices)                  # 다음 번 오프라인 데모용 최신 캐시

    merged = sum(1 for c in clusters if c.is_merged)
    notes.append(f"중복 통합: 공고 {len(notices)}건 → 사업 {len(clusters)}건 "
                 f"(양 기관 중복 {merged}건 통합)")
    return {"ok": True, "notes": notes, "new": new_cnt, "updated": updated_cnt,
            "total": len(notices), "db_total": db_total,
            "clusters": len(clusters), "merged": merged}


def dry_run() -> None:
    """실제 응답 원본을 찍어본다 — K-Startup 필드명 확정용."""
    for name, sampler in (("기업마당", bizinfo_client.fetch_raw_sample),
                          ("K-Startup", kstartup_client.fetch_raw_sample)):
        print(f"\n{'=' * 60}\n{name} 응답 원본\n{'=' * 60}")
        try:
            rows = sampler(2)
        except Exception as e:
            print(f"  호출 실패 — {e}")
            continue
        if not rows:
            print("  (응답에 공고 행이 없습니다)")
            continue
        print(f"  필드 {len(rows[0])}개: {sorted(rows[0].keys())}\n")
        print(json.dumps(rows[0], ensure_ascii=False, indent=2)[:2500])


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if "--dry-run" in args:
        dry_run()
    else:
        result = run(use_llm="--no-llm" not in args, offline="--offline" in args)
        print()
        for note in result["notes"]:
            print(f"  · {note}")
        print(f"\n  이번 수집 {result['total']}건 (신규 {result['new']} / 갱신 "
              f"{result['updated']}) · DB 누적 {result['db_total']}건")

"""CLI 시연 — 수집부터 점검까지 한 번에 돌려본다.

웹 화면 없이도 파이프라인 전체가 살아 있는지 30초 만에 확인할 수 있다. 본선 발표
직전 점검용이자, 서버가 안 뜨는 상황의 대비책이기도 하다.

실행:
    python demo.py                  # 수집(오프라인 캐시) → 목록 → 판정 → 초안 → 점검
    python demo.py --online         # 실제 오픈API로 수집해서 진행
    python demo.py --notice <ID>    # 특정 공고 하나만 자세히
"""
from __future__ import annotations

import sys

from agent import orchestrator
from agent.schemas import VERDICT_CHECK, VERDICT_NO, VERDICT_OK
from tools import ingest, profile_store, store

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_BADGE = {VERDICT_OK: "[가능]  ", VERDICT_NO: "[불가]  ", VERDICT_CHECK: "[확인필요]"}
_LINE = "─" * 74


def _head(title: str) -> None:
    print(f"\n{_LINE}\n  {title}\n{_LINE}")


def show_notice(conn, notice, profile) -> None:
    """공고 1건 — 판정표 · 초안 · 점검을 차례로 보여준다."""
    _head(f"② 자격 판정 — {notice.title}")
    report = orchestrator.eligibility_of(conn, notice, profile)
    print(f"  종합 판정: {report.overall}   ({report.schedule.get('label', '')})")
    if report.note:
        print(f"  알림: {report.note}")

    print(f"\n  {'요건':<26}{'우리 회사':<20}판정")
    print(f"  {'-' * 70}")
    for r in report.rows:
        value = f"{r.requirement.axis}: {r.requirement.value}"
        print(f"  {value[:25]:<26}{r.company_value[:19]:<20}{r.verdict}")
        print(f"      이유 · {r.reason}")
        print(f"      근거 · \"{r.requirement.quote[:60]}\"")
    if report.required_docs:
        print(f"\n  제출서류: {', '.join(report.required_docs)}")

    _head(f"③ 신청서 초안 — {notice.title}")
    draft = orchestrator.draft_of(conn, notice, profile)
    if draft.note:
        print(f"  알림: {draft.note}\n")
    for s in draft.sections:
        source = f"  (참고: {', '.join(s.sources)})" if s.sources else ""
        print(f"\n  ■ {s.title}{source}")
        for line in s.body.split("\n"):
            print(f"    {line}")
    if draft.unresolved:
        print(f"\n  담당자가 채워야 할 값: {', '.join(draft.unresolved)}")

    _head(f"④ 제출 전 점검 — {notice.title}")
    issues = orchestrator.check_of(conn, notice, draft, profile)
    if not issues:
        print("  발견된 문제가 없습니다.")
    for i in issues:
        print(f"  [{i.severity}] {i.kind} · {i.where}")
        print(f"      {i.message}")
        if i.suggestion:
            print(f"      → {i.suggestion}")


def main() -> None:
    args = sys.argv[1:]
    offline = "--online" not in args
    target = None
    if "--notice" in args:
        target = args[args.index("--notice") + 1]

    _head("① 공고 수집 · 중복 통합")
    result = ingest.run(use_llm=True, offline=offline)
    for note in result["notes"]:
        print(f"  · {note}")

    conn = store.connect()
    profile = profile_store.load()
    try:
        years = f" / 업력 {profile.years:.1f}년" if profile.years else ""
        print(f"\n  회사 프로필: {profile.name} / {profile.industry} / {profile.region}"
              f" / 상시근로자 {profile.employees}명{years}")

        if target:
            notice = store.get_notice(conn, target)
            if notice is None:
                print(f"\n  그런 공고가 없습니다: {target}")
                return
            show_notice(conn, notice, profile)
            return

        # 목록 — 사업 단위(중복 통합 후), 마감 임박순.
        # 판정은 앞의 몇 건만 한다 — 전체를 돌리면 LLM 호출 때문에 시연이 느려진다.
        notices = store.open_businesses(conn)
        _head("공고함 — 마감 임박순 (중복 통합 후 사업 단위)")
        preview = notices[:6]
        for n in preview:
            report = orchestrator.eligibility_of(conn, n, profile)
            view = orchestrator.notice_view(conn, n)
            merged = f"  [{' · '.join(view['sources'])} 통합]" if view["merged"] else ""
            print(f"  {report.schedule.get('label', ''):>7}  "
                  f"{_BADGE.get(report.overall, '')}  {n.title[:44]}{merged}")
        print(f"\n  (사업 {len(notices)}건 중 {len(preview)}건만 판정했습니다 — "
              f"나머지는 웹 화면에서 확인하세요)")

        # 가장 먼저 손대야 할 공고 = 신청 가능한 것 중 마감이 가장 급한 것
        best = next((n for n in preview
                     if orchestrator.eligibility_of(conn, n, profile).overall == VERDICT_OK),
                    preview[0] if preview else None)
        if best is not None:
            show_notice(conn, best, profile)

        print(f"\n{_LINE}\n  끝. 웹 화면으로 보려면: python serve.py\n{_LINE}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

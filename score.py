"""채점 — 이 도구가 실제로 얼마나 맞히는지 숫자로 확인한다.

두 가지를 잰다.

  ① 중복 제거 (data/golden_dupe.csv)
     사람이 '같은 사업 / 다른 사업'을 라벨링한 공고쌍으로 precision·recall을 낸다.
     precision이 낮으면 서로 다른 공고를 합쳐 지원 기회를 통째로 날린 것이고,
     recall이 낮으면 같은 공고를 두 번 검토하게 만든 것이다. 전자가 훨씬 나쁘다.

  ② 자격 판정 (data/golden_eligibility.csv)
     사람이 매긴 가능/불가/확인필요와 대조해 정확도와 혼동행렬을 낸다.
     특히 **'불가'인데 '가능'이라고 답한 건수(위양성)** 를 따로 뽑는다. 담당자가
     쓸 수 없는 공고에 서류를 준비하게 만드는, 가장 비싼 실수이기 때문이다.

실행:
    python score.py              # 둘 다 채점
    python score.py --dupe       # 중복 제거만
    python score.py --eligibility  # 자격 판정만 (LLM 호출 있음 — 시간이 걸린다)
"""
from __future__ import annotations

import csv
import io
import os
import sys

from agent import eligibility
from agent.schemas import VERDICT_CHECK, VERDICT_NO, VERDICT_OK, VERDICTS
from tools import dedupe, ingest, profile_store

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _read_csv(name: str) -> list[dict]:
    path = os.path.join(_DATA_DIR, name)
    try:
        with io.open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"  정답셋이 없습니다: data/{name}  (python -m tools.seed_dummy 로 생성)")
        return []


# ──────────────────────────────────────────────────────── ① 중복 제거 채점

def score_dupe(use_llm: bool = True) -> None:
    rows = _read_csv("golden_dupe.csv")
    if not rows:
        return
    # 실수집 캐시가 아니라 **더미 공고**로 채점한다 — 정답셋이 더미 기준이라,
    # 실제 공고를 수집해도 채점 결과가 흔들리지 않아야 회귀 방지 장치로 쓸 수 있다.
    notices = {n.id: n for n in ingest.load_golden()}
    missing = {r["notice_a"] for r in rows} | {r["notice_b"] for r in rows}
    missing -= set(notices)
    if missing:
        print(f"  더미 공고에 없는 {len(missing)}건은 채점에서 제외합니다.")
        rows = [r for r in rows
                if r["notice_a"] not in missing and r["notice_b"] not in missing]

    # 실제 파이프라인과 같은 방식으로 클러스터를 만든 뒤, 쌍 단위로 정답과 대조한다.
    clusters = dedupe.build_clusters(list(notices.values()), use_llm=use_llm)
    cluster_of = {m.id: c.cluster_id for c in clusters for m in c.members}

    tp = fp = fn = tn = 0
    mistakes: list[str] = []
    for r in rows:
        gold = r["same"] == "1"
        pred = cluster_of.get(r["notice_a"]) == cluster_of.get(r["notice_b"])
        if gold and pred:
            tp += 1
        elif gold and not pred:
            fn += 1
            mistakes.append(f"  [놓침] {notices[r['notice_a']].title[:34]}"
                            f"  ↔  {notices[r['notice_b']].title[:34]}")
        elif not gold and pred:
            fp += 1
            mistakes.append(f"  [오통합] {notices[r['notice_a']].title[:34]}"
                            f"  ↔  {notices[r['notice_b']].title[:34]}")
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    print("\n① 중복 제거")
    print(f"  정답셋 {len(rows)}쌍 (같음 {tp + fn}쌍 / 다름 {tn + fp}쌍)")
    print(f"  precision {precision:.3f}  recall {recall:.3f}  F1 {f1:.3f}")
    print(f"  통합 성공 {tp} · 놓침 {fn} · 잘못 통합 {fp}")
    if mistakes:
        print("  틀린 쌍:")
        print("\n".join(mistakes))


# ──────────────────────────────────────────────────────── ② 자격 판정 채점

def score_eligibility() -> None:
    rows = _read_csv("golden_eligibility.csv")
    if not rows:
        return

    # DB를 거치지 않고 더미 공고에서 바로 판정한다. DB를 읽으면 실제 공고를 수집한
    # 뒤 클러스터 표가 실데이터로 갈아엎여, 더미 공고의 '통합된 짝'이 사라진 채로
    # 채점돼 점수가 엉뚱하게 흔들린다.
    notices = {n.id: n for n in ingest.load_golden()}
    clusters = dedupe.build_clusters(list(notices.values()), use_llm=True)
    siblings: dict[str, list] = {}
    for c in clusters:
        for m in c.members:
            siblings[m.id] = [x for x in c.members if x.id != m.id]

    profile = profile_store.load()
    matrix = {g: {p: 0 for p in VERDICTS} for g in VERDICTS}
    wrong: list[str] = []
    graded = 0

    for r in rows:
        notice = notices.get(r["notice_id"])
        if notice is None:
            print(f"  더미 공고에 없어 건너뜁니다: {r['notice_id']}")
            continue

        report = eligibility.evaluate(notice, profile, siblings.get(notice.id, []))
        gold, pred = r["verdict"], report.overall
        if gold not in VERDICTS:
            continue
        matrix[gold][pred] += 1
        graded += 1
        if gold != pred:
            wrong.append(f"  정답 {gold} → 예측 {pred} : {notice.title[:40]}\n"
                         f"      정답 근거: {r.get('note', '')}")

    if not graded:
        print("\n② 자격 판정 — 채점할 항목이 없습니다. "
              "(python -m tools.seed_dummy 로 더미 데이터를 만드세요)")
        return

    correct = sum(matrix[v][v] for v in VERDICTS)
    # 가장 비싼 실수: 실제로는 신청할 수 없는데 '가능'이라고 답한 경우.
    false_ok = matrix[VERDICT_NO][VERDICT_OK] + matrix[VERDICT_CHECK][VERDICT_OK]

    print("\n② 자격 판정")
    print(f"  정답셋 {graded}건 · 정확도 {correct / graded:.3f} ({correct}/{graded})")
    print(f"  위양성(불가·확인필요를 '가능'으로 판정) {false_ok}건  ← 가장 비싼 실수")
    print("\n  혼동행렬 (행=정답, 열=예측)")
    header = "".join(f"{v:>10}" for v in VERDICTS)
    print(f"        {header}")
    for g in VERDICTS:
        cells = "".join(f"{matrix[g][p]:>10}" for p in VERDICTS)
        print(f"  {g:<6}{cells}")
    if wrong:
        print("\n  틀린 건:")
        print("\n".join(wrong))


if __name__ == "__main__":
    args = set(sys.argv[1:])
    run_dupe = "--eligibility" not in args
    run_elig = "--dupe" not in args
    if run_dupe:
        score_dupe(use_llm="--no-llm" not in args)
    if run_elig:
        score_eligibility()
    print()

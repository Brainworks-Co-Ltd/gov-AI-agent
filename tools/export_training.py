"""튜닝(학습) 데이터셋 만들기 — CLOVA Studio '학습'에 올릴 CSV를 뽑는다.

튜닝은 모델에게 **"이런 입력이 오면 이런 문체·구성으로 써라"**를 예시로 가르치는 일이다.
그러니 학습 데이터의 입력은 실제 서비스에서 넣는 프롬프트와 **같은 모양**이어야 하고,
정답(Completion)은 우리가 원하는 결과물이어야 한다. 그래서 여기서는

    입력(Text)      = agent/drafter.py 가 실제로 만드는 프롬프트와 같은 형식
    정답(Completion) = 과거에 실제로 제출한 신청서 문장 (data/past_applications/)

로 짝을 만든다. 형식이 어긋나면 학습해도 서비스에서 효과가 안 난다.

CLOVA Studio 학습 데이터셋 CSV 컬럼 (순서 고정):
    System_Prompt, C_ID, T_ID, Text, Completion
  · C_ID = 대화(샘플) 번호, T_ID = 그 대화 안의 순서. 둘 다 0부터 1씩 증가.
  · 한 번에 주고받는 단발성 학습이면 T_ID는 전부 0이다.

실행:
    python -m tools.export_training                 # data/training_draft.csv 생성
    python -m tools.export_training --min-chars 200 # 짧은 문단은 제외
"""
from __future__ import annotations

import csv
import os
import sys

from agent import drafter
from tools import past_search, profile_store

_ROOT = os.path.dirname(os.path.dirname(__file__))
OUT_PATH = os.path.join(_ROOT, "data", "training_draft.csv")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _build_row(passage, profile, others) -> tuple[str, str] | None:
    """과거 신청서 문항 하나 → (입력 프롬프트, 정답 문장).

    같은 문서의 다른 문항을 '참고 문장'으로 넣는다. 실제 서비스에서도 과거 신청서를
    참고로 넣기 때문이다. 정답 문장 자신을 참고로 넣으면 베껴 쓰기만 배우므로 뺀다.
    """
    body = passage.text.strip()
    if not body:
        return None

    reference = "\n\n".join(
        f"[{p.source} — {p.heading}]\n{p.text[:600]}" for p in others[:2]
    ) or "(참고할 과거 문장이 없습니다)"

    user = (
        f"[공고]\n제목: {passage.source.replace('.md', '')}\n소관기관: \n"
        f"지원분야: \n개요: \n지원대상: \n\n"
        f"[회사 정보]\n{drafter._profile_block(profile)}\n\n"
        f"[참고: 과거 신청서 문장]\n### {passage.heading}\n{reference}\n\n"
        f"[작성할 항목]\n- {passage.heading}"
    )
    completion = f"■ {passage.heading}\n{body}"
    return user, completion


def main() -> None:
    args = sys.argv[1:]
    # 한글 신청서 문단은 100자 안팎이 흔하다. 너무 높게 잡으면 쓸 만한 문항까지
    # 통째로 걸러진다(처음 120자로 뒀다가 표본이 0건이 됐다).
    min_chars = 60
    if "--min-chars" in args:
        min_chars = int(args[args.index("--min-chars") + 1])

    profile = profile_store.load()
    passages = past_search.load_passages()
    if not passages:
        print("\n  data/past_applications/ 에 과거 신청서가 없습니다.")
        print("  실제로 제출했던 신청서를 .md 파일로 넣어 주세요 "
              "(## 문항제목 으로 항목을 나누면 됩니다).\n")
        return

    rows: list[list] = []
    skipped = 0
    for i, passage in enumerate(passages):
        if len(passage.text.strip()) < min_chars:
            skipped += 1
            continue
        others = [p for p in passages
                  if p.source == passage.source and p.heading != passage.heading]
        built = _build_row(passage, profile, others)
        if not built:
            continue
        user, completion = built
        # C_ID는 샘플마다 증가, T_ID는 단발성이라 0 고정.
        rows.append([drafter._TUNED_SYSTEM, len(rows), 0, user, completion])

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["System_Prompt", "C_ID", "T_ID", "Text", "Completion"])
        writer.writerows(rows)

    print(f"\n  학습 데이터 {len(rows)}건 → {os.path.relpath(OUT_PATH, _ROOT)}")
    if skipped:
        print(f"  ({min_chars}자 미만이라 제외한 문항 {skipped}건 — "
              f"--min-chars 로 조절)")
    if len(rows) < 50:
        print(f"\n  ⚠ 표본이 {len(rows)}건뿐입니다. 튜닝은 보통 수백 건은 있어야 "
              f"문체가 잡힙니다.")
        print(f"    과거 신청서를 data/past_applications/ 에 더 넣고 다시 실행하세요.")
    print(f"\n  다음: CLOVA Studio → 학습 → 이 CSV를 Object Storage에 올려 학습 생성")
    print(f"        학습이 끝나면 작업 ID를 clova_tuned_task.txt 에 붙여 넣으세요.\n")


if __name__ == "__main__":
    main()

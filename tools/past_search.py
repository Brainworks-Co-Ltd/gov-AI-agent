"""과거 신청서에서 비슷한 문항을 찾아온다 — 초안 생성의 재료 검색.

담당자가 실제로 하는 일이 이거다. 새 공고를 받으면 "작년에 비슷한 거 썼던 것 같은데"
하며 예전 파일을 뒤진다. 그 뒤지는 일을 대신한다.

임베딩을 쓰지 않고 키워드 가중치(TF-IDF에 가까운 방식)만 쓴다. 과거 신청서는 많아야
수십 건이라 의미 검색까지 갈 필요가 없고, 임베딩 API 호출이 없으면 오프라인에서도
그대로 동작하기 때문이다.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter

_PAST_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "past_applications")

# 어느 문서에나 나와서 변별력이 없는 단어.
_STOP = {"사업", "지원", "기업", "신청", "내용", "계획", "경우", "관련", "통해", "위해",
         "있다", "한다", "된다", "우리", "당사", "대한", "따라", "등의", "및"}

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")
# 마크다운 제목(## 사업 개요)을 기준으로 문서를 문항 단위로 쪼갠다.
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)

# ── 빈 양식 걸러내기 ────────────────────────────────────────────────────
# 담당자가 올리는 파일에는 '작성이 끝난 신청서'와 '빈 서식'이 섞인다. 빈 서식이
# 재료로 들어가면 분량만 못 채우는 게 아니라 **초안을 망친다** — 실제로 서식에 들어
# 있던 예시 문장(다른 회사의 AI 솔루션 목록)이 우리 회사 '사업 개요'에 그대로
# 옮겨 적힌 적이 있다. 심사 지침이 거짓을 탈락 사유로 못 박은 사업에서 이건 사고다.
#
# 제목만 보고 판단한다. '본문 (휴먼명조, 12pt)' 처럼 서식에만 나오는 제목이 뚜렷해서,
# 본문 내용까지 뒤지는 것보다 정확하고 오판이 적다.
_TEMPLATE_HEADING = re.compile(
    r"작성\s*(?:요령|지침|방법|안내|시\s*유의)|유의\s*사항|목\s*차|"
    r"휴먼명조|맑은\s*고딕|바탕체|\d+\s*pt|본문\s*\(|"
    r"분량|서식\s*\d|별지|붙임\s*\d|양식\s*다운")
# 너무 짧은 문항은 재료로 못 쓴다 (제목만 있고 내용은 담당자가 채울 빈칸인 경우).
_MIN_PASSAGE_CHARS = 120


def is_template(heading: str, text: str) -> bool:
    """이 문항이 '빈 서식/작성요령'이라 재료로 쓸 수 없는지."""
    if _TEMPLATE_HEADING.search(heading or ""):
        return True
    return len((text or "").strip()) < _MIN_PASSAGE_CHARS


class Passage:
    """과거 신청서의 문항 하나."""

    def __init__(self, source: str, heading: str, text: str):
        self.source = source        # 파일명
        self.heading = heading      # 문항 제목 (예: "추진 배경 및 필요성")
        self.text = text

    def __repr__(self) -> str:      # 디버깅용
        return f"<Passage {self.source}:{self.heading}>"


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text or "") if t not in _STOP]


def load_passages() -> list[Passage]:
    """data/past_applications/*.md 를 문항 단위로 읽어들인다.

    문서를 통째로 넘기지 않고 문항으로 쪼개는 이유: 새 공고의 '기대효과' 항목을 쓸 때
    필요한 건 예전 신청서의 '기대효과' 문단이지 신청서 전체가 아니다. 통째로 넘기면
    LLM이 엉뚱한 문단을 베껴 오고, 토큰도 낭비된다.
    """
    passages: list[Passage] = []
    if not os.path.isdir(_PAST_DIR):
        return passages

    for name in sorted(os.listdir(_PAST_DIR)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        # 폴더 안내문(README)은 신청서가 아니다. 걸러 내지 않으면 그 안내 문장이
        # 초안 프롬프트에 '과거 신청서 문장'으로 섞여 들어간다.
        if name.lower().startswith(("readme", "_")):
            continue
        with open(os.path.join(_PAST_DIR, name), encoding="utf-8") as f:
            content = f.read()

        marks = list(_HEADING_RE.finditer(content))
        if not marks:                       # 제목이 없으면 문서 전체를 한 문항으로
            passages.append(Passage(name, name, content.strip()))
            continue
        for i, m in enumerate(marks):
            start = m.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
            body = content[start:end].strip()
            heading = m.group(1)
            if body and not is_template(heading, body):
                passages.append(Passage(name, heading, body))
    return passages


def usable_ratio(name: str) -> tuple[int, int]:
    """파일 하나에서 (재료로 쓸 수 있는 문항 수, 전체 문항 수).

    올린 파일이 빈 서식이면 앞 숫자가 0에 가깝다. 담당자에게 "이건 재료가 안 된다"고
    바로 알려 주려고 따로 센다 — 조용히 걸러 버리면, 파일을 올렸는데도 초안이 그대로인
    이유를 담당자가 알 길이 없다.
    """
    path = os.path.join(_PAST_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        return 0, 0
    with open(path, encoding="utf-8") as f:
        content = f.read()
    marks = list(_HEADING_RE.finditer(content))
    if not marks:
        text = content.strip()
        return (0, 1) if is_template(name, text) else (1, 1)

    total = usable = 0
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        body = content[start:end].strip()
        if not body:
            continue
        total += 1
        if not is_template(m.group(1), body):
            usable += 1
    return usable, total


def _idf(passages: list[Passage]) -> dict[str, float]:
    """단어가 몇 개 문항에 등장하는지로 가중치를 매긴다.

    '전자부품'처럼 몇 문항에만 나오는 단어가 '개선'처럼 어디에나 나오는 단어보다
    검색에 훨씬 유용하다. 그 차이를 반영하려고 IDF를 쓴다.
    """
    n = len(passages) or 1
    df = Counter()
    for p in passages:
        df.update(set(_tokens(f"{p.heading} {p.text}")))
    return {w: math.log(1 + n / (1 + c)) for w, c in df.items()}


def search(query: str, passages: list[Passage] | None = None,
           top_k: int = 3) -> list[tuple[float, Passage]]:
    """질의(공고 항목명 + 공고 내용)와 가장 가까운 과거 문항 top_k개.

    문항 제목이 걸리면 점수를 두 배로 준다 — '기대효과'를 찾을 때는 본문에 그 말이
    몇 번 나오는지보다 제목이 '기대효과'인지가 훨씬 강한 신호다.
    """
    items = passages if passages is not None else load_passages()
    if not items:
        return []

    idf = _idf(items)
    q_terms = Counter(_tokens(query))
    if not q_terms:
        return []

    scored: list[tuple[float, Passage]] = []
    for p in items:
        body_terms = Counter(_tokens(p.text))
        head_terms = Counter(_tokens(p.heading))
        length = sum(body_terms.values()) or 1
        score = 0.0
        for w, qc in q_terms.items():
            weight = idf.get(w, 0.0)
            score += weight * qc * (body_terms.get(w, 0) / length)
            score += weight * qc * head_terms.get(w, 0) * 2.0
        if score > 0:
            scored.append((score, p))

    scored.sort(key=lambda t: -t[0])
    return scored[:top_k]

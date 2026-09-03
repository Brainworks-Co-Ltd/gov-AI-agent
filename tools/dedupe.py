"""중복 공고 통합 — 이 과제의 핵심 난이도 ①.

같은 사업이 기업마당과 K-Startup에 서로 다른 제목으로 올라온다.

    기업마당  : "2026년 광주광역시 중소기업 스마트공장 구축지원사업 모집공고(1차)"
    K-Startup : "[광주] 중소기업 스마트공장 구축 지원 사업 참여기업 모집"

담당자가 이걸 별개 공고로 보고 두 번 검토하면 그대로 시간 낭비다. 제목이 완전히
다르진 않지만 똑같지도 않으므로, 세 단계로 좁혀 간다:

    1) 정규화  — 연도·차수·괄호·상용어를 걷어내 '사업의 알맹이'만 남긴다
    2) 블로킹  — 마감일이 비슷하거나 기관이 같은 쌍만 후보로 (전수 비교 O(n²) 회피)
    3) 유사도  — difflib + 토큰 자카드. 확실한 구간은 코드가 결정하고,
                 애매한 경계 구간만 하이퍼클로바X에 물어본다

LLM을 경계 구간에만 쓰는 이유: 전부 LLM에 맡기면 공고 수백 건 × 쌍 조합이라 느리고
비싸며, 무엇보다 매번 결과가 흔들려 재현이 안 된다. 확실한 건 코드가 정하고 애매한
것만 물어보면 빠르면서도 정확도를 올릴 수 있다.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache

from agent.schemas import Notice, NoticeCluster
from tools import hyperclova_api

# 두 임계값 사이가 'LLM에게 물어보는 구간'이다. data/golden_dupe.csv로 튜닝했다
# (python score.py 로 언제든 다시 잰다).
#
# 하한을 넉넉히(0.55) 잡은 건 의도적이다. 이 구간에 들어온 쌍은 자동으로 합쳐지는 게
# 아니라 LLM에게 한 번 더 물어보는 것뿐이라, 애매한 쌍을 많이 넣어도 안전하다.
# 반대로 놓친 중복은 되돌릴 방법이 없다 — 담당자가 같은 공고를 두 번 검토하게 된다.
SAME_THRESHOLD = 0.85       # 이 이상이면 코드가 '같다'로 확정
DIFF_THRESHOLD = 0.55       # 이 미만이면 코드가 '다르다'로 확정
DATE_WINDOW_DAYS = 3        # 마감일이 이 일수 안이면 후보로 본다

# 한 번 수집에서 LLM에 물어볼 쌍의 최대 개수. 공고가 수백 건이면 경계 쌍도 늘어나
# 수집이 하염없이 느려질 수 있다. 점수가 높은(= 같을 가능성이 큰) 쌍부터 쓴다.
MAX_LLM_PAIRS = 40

# 제목에서 걷어낼 상용어 — 이런 단어는 어느 공고에나 붙어서 유사도를 부풀린다.
# 순서가 중요하다 — 긴 것부터 지워야 "지원사업"이 "지원"+"사업"으로 쪼개지지 않는다.
_STOPWORDS = [
    "참여기업모집", "참가기업모집", "모집공고", "모집안내", "재공고", "추가모집",
    "참여기업", "참가기업", "선정계획", "지원사업", "공고", "안내", "모집", "공모",
    "사업", "지원", "신청", "접수", "선정", "계획", "대상",
]
_BRACKET_RE = re.compile(r"[\[\(【「<]{1}[^\]\)】」>]*[\]\)】」>]{1}")
_YEAR_RE = re.compile(r"20\d{2}\s*년?도?")
_ROUND_RE = re.compile(r"제?\s*\d+\s*(차|회|기|분기)")
_NONWORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")

# 기관명 표기 흔들림 흡수 ("(재)광주테크노파크" ↔ "광주테크노파크")
_AGENCY_NOISE_RE = re.compile(r"\(재\)|\(사\)|\(주\)|재단법인|사단법인|주식회사|\s+")


@lru_cache(maxsize=8192)
def normalize_title(title: str) -> str:
    """제목에서 '사업의 알맹이'만 남긴다.

    괄호 안 부가정보 → 연도 → 차수 → 기호·공백 → 상용어 순으로 지운다.

    공백을 상용어보다 **먼저** 지우는 게 요령이다. 기관마다 띄어쓰기가 달라서
    ("참여기업 모집" vs "참여기업모집") 공백이 남아 있으면 같은 상용어를 못 지운다.
    """
    s = _BRACKET_RE.sub(" ", title or "")
    s = _YEAR_RE.sub(" ", s)
    s = _ROUND_RE.sub(" ", s)
    s = _NONWORD_RE.sub("", s)      # 공백·기호 제거 → 붙여쓰기 형태로 통일
    for w in _STOPWORDS:
        s = s.replace(w, "")
    return s


def normalize_agency(agency: str) -> str:
    return _AGENCY_NOISE_RE.sub("", agency or "")


@lru_cache(maxsize=8192)
def _tokens(title: str) -> frozenset[str]:
    """정규화된 제목의 2글자 조각(bigram) 집합.

    공고가 수백~수천 건이면 쌍 비교가 수십만 번이라, 같은 제목을 매번 다시 정규화하면
    수집이 눈에 띄게 느려진다. 제목 문자열을 키로 캐시한다(그래서 반환형이 frozenset).

    한글은 기관마다 띄어쓰기가 달라서("스마트공장" vs "스마트 공장") 어절로 자르면
    같은 말이 다른 토큰이 된다. 공백을 없앤 뒤 2글자씩 겹쳐 자르면 이 문제가 사라진다.

    어절 토큰을 섞지 않는 이유: 상용어가 붙은 긴 제목이 토큰 수만 부풀려서
    containment 분모를 키우고, 정작 같은 사업의 점수를 깎아 먹는다.
    """
    norm = normalize_title(title)
    return frozenset(norm[i:i + 2] for i in range(len(norm) - 1))


def similarity(a: Notice, b: Notice) -> float:
    """0~1 결합 유사도. 세 가지 관점을 섞는다.

    seq(문자열)   — "수출바우처" ↔ "수출바우처사업" 같은 한두 글자 차이를 잡는다
    jaccard(집합) — 어순이 다른 같은 사업을 잡는다
    containment   — **한쪽 제목이 다른 쪽을 통째로 품고 있는 경우**를 잡는다

    containment의 비중이 가장 큰 이유가 이 문제의 핵심이다. 기관 간 교차 게시는
    보통 한쪽이 더 길다:
        기업마당  "광주광역시 중소기업 스마트공장 구축지원사업"
        K-Startup "중소기업 스마트공장 구축 지원 참여기업 모집"
    이럴 때 자카드는 합집합이 커져 점수가 깎이지만, "짧은 쪽이 긴 쪽에 얼마나
    들어 있나"를 보는 containment는 제대로 높게 나온다.
    """
    na, nb = normalize_title(a.title), normalize_title(b.title)
    if not na or not nb:
        return 0.0
    seq = SequenceMatcher(None, na, nb).ratio()

    ta, tb = _tokens(a.title), _tokens(b.title)
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb) if (ta | tb) else 0.0
    containment = inter / min(len(ta), len(tb)) if ta and tb else 0.0

    score = 0.25 * seq + 0.20 * jaccard + 0.55 * containment

    # 소관기관이 같으면 같은 사업일 확률이 크게 오른다 (가산점, 상한 1.0).
    if a.agency and b.agency and normalize_agency(a.agency) == normalize_agency(b.agency):
        score = min(1.0, score + 0.05)
    return score


def _dates_close(a: Notice, b: Notice) -> bool:
    if a.apply_end is None or b.apply_end is None:
        return True         # 한쪽이라도 마감일 미상이면 날짜로 거르지 않는다
    return abs((a.apply_end - b.apply_end).days) <= DATE_WINDOW_DAYS


def _is_candidate(a: Notice, b: Notice) -> bool:
    """블로킹 — 비교할 가치가 있는 쌍만 통과시킨다.

    같은 기관에서 온 공고끼리는 비교하지 않는다. 한 기관 안에서 비슷한 이름의
    별개 사업(1차/2차, 분야별 공고)이 정상적으로 여러 개 존재하기 때문이다.
    우리가 잡으려는 건 '기관 간 중복 게시'다.
    """
    if a.source == b.source:
        return False
    if not _dates_close(a, b):
        return False
    return bool(_tokens(a.title) & _tokens(b.title))


_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "same": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["same", "reason"],
}

_LLM_SYSTEM = (
    "너는 정부지원사업 공고를 정리하는 담당자다. 두 공고가 '같은 사업을 두 기관이 "
    "각각 게시한 것'인지 판단하라. 다음은 같은 사업으로 본다: 제목 표기만 다르고 "
    "주관기관·지원내용·모집기간이 사실상 동일한 경우. 다음은 다른 사업으로 본다: "
    "같은 사업의 서로 다른 차수(1차/2차)나 분야(기술/수출)로 나뉜 별개 공고, "
    "이름만 비슷한 다른 기관의 사업. 확신이 서지 않으면 same을 false로 하라 — "
    "서로 다른 공고를 잘못 합치면 담당자가 지원 기회를 통째로 놓친다. "
    "reason은 한 문장으로 짧게 쓴다."
)


def _ask_llm(a: Notice, b: Notice) -> tuple[bool, str]:
    """경계 구간 전용 — 하이퍼클로바X에 이진 판정을 맡긴다.

    키가 없거나 호출이 실패하면 '다르다'로 처리한다(보수적 폴백). 합치는 실수가
    나누는 실수보다 훨씬 비싸기 때문이다.
    """
    if not hyperclova_api.is_configured():
        return False, "LLM 미설정 — 보수적으로 분리"

    def brief(n: Notice) -> str:
        return (f"제목: {n.title}\n기관: {n.agency or '미상'}\n"
                f"분야: {n.support_field or '미상'}\n"
                f"접수마감: {n.apply_end.isoformat() if n.apply_end else '미상'}\n"
                f"개요: {(n.summary or '')[:300]}")

    user = f"[공고 A — 출처 {a.source}]\n{brief(a)}\n\n[공고 B — 출처 {b.source}]\n{brief(b)}"
    try:
        data = hyperclova_api.chat_structured(_LLM_SYSTEM, user, _LLM_SCHEMA,
                                              max_tokens=200)
        return bool(data.get("same")), str(data.get("reason", ""))[:200]
    except Exception as e:
        print(f"[알림] 중복 판정 LLM 호출 실패 — 보수적으로 분리했습니다. ({e})")
        return False, "LLM 호출 실패 — 보수적으로 분리"


class _UnionFind:
    """같다고 판단된 공고들을 묶는 표준 자료구조.

    A=B, B=C 가 각각 발견되면 A·B·C가 한 덩어리여야 한다. 쌍 단위 판정 결과를
    덩어리로 모으는 데 이만한 게 없다.
    """

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_clusters(notices: list[Notice], use_llm: bool = True) -> list[NoticeCluster]:
    """공고 목록 → 중복 통합된 클러스터 목록.

    대표(representative)는 기업마당 공고를 우선한다. 기업마당이 소관기관명과
    제출서류(reqstMthPapersCn)를 더 충실히 주기 때문에, 이후 자격 판정·서류 점검의
    재료가 풍부해진다. 같은 출처끼리면 마감일이 있는 쪽을 고른다.
    """
    uf = _UnionFind()
    reasons: dict[tuple[str, str], str] = {}
    borderline: list[tuple[float, Notice, Notice]] = []

    # 1단계 — 점수만으로 확정되는 쌍을 먼저 처리하고, 경계 쌍은 모아만 둔다.
    for i, a in enumerate(notices):
        for b in notices[i + 1:]:
            if not _is_candidate(a, b):
                continue
            score = similarity(a, b)
            if score >= SAME_THRESHOLD:
                uf.union(a.id, b.id)
                reasons[(a.id, b.id)] = f"제목 유사도 {score:.2f} (자동 통합)"
            elif score >= DIFF_THRESHOLD:
                borderline.append((score, a, b))

    # 2단계 — 경계 쌍만 LLM에 물어본다. 점수가 높은 쌍부터, 정해진 개수까지만.
    if use_llm and borderline:
        borderline.sort(key=lambda t: -t[0])
        for score, a, b in borderline[:MAX_LLM_PAIRS]:
            if uf.find(a.id) == uf.find(b.id):
                continue        # 다른 쌍을 거쳐 이미 같은 덩어리에 들어왔다
            same, why = _ask_llm(a, b)
            if same:
                uf.union(a.id, b.id)
                reasons[(a.id, b.id)] = f"유사도 {score:.2f} · AI 판단: {why}"

    groups: dict[str, list[Notice]] = {}
    for n in notices:
        groups.setdefault(uf.find(n.id), []).append(n)

    clusters: list[NoticeCluster] = []
    for root, members in groups.items():
        rep = min(members, key=lambda m: (m.source != "기업마당",
                                          m.apply_end is None,
                                          m.id))
        reason = ""
        if len(members) > 1:
            ids = {m.id for m in members}
            for (x, y), why in reasons.items():
                if x in ids and y in ids:
                    reason = why
                    break
        clusters.append(NoticeCluster(cluster_id=root, representative=rep,
                                      members=members, reason=reason))

    # 통합된 것(2건 이상)을 앞에 두면 로그·디버깅에서 확인하기 쉽다.
    clusters.sort(key=lambda c: (-len(c.members), c.representative.title))
    return clusters

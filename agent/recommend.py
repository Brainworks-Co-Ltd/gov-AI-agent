"""회사 프로필에 맞는 공고를 골라 위로 올린다.

공고가 수백 건이면 마감 임박순 목록만으로는 부족하다. 담당자가 위에서부터 훑다가
지치고, 정작 우리 회사에 딱 맞는 공고는 아래쪽에 묻힌다.

**규칙으로 점수를 매긴다.** 공고마다 LLM을 부르면 673건에 수십 분이 걸리고 매번
순위가 흔들린다. 지역·업종·규모·마감은 코드로 비교할 수 있는 것들이라, 자격 판정과
같은 분업을 그대로 따른다 — 판단은 코드가, 재현 가능하게.

**왜 추천했는지 반드시 같이 보여준다.** 근거 없는 추천 목록은 담당자가 한 번 훑고
무시한다. 점수 대신 '광주 소재 기업 대상 · 제조업 대상 · D-12' 처럼 사람이 읽을 수
있는 이유를 함께 돌려준다.
"""
from __future__ import annotations

import re

from agent.eligibility import (_NON_MANUFACTURING, _districts_in, _is_manufacturer,
                               _regions_in)
from agent.schemas import (VERDICT_CHECK, VERDICT_NO, VERDICT_OK, CompanyProfile,
                           Notice)

# 제조 중소기업에 실제로 쓸모가 있는 지원 내용. 공고 제목·개요에서 찾는다.
_USEFUL_TOPICS = [
    (("스마트공장", "스마트제조", "제조혁신", "공정개선", "자동화", "생산성"), 22, "공정·설비"),
    (("기술개발", "R&D", "연구개발", "시제품", "기술사업화"), 18, "기술개발"),
    (("수출", "해외진출", "해외마케팅", "무역", "바우처"), 16, "수출"),
    (("자금", "융자", "정책자금", "운전자금", "보증"), 14, "자금"),
    (("인력", "채용", "고용", "일자리"), 12, "인력"),
    (("시험", "인증", "규격", "성적서", "시험분석"), 12, "시험·인증"),
    (("컨설팅", "진단", "멘토링"), 8, "컨설팅"),
    (("판로", "마케팅", "전시회", "박람회"), 8, "판로"),
]

# 우리 회사를 콕 집는 말 (프로필의 업종·주생산품에서 뽑아 쓴다).
_TOKEN_RE = re.compile(r"[가-힣A-Za-z]{2,}")
_STOP = {"기업", "회사", "사업", "지원", "제조", "생산", "주식", "제조업"}


def _profile_keywords(profile: CompanyProfile) -> set[str]:
    """회사를 특징짓는 낱말. '제조' 같은 흔한 말은 빼야 변별력이 생긴다.

    **세 글자 이상만 쓴다.** 한글은 단어 경계가 없어서 두 글자짜리는 엉뚱한 곳에
    걸린다 — 'SMT 라인'에서 뽑은 '라인'이 '**온라인** 접수'에 걸려, 전혀 무관한 공고가
    '라인 관련'이라며 추천 상단에 올라왔다.
    """
    text = " ".join([profile.industry or "", profile.tech_services or "",
                     " ".join(str(v) for v in (profile.extra or {}).values())])
    return {w for w in _TOKEN_RE.findall(text) if w not in _STOP and len(w) >= 3}


def score(notice: Notice, profile: CompanyProfile,
          verdict: str | None = None) -> tuple[int, list[str]]:
    """공고 하나의 추천 점수와 근거. 점수가 음수면 추천하지 않는다."""
    text = notice.full_text()
    title_area = f"{notice.title} {notice.support_field} {notice.summary}"
    points = 0
    reasons: list[str] = []

    # ── 지역 — 가장 강한 신호. 다른 지역 전용 공고는 아예 걸러야 한다.
    ours = _regions_in(profile.region)
    theirs = _regions_in(f"{notice.title} {notice.region_text} {notice.agency}")
    if theirs and "*" not in theirs:
        if ours & theirs:
            points += 30
            reasons.append(f"{profile.region} 소재 기업 대상")
        else:
            return -100, [f"다른 지역({', '.join(sorted(theirs))}) 대상"]
    elif "*" in theirs:
        points += 12
        reasons.append("전국 대상")

    # 시·군·구까지 지정된 공고 (예: 금천구) — 우리와 다르면 제외.
    wanted_districts = _districts_in(f"{notice.title} {notice.region_text}")
    if wanted_districts and not (wanted_districts
                                 & _districts_in(f"{profile.region} {profile.region_detail}")):
        return -100, [f"다른 시·군·구({', '.join(sorted(wanted_districts))}) 대상"]

    # ── 업종 — 제조업체가 쓸 수 없는 게 분명한 공고는 제외.
    if _is_manufacturer(profile):
        blocked = next((w for w in _NON_MANUFACTURING if w in text), None)
        if blocked:
            return -100, [f"'{blocked}' 대상 사업"]
        if "제조" in text:
            points += 20
            reasons.append("제조업 대상")

    # 우리 업종·주생산품을 콕 집는 말이 있으면 가점 (전자부품, ECU 등).
    hit = sorted(w for w in _profile_keywords(profile) if w in text)
    if hit:
        points += min(20, 8 * len(hit))
        reasons.append(f"‘{hit[0]}’ 관련")

    # ── 지원 내용 — 제조 중소기업에 쓸모가 있는 분야인가.
    for words, weight, label in _USEFUL_TOPICS:
        if any(w in title_area for w in words):
            points += weight
            reasons.append(label)
            break

    # ── 규모
    if profile.company_type and profile.company_type in text:
        points += 8

    # ── 마감 — 준비할 시간이 있어야 추천할 값어치가 있다.
    d = notice.d_day
    if d is None:
        points += 2
    elif d < 0:
        return -100, ["마감된 공고"]
    elif d <= 2:
        points -= 8
        reasons.append(f"D-{d} (준비 시간 촉박)")
    elif d <= 30:
        points += 14
        reasons.append(f"D-{d}")
    else:
        points += 6
        reasons.append(f"D-{d}")

    # ── 이미 판정한 공고는 그 결과를 그대로 반영한다.
    if verdict == VERDICT_OK:
        points += 40
        reasons.insert(0, "신청 가능으로 판정됨")
    elif verdict == VERDICT_NO:
        return -100, ["신청 불가로 판정됨"]
    elif verdict == VERDICT_CHECK:
        points += 8

    return points, reasons


def top(notices: list[Notice], profile: CompanyProfile,
        verdicts: dict[str, str] | None = None, limit: int = 5) -> list[dict]:
    """추천 공고 상위 몇 건. 반환: [{notice_id, score, reasons}]

    점수가 같으면 마감이 급한 쪽을 먼저 올린다 — 같은 값어치라면 놓치기 쉬운 것부터
    봐야 한다.
    """
    verdicts = verdicts or {}
    scored: list[tuple[int, int, Notice, list[str]]] = []
    for n in notices:
        points, reasons = score(n, profile, verdicts.get(n.id))
        if points <= 0:
            continue
        scored.append((points, -(n.d_day if n.d_day is not None else 9999), n, reasons))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [{"notice_id": n.id, "score": p, "reasons": reasons}
            for p, _, n, reasons in scored[:limit]]

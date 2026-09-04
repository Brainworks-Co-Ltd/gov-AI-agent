"""회사 프로필 읽기/쓰기 + 해시.

프로필은 자격 판정의 '정답지 절반'이다(나머지 절반은 공고문). 그래서 담당자가
업력이나 인원 수를 고치면 이전 판정 결과는 전부 무효가 되어야 한다. 그걸 자동으로
처리하려고 프로필 내용의 해시를 판정 캐시와 함께 저장한다(tools/store.py 참고).
"""
from __future__ import annotations

import hashlib
import json
import os

from agent.schemas import CompanyProfile

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
PROFILE_PATH = os.path.join(_DATA_DIR, "company_profile.json")


def load() -> CompanyProfile:
    """프로필을 읽는다. 파일이 없으면 빈 프로필(전부 '확인필요'로 판정됨)."""
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return CompanyProfile.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return CompanyProfile()


def save(profile: CompanyProfile) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    data = {k: v for k, v in profile.to_dict().items() if k != "years"}  # years는 파생값
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 판정이 읽지 않는 항목. 초안을 쓸 때만 쓰인다(agent/eligibility.py 에 참조 0곳).
# 지문에 넣어 두면 회사 강점 문구 한 글자만 고쳐도 판정 수백 건이 통째로 날아가,
# LLM 을 30분씩 다시 부르게 된다.
_DRAFT_ONLY = ("name", "ceo", "biz_no", "strengths", "tech_services", "extra")


def hash_of(profile: CompanyProfile) -> str:
    """판정 캐시 무효화용 지문.

    years는 오늘 날짜에 따라 매일 바뀌므로 해시에서 뺀다 — 안 그러면 날짜만 넘어가도
    캐시가 전부 날아가 LLM을 다시 부르게 된다. 업력 요건은 founded로 계산되므로
    founded가 해시에 들어 있으면 충분하다.
    """
    data = {k: v for k, v in profile.to_dict().items()
            if k != "years" and k not in _DRAFT_ONLY}
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

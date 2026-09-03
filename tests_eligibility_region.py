"""지역 요건 판정 회귀 테스트.

'원주센터 관할(원주시·횡성군·영월군) 소재' 처럼 시·도 없이 시·군·구만 적힌 요건을
광주 기업이 봤을 때 '불가'가 나와야 한다. _effective_axis 가 시·도만 보고 축을
'기타'로 낮추면 "프로필에 대응하는 항목이 없어 확인 필요"라는 엉뚱한 답이 나갔다.

    python tests_eligibility_region.py
"""
from agent import eligibility as E
from agent.schemas import Requirement, AXIS_REGION
from tools import profile_store


def _profile(sido: str, gu: str):
    p = profile_store.load()
    p.region, p.region_detail = sido, gu
    return p


def demo() -> None:
    # 시·군·구만 적힌 요건 — 판정기가 다룰 수 있으므로 '기타'로 낮추지 않는다.
    gu_only = Requirement(
        axis=AXIS_REGION, operator="포함", value="원주시 · 횡성군 · 영월군",
        quote="공고일 기준 업력 1년 이상 원주센터 관할(원주시·횡성군·영월군) 소재 사업장을 운영 중인")
    assert E._effective_axis(gu_only) == AXIS_REGION
    assert E.judge(gu_only, _profile("광주광역시", "광산구")).verdict == "불가"
    assert E.judge(gu_only, _profile("강원도", "원주시")).verdict == "가능"

    # 시·도가 적힌 요건은 원래대로.
    sido = Requirement(axis=AXIS_REGION, operator="포함", value="광주",
                       quote="광주광역시 소재 중소기업")
    assert E.judge(sido, _profile("광주광역시", "북구")).verdict == "가능"
    assert E.judge(sido, _profile("서울특별시", "금천구")).verdict == "불가"

    # 지역 신호가 전혀 없으면 여전히 '기타'로 낮춘다 — 지역 판정기가
    # "대상 지역을 특정하지 못했습니다" 같은 엉뚱한 이유를 내지 않게 한다.
    not_region = Requirement(axis=AXIS_REGION, operator="제외", value="유흥주점업",
                             quote="일반유흥주점업 등 창업에서 제외되는 업종")
    assert E._effective_axis(not_region) == "기타"

    print("모두 통과")


if __name__ == "__main__":
    demo()

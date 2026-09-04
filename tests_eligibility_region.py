"""자격 요건 판정 회귀 테스트.

세 가지를 본다.
  ① 시·도 없이 시·군·구만 적힌 요건을 광주 기업이 보면 '불가'가 나오는가
     (_effective_axis 가 시·도만 보고 축을 '기타'로 낮추면 엉뚱한 답이 나갔다)
  ② 추출이 소재지 요건을 통째로 흘렸을 때 규칙이 그것을 알아채는가
     (_region_duty_sentences — LLM 을 부르지 않는 결정적 부분만 검사한다)
  ③ 쉼표로 나열된 선택지를 하나의 범위로 묶어 '불가'로 만들지 않는가
     (_satisfies — 기업마당 biz_enyy 가 이 모양으로 온다)

    python -X utf8 tests_eligibility_region.py
"""
from agent import eligibility as E
from agent.schemas import Requirement, AXIS_REGION
from tools import profile_store


def _profile(sido: str, gu: str):
    p = profile_store.load()
    p.region, p.region_detail = sido, gu
    return p


def test_axis_not_downgraded_when_only_districts() -> None:
    """시·군·구만 있는 요건도 지역 판정기가 받아야 한다."""
    gu_only = Requirement(
        axis=AXIS_REGION, operator="포함", value="원주시 · 횡성군 · 영월군",
        quote="공고일 기준 업력 1년 이상 원주센터 관할(원주시·횡성군·영월군) 소재 사업장을 운영 중인")
    assert E._effective_axis(gu_only) == AXIS_REGION
    assert E.judge(gu_only, _profile("광주광역시", "광산구")).verdict == "불가"
    # 역방향 대조 — 전부 '불가'로 만들어 버린 게 아니어야 한다.
    assert E.judge(gu_only, _profile("강원도", "원주시")).verdict == "가능"

    sido = Requirement(axis=AXIS_REGION, operator="포함", value="광주",
                       quote="광주광역시 소재 중소기업")
    assert E.judge(sido, _profile("광주광역시", "북구")).verdict == "가능"
    assert E.judge(sido, _profile("서울특별시", "금천구")).verdict == "불가"

    # 지역 신호가 전혀 없으면 여전히 '기타'로 낮춘다 — 지역 판정기가
    # "대상 지역을 특정하지 못했습니다" 같은 엉뚱한 이유를 내지 않게 한다.
    not_region = Requirement(axis=AXIS_REGION, operator="제외", value="유흥주점업",
                             quote="일반유흥주점업 등 창업에서 제외되는 업종")
    assert E._effective_axis(not_region) == "기타"


def test_region_duty_sentences() -> None:
    """소재지를 '요구하는' 문장만 골라야 한다."""
    hit = E._region_duty_sentences(
        "공고일 기준 업력 1년 이상 원주센터 관할(원주시·횡성군·영월군) 소재 사업장을 운영 중인 소상공인")
    assert hit and "원주시" in hit[0]
    assert E._region_duty_sentences("화성시 관내 제조업 기업")
    assert E._region_duty_sentences("본점 소재지가 경북이면서 전시품목에 적합한 기업")

    # '소재'가 재료라는 뜻일 때. 목적격 조사가 붙으면 장소가 아니다.
    # 이걸 못 거르면 (서울)RISE사업단 공고가 '서울 소재' 요건으로 둔갑해,
    # 광주 기업에게 신청 가능한 공고가 '불가'로 숨는다.
    assert not E._region_duty_sentences(
        "경희대학교 (서울)RISE사업단은 혁신적인 기술 창업 소재를 보유한 예비 창업자를 육성한다")
    # 소재지 의무 표현이 없으면 지역 이름이 있어도 요건이 아니다.
    assert not E._region_duty_sentences("광주테크노파크가 주관하는 사업입니다")
    # 지역 이름이 없으면 소재 표현이 있어도 요건으로 세울 수 없다.
    assert not E._region_duty_sentences("본사를 두고 3년 이상 영업 중인 기업")


def test_쉼표_나열은_OR로_본다() -> None:
    """기업마당 biz_enyy 는 '신청 가능 업력' 선택지를 이어 붙여 준다.

    '이 중 아무거나'라는 뜻인데 한 범위로 묶어 AND 로 보면 첫 항에서 걸려,
    신청할 수 있는 공고가 '불가'가 된다. 저장된 판정 93행 중 14행이 이 오류였고
    전부 '불가 → 가능' 방향이었다.
    """
    업력 = 2.3
    # 선택지 목록 — 2.3년은 '3년미만'에 해당하므로 가능
    assert E._satisfies(업력, "1년미만,2년미만,3년미만,5년미만,7년미만,10년미만") is True
    assert E._satisfies(업력, "예비창업자, 1년 미만, 2년 미만, 3년 미만") is True
    # 어느 선택지에도 안 맞으면 그대로 불가
    assert E._satisfies(업력, "1년미만,2년미만") is False
    # 쉼표가 없는 진짜 범위는 AND 그대로 — 2.3년은 '3년 초과'가 아니다
    assert E._satisfies(업력, "3년 초과 7년 이내") is False
    assert E._satisfies(업력, "창업 7년 이내") is True
    # 읽어낼 경계가 없으면 판정 불가(None)이지 '불가'가 아니다
    assert E._satisfies(업력, "중소기업") is None


def demo() -> None:
    test_axis_not_downgraded_when_only_districts()
    test_region_duty_sentences()
    test_쉼표_나열은_OR로_본다()
    print("모두 통과")


if __name__ == "__main__":
    demo()

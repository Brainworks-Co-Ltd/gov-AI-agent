"""더미 데이터 생성기 — 오프라인에서도 전 과정을 시연·채점할 수 있게 한다.

문제 Pool이 "더미데이터(회사 프로필·공고문 캐시·과거 신청서)"를 명시적으로 허용하고
있고, 실제로도 필요하다:
  · 인증키가 없거나 현장 네트워크가 끊겨도 데모가 돌아야 한다
  · 중복 제거 성능을 측정하려면 '정답을 아는' 중복쌍이 있어야 한다
  · 자격 판정 정확도를 재려면 사람이 라벨링한 정답셋이 있어야 한다

여기서 만드는 공고는 실존 공고가 아니라, 두 기관 교차 게시·표기 흔들림·요건 유형을
재현하려고 지어낸 연습용 데이터다. 실제 수집(python -m tools.ingest)이 성공하면
notices_cache.json은 진짜 데이터로 덮어써진다.

실행: python -m tools.seed_dummy
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, timedelta

from agent.schemas import Notice
from tools import ingest

_ROOT = os.path.dirname(os.path.dirname(__file__))
_DATA_DIR = os.path.join(_ROOT, "data")
_PAST_DIR = os.path.join(_DATA_DIR, "past_applications")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _d(days: int) -> str:
    """오늘로부터 n일 뒤 (D-day가 항상 그럴듯하게 보이도록 상대 날짜로 만든다)."""
    return (date.today() + timedelta(days=days)).isoformat()


# 우리 회사 — 문제 Pool의 제공 기업(전자부품 제조업 / 10인 미만 / 광주)을 따랐다.
PROFILE = {
    "name": "한빛정밀전자(주)",
    "biz_no": "410-81-00000",
    "industry": "전자부품 제조업",
    "ksic": "C262",
    "founded": "2019-03-11",
    "region": "광주광역시",
    "region_detail": "광산구",
    "employees": 8,
    "revenue_krw": 1_850_000_000,
    "company_type": "중소기업",
    "ceo": "김하늘",
    "docs_on_hand": [
        "사업자등록증", "법인등기부등본", "국세납세증명서", "지방세납세증명서",
        "중소기업확인서", "재무제표(직전 2개년)", "4대보험 가입자명부",
    ],
    "strengths": (
        "차량용 전자제어 모듈의 정밀 SMT 실장과 인라인 검사 공정을 자체 보유하고 있으며, "
        "다품종 소량 생산에서도 불량률을 낮게 유지해 온 것이 강점이다. 다만 생산 데이터가 "
        "수기로 관리되고 있어 공정 개선의 근거를 확보하기 어렵다."
    ),
    # 공고가 반복해서 묻는 확인 항목 — 여기 값이 있으면 판정이 '확인필요'에서
    # 확정으로 바뀐다. 모르는 항목은 None으로 두면 정직하게 '확인필요'가 된다.
    "tax_arrears": False,       # 국세·지방세 체납 없음
    "closed": False,            # 휴·폐업 아님
    "recent_layoffs": False,    # 최근 1년 이내 고용조정 없음
    "root_tech": False,         # 뿌리기술 활용 기업 아님
    "women_owned": False,       # 여성기업 확인서 미보유
    "smart_factory": False,     # 스마트공장 미구축
    "export_usd": 0,            # 직전연도 수출 실적 없음
    "prior_support": [],        # 최근 3년 정부지원사업 수혜 이력 없음
    "extra": {"주생산품": "차량용 전자제어 모듈(ECU) PCB 어셈블리",
              "설비": "SMT 라인 2기, AOI 검사기 1기"},
}


# (공고, 클러스터 라벨) — 라벨이 같으면 '같은 사업'이라는 정답이다.
# 교차 게시된 쌍은 일부러 제목 표기를 흔들어 두었다. 중복 제거가 이걸 잡아야 한다.
_NOTICES: list[tuple[dict, str]] = [
    # ── 중복쌍 1: 스마트공장 (제목 표기 차이 + 괄호 차수)
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0001",
        "title": "2026년 광주광역시 중소기업 스마트공장 구축지원사업 모집공고(1차)",
        "agency": "광주광역시", "support_field": "기술",
        "summary": "지역 중소 제조기업의 생산 공정에 MES 등 스마트시스템을 도입하는 비용을 "
                   "지원한다. 기업당 최대 5천만원, 총 사업비의 50% 이내로 지원한다.",
        "target_text": "광주광역시에 사업장을 둔 중소 제조기업. 상시근로자 5인 이상 100인 "
                       "미만인 기업에 한한다. 스마트공장 미구축 기업을 우선 지원한다.",
        "docs_text": "신청서, 사업계획서, 사업자등록증, 중소기업확인서, 재무제표(직전 2개년), "
                     "국세 및 지방세 납세증명서",
        "apply_begin": _d(-6), "apply_end": _d(12),
        "url": "https://www.bizinfo.go.kr/dummy/0001",
    }, "C-스마트공장"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9001",
        "title": "[광주] 중소기업 스마트공장 구축 지원 참여기업 모집",
        "agency": "광주광역시", "support_field": "기술·사업화",
        "summary": "광주 소재 중소 제조기업을 대상으로 MES·POP 등 스마트시스템 구축비를 "
                   "지원하는 사업입니다. 기업당 최대 5천만원.",
        "target_text": "광주광역시 소재 중소 제조기업 / 상시근로자 5인 이상",
        "exclude_text": "국세 또는 지방세를 체납 중인 기업, 최근 3년 이내 동일 사업 수혜기업",
        "region_text": "광주",
        "apply_begin": _d(-6), "apply_end": _d(12),
        "url": "https://www.k-startup.go.kr/dummy/9001",
    }, "C-스마트공장"),

    # ── 중복쌍 2: 수출바우처 (기관명 표기 흔들림)
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0002",
        "title": "2026년 수출바우처사업 참여기업 모집공고",
        "agency": "중소벤처기업진흥공단", "support_field": "수출",
        "summary": "수출 초보기업의 해외 마케팅 활동을 바우처 방식으로 지원한다. "
                   "기업당 최대 3천만원.",
        "target_text": "직전연도 수출액 100만불 미만의 중소기업. 업종 제한 없음.",
        "docs_text": "신청서, 수출실적증명원, 사업자등록증, 중소기업확인서",
        "apply_begin": _d(-2), "apply_end": _d(21),
        "url": "https://www.bizinfo.go.kr/dummy/0002",
    }, "C-수출바우처"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9002",
        "title": "2026년도 수출바우처 사업 신규 참여기업 모집 안내",
        "agency": "(재)중소벤처기업진흥공단", "support_field": "판로·해외진출",
        "summary": "수출 초보기업 대상 해외 마케팅 바우처 지원 사업입니다.",
        "target_text": "직전연도 수출액 100만불 미만 중소기업",
        "region_text": "전국",
        "apply_begin": _d(-2), "apply_end": _d(21),
        "url": "https://www.k-startup.go.kr/dummy/9002",
    }, "C-수출바우처"),

    # ── 중복쌍 3: 뿌리기업 (마감일 하루 차이 — 날짜 블로킹 허용폭 검증용)
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0003",
        "title": "2026년 뿌리기업 공정개선 지원사업 공고",
        "agency": "한국산업기술진흥원", "support_field": "기술",
        "summary": "뿌리기술 활용 중소기업의 공정 개선과 자동화 도입을 지원한다.",
        "target_text": "뿌리기술을 활용하는 중소 제조기업. 업력 3년 이상.",
        "docs_text": "신청서, 사업계획서, 뿌리기업 확인서, 사업자등록증, 재무제표",
        "apply_begin": _d(0), "apply_end": _d(30),
        "url": "https://www.bizinfo.go.kr/dummy/0003",
    }, "C-뿌리기업"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9003",
        "title": "뿌리기업 공정개선 지원 사업 참여기업 모집(2026)",
        "agency": "한국산업기술진흥원", "support_field": "기술·사업화",
        "summary": "뿌리기술 중소기업 공정개선·자동화 지원.",
        "target_text": "뿌리기술 활용 중소 제조기업 / 업력 3년 이상",
        "region_text": "전국",
        "apply_begin": _d(0), "apply_end": _d(31),
        "url": "https://www.k-startup.go.kr/dummy/9003",
    }, "C-뿌리기업"),

    # ── 중복쌍 4: 청년채용 (제목 어순이 크게 다름 — LLM 경계 판정 유도)
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0004",
        "title": "중소기업 청년 신규채용 인건비 지원사업 모집",
        "agency": "고용노동부", "support_field": "인력",
        "summary": "만 34세 이하 청년을 정규직으로 신규 채용하는 중소기업에 "
                   "1인당 연 최대 1,200만원의 인건비를 지원한다.",
        "target_text": "상시근로자 5인 이상 중소기업. 최근 1년 이내 고용조정이 없는 기업.",
        "docs_text": "신청서, 사업자등록증, 4대보험 가입자명부, 고용보험 피보험자 현황",
        "apply_begin": _d(-10), "apply_end": _d(5),
        "url": "https://www.bizinfo.go.kr/dummy/0004",
    }, "C-청년채용"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9004",
        "title": "청년 정규직 신규채용 기업 인건비 지원 참여기업 모집 안내",
        "agency": "고용노동부", "support_field": "인력·고용",
        "summary": "청년 정규직 신규 채용 중소기업 인건비 지원.",
        "target_text": "상시근로자 5인 이상 중소기업",
        "region_text": "전국",
        "apply_begin": _d(-10), "apply_end": _d(5),
        "url": "https://www.k-startup.go.kr/dummy/9004",
    }, "C-청년채용"),

    # ── 단독 공고들 (중복 아님) ─────────────────────────────────────────
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0005",
        "title": "2026년 초기창업패키지 창업기업 모집공고",
        "agency": "창업진흥원", "support_field": "창업",
        "summary": "창업 3년 이내 기업의 시제품 제작과 마케팅을 지원한다. 최대 1억원.",
        "target_text": "창업 후 3년 이내인 기업. 대표자가 만 39세 이하인 경우 우대.",
        "docs_text": "신청서, 사업계획서, 사업자등록증, 대표자 신분증 사본",
        "apply_begin": _d(-3), "apply_end": _d(9),
        "url": "https://www.bizinfo.go.kr/dummy/0005",
    }, "C-초기창업"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9005",
        "title": "2026년 전남 지역주력산업 육성 R&D 과제 공모",
        "agency": "전남테크노파크", "support_field": "기술·사업화",
        "summary": "전라남도 소재 중소기업의 주력산업 연계 R&D 과제를 지원합니다. "
                   "과제당 최대 2억원, 2년 이내.",
        "target_text": "전라남도에 본사 또는 공장을 둔 중소기업",
        "exclude_text": "타 지역 소재 기업은 신청할 수 없습니다.",
        "region_text": "전남",
        "apply_begin": _d(-1), "apply_end": _d(17),
        "url": "https://www.k-startup.go.kr/dummy/9005",
    }, "C-전남RND"),
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0006",
        "title": "2026년 중소기업 정책자금 융자계획 공고(운전자금)",
        "agency": "중소벤처기업진흥공단", "support_field": "금융",
        "summary": "중소기업의 원부자재 구입 등 경영 안정에 필요한 운전자금을 융자한다.",
        "target_text": "업력 7년 미만 중소기업에 한한다. 휴업 또는 폐업 중인 기업은 제외한다.",
        "docs_text": "신청서, 사업자등록증, 재무제표(직전 2개년), 국세·지방세 납세증명서, "
                     "부가가치세 과세표준증명원",
        "apply_begin": _d(-15), "apply_end": _d(45),
        "url": "https://www.bizinfo.go.kr/dummy/0006",
    }, "C-정책자금"),
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0007",
        "title": "2026년 소상공인 온라인 판로개척 지원사업 공고",
        "agency": "소상공인시장진흥공단", "support_field": "내수",
        "summary": "소상공인의 온라인 쇼핑몰 입점과 콘텐츠 제작을 지원한다.",
        "target_text": "상시근로자 5인 미만의 소상공인. 제조업의 경우 10인 미만.",
        "docs_text": "신청서, 사업자등록증, 소상공인 확인서",
        "apply_begin": _d(-4), "apply_end": _d(3),
        "url": "https://www.bizinfo.go.kr/dummy/0007",
    }, "C-소상공인"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9006",
        "title": "2026년 창업도약패키지 참여기업 모집",
        "agency": "창업진흥원", "support_field": "창업",
        "summary": "창업 3년 초과 7년 이내 도약기 기업의 사업 모델 고도화를 지원합니다.",
        "target_text": "창업 후 3년 초과 7년 이내 기업",
        "exclude_text": "창업 7년을 초과한 기업은 신청 대상이 아닙니다.",
        "region_text": "전국",
        "apply_begin": _d(-8), "apply_end": _d(26),
        "url": "https://www.k-startup.go.kr/dummy/9006",
    }, "C-창업도약"),
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0008",
        "title": "2026년 광주 지역 중소기업 시험분석·인증 지원사업 공고",
        "agency": "광주테크노파크", "support_field": "기술",
        "summary": "광주 소재 중소기업의 제품 시험분석과 국내외 인증 취득 비용을 지원한다. "
                   "기업당 최대 1천만원.",
        "target_text": "광주광역시 소재 중소기업. 업종 제한 없음.",
        "docs_text": "신청서, 사업자등록증, 중소기업확인서, 견적서",
        "apply_begin": _d(-5), "apply_end": _d(7),
        "url": "https://www.bizinfo.go.kr/dummy/0008",
    }, "C-시험인증"),
    ({
        "source": "기업마당", "source_id": "PBLN-DUMMY-0009",
        "title": "2026년 벤처기업 기술개발 역량강화 지원사업 공고",
        "agency": "중소벤처기업부", "support_field": "기술",
        "summary": "벤처기업의 기술개발 인력 양성과 시제품 고도화를 지원한다.",
        "target_text": "벤처기업 확인서를 보유한 중소기업. 업종 제한 없음.",
        "docs_text": "신청서, 벤처기업 확인서, 사업자등록증, 기술개발 계획서",
        "apply_begin": _d(-7), "apply_end": _d(19),
        "url": "https://www.bizinfo.go.kr/dummy/0009",
    }, "C-벤처기술"),
    ({
        "source": "K-Startup", "source_id": "KS-DUMMY-9007",
        "title": "2026년 여성기업 제품 공공구매 상담회 참가기업 모집",
        "agency": "한국여성경제인협회", "support_field": "판로·해외진출",
        "summary": "여성기업 확인서를 보유한 기업의 공공기관 판로 개척을 지원합니다.",
        "target_text": "여성기업 확인서를 보유한 중소기업",
        "region_text": "전국",
        "apply_begin": _d(-2), "apply_end": _d(14),
        "url": "https://www.k-startup.go.kr/dummy/9007",
    }, "C-여성기업"),
]

# 자격 판정 정답셋 — 사람이 회사 프로필과 공고문을 직접 대조해 매긴 라벨.
# 업력은 설립 2019-03-11 기준이므로 2026년 현재 약 7.5년이다.
_ELIGIBILITY_GOLD: list[tuple[str, str, str]] = [
    ("기업마당:PBLN-DUMMY-0001", "가능",
     "광주 소재 제조 중소기업, 상시근로자 8명으로 5~100인 범위 충족. 통합된 K-Startup "
     "공고의 제외대상(체납·최근 3년 수혜)도 해당 없음. 스마트공장 미구축은 우대사항"),
    ("기업마당:PBLN-DUMMY-0002", "가능", "수출액 0달러로 100만불 미만 충족, 업종 제한 없음"),
    ("기업마당:PBLN-DUMMY-0003", "불가", "뿌리기술 활용 기업이 아님"),
    ("기업마당:PBLN-DUMMY-0004", "가능", "상시근로자 8명(5인 이상), 최근 1년 고용조정 없음"),
    ("기업마당:PBLN-DUMMY-0005", "불가", "창업 3년 이내 요건 — 업력 약 7.5년으로 초과"),
    ("K-Startup:KS-DUMMY-9005", "불가", "전남 소재 요건 — 우리 회사는 광주광역시 소재"),
    ("기업마당:PBLN-DUMMY-0006", "불가", "업력 7년 미만 요건 — 약 7.5년으로 초과"),
    ("기업마당:PBLN-DUMMY-0007", "가능", "제조업 10인 미만 요건 — 상시근로자 8명"),
    ("K-Startup:KS-DUMMY-9006", "불가", "창업 7년 이내 요건 — 약 7.5년으로 초과"),
    ("기업마당:PBLN-DUMMY-0008", "가능", "광주 소재 중소기업, 업종 제한 없음"),
    ("기업마당:PBLN-DUMMY-0009", "확인필요",
     "벤처기업 확인서 보유 여부가 프로필에 없는 항목이라 판단 불가"),
    ("K-Startup:KS-DUMMY-9007", "불가", "여성기업 확인서 미보유"),
]

# 과거 신청서 더미 — 초안 생성의 재료. 실제로 담당자가 예전에 쓴 문장을 재활용하는
# 상황을 재현한다. 숫자는 일부러 넣지 않는다(수치는 프로필에서만 채워야 하므로).
_PAST_APPLICATIONS = {
    "2025_스마트공장_신청서.md": """# 2025년 스마트공장 구축지원사업 신청서 (제출본)

## 사업 개요
당사는 차량용 전자제어 모듈(ECU) PCB 어셈블리를 생산하는 전자부품 제조기업이다.
SMT 실장과 인라인 검사 공정을 자체 보유하고 있으며, 다품종 소량 생산 체계에서
고객사별 사양 변경에 신속히 대응해 왔다.

## 추진 배경 및 필요성
생산 실적과 불량 이력이 여전히 수기 대장으로 관리되고 있어, 공정별 불량 원인을
데이터로 추적하기 어렵다. 담당자의 경험에 의존한 개선이 반복되면서 개선 효과를
정량적으로 검증하지 못하는 한계가 있다.

## 추진 계획
1단계로 SMT 라인에 생산 실적 자동 수집 체계를 도입하고, 2단계로 AOI 검사 결과와
연계해 불량 유형별 통계를 확보한다. 3단계로 수집된 데이터를 기반으로 설비별
가동률과 불량률 지표를 관리한다.

## 기대효과
공정 데이터가 축적되면 불량 원인을 설비·자재·작업 조건 단위로 분리해 볼 수 있어,
개선 활동의 우선순위를 근거 있게 정할 수 있다. 고객사 품질 요구에 대한 대응
자료도 즉시 제출할 수 있게 된다.
""",
    "2025_청년채용_신청서.md": """# 2025년 청년 신규채용 인건비 지원사업 신청서 (제출본)

## 사업 개요
당사는 광주광역시 광산구에 소재한 전자부품 제조기업으로, 생산·품질 부문에서
청년 인력의 신규 채용을 계획하고 있다.

## 채용 계획
SMT 공정 운영과 검사 데이터 관리를 담당할 생산기술 인력을 정규직으로 채용한다.
입사 후 3개월간 사내 OJT를 거쳐 라인 운영에 투입한다.

## 고용 유지 방안
표준 작업 지침서를 정비해 신규 인력이 조기에 적응할 수 있도록 하고, 직무 교육
과정을 연 2회 운영해 장기 근속을 유도한다.

## 기대효과
생산 현장의 인력 고령화에 대응하고, 데이터 기반 공정 관리로 전환하는 과정에서
필요한 실무 역량을 내부에 축적할 수 있다.
""",
    "2024_시험인증_신청서.md": """# 2024년 시험분석·인증 지원사업 신청서 (제출본)

## 신청 배경
납품처 요구사양 변경에 따라 차량용 전자제어 모듈의 내환경 시험 성적서가
추가로 필요해졌다. 자체 시험 설비로는 온습도 복합 시험을 수행할 수 없어
외부 공인시험기관 의뢰가 불가피하다.

## 지원 요청 내용
공인시험기관을 통한 내환경 시험(고온·저온·온습도 사이클) 성적서 취득 비용을
지원받고자 한다.

## 기대효과
성적서를 확보하면 기존 납품처의 사양 변경 요구에 대응할 수 있고, 동일 사양을
요구하는 신규 거래처 발굴에도 활용할 수 있다.
""",
}


def _write_profile() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    path = os.path.join(_DATA_DIR, "company_profile.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(PROFILE, f, ensure_ascii=False, indent=2)
    print(f"  · 회사 프로필 → {os.path.relpath(path, _ROOT)}")


def _write_notices() -> list[Notice]:
    """더미 공고를 golden_notices.json 에 쓴다.

    실수집 캐시(notices_cache.json)와 일부러 분리한다. 같은 파일을 쓰면 실제 공고를
    한 번 수집하는 순간 더미가 사라지고, 정답셋만 남아 채점이 불가능해진다.
    """
    notices = [Notice.from_dict(d) for d, _ in _NOTICES]
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(ingest.GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump([n.to_dict() for n in notices], f, ensure_ascii=False, indent=1)
    print(f"  · 더미 공고 {len(notices)}건 → data/golden_notices.json")
    return notices


def _write_dupe_gold() -> None:
    """중복 정답셋 — 모든 (기업마당 × K-Startup) 쌍에 same=1/0 라벨을 붙인다.

    같은 기관 안의 쌍은 애초에 비교 대상이 아니므로(tools/dedupe._is_candidate)
    정답셋에서도 뺀다.
    """
    rows = list(_NOTICES)
    pairs = []
    for i, (a, la) in enumerate(rows):
        for b, lb in rows[i + 1:]:
            if a["source"] == b["source"]:
                continue
            aid = f"{a['source']}:{a['source_id']}"
            bid = f"{b['source']}:{b['source_id']}"
            pairs.append((aid, bid, 1 if la == lb else 0))

    path = os.path.join(_DATA_DIR, "golden_dupe.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["notice_a", "notice_b", "same"])
        w.writerows(pairs)
    positives = sum(p[2] for p in pairs)
    print(f"  · 중복 정답셋 {len(pairs)}쌍(같음 {positives}쌍) → data/golden_dupe.csv")


def _write_eligibility_gold() -> None:
    path = os.path.join(_DATA_DIR, "golden_eligibility.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["notice_id", "verdict", "note"])
        w.writerows(_ELIGIBILITY_GOLD)
    print(f"  · 판정 정답셋 {len(_ELIGIBILITY_GOLD)}건 → data/golden_eligibility.csv")


def _write_past_applications() -> None:
    os.makedirs(_PAST_DIR, exist_ok=True)
    for name, text in _PAST_APPLICATIONS.items():
        with open(os.path.join(_PAST_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    print(f"  · 과거 신청서 {len(_PAST_APPLICATIONS)}건 → data/past_applications/")


def main() -> None:
    print("\n더미 데이터를 만듭니다 (실제 수집이 성공하면 공고는 진짜 데이터로 대체됩니다)\n")
    _write_profile()
    _write_notices()
    _write_dupe_gold()
    _write_eligibility_gold()
    _write_past_applications()
    print("\n완료. 다음: python -m tools.ingest --offline\n")


if __name__ == "__main__":
    main()

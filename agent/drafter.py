"""신청서 초안 생성.

담당자가 건당 2~4시간 쓴다는 그 일을, '확인·수정' 수준으로 줄이는 게 목표다.
백지에서 시작하지 않게 하는 것만으로도 대부분의 시간이 사라진다.

    ① 공고문에서 신청서가 요구하는 항목을 뽑는다 (사업 개요·필요성·추진 계획…)
    ② 항목마다 과거 신청서에서 비슷한 문항을 찾아온다 (tools/past_search.py)
    ③ 공고 내용 + 회사 프로필 + 과거 문장을 근거로 초안을 쓴다

**숫자는 AI가 만들지 않는다.**
이 모듈에서 가장 중요한 규칙이다. 매출액·상시근로자 수·설립연도 같은 값을 LLM이
그럴듯하게 지어내면, 제출 직전까지 아무도 못 알아챈다. 그래서 프롬프트에서 숫자를
쓸 자리에는 {{매출액}} 같은 자리표시자만 넣게 하고, 생성이 끝난 뒤 파이썬이 회사
프로필의 실제 값으로 바꿔 넣는다. 프로필에 없는 값은 [확인 필요: …] 로 남겨
담당자가 직접 채우게 한다 — 비어 있는 건 눈에 띄지만, 틀린 숫자는 안 띈다.
"""
from __future__ import annotations

import re
import time

from agent.schemas import CompanyProfile, Draft, DraftSection, Notice
from tools import hyperclova_api, past_search

# 공고문이 항목을 따로 명시하지 않을 때 쓰는 기본 구성. 정부지원사업 신청서가
# 기관을 막론하고 대체로 이 흐름을 요구한다.
DEFAULT_SECTIONS = ["사업 개요", "추진 배경 및 필요성", "추진 계획", "기대효과"]

# 자리표시자 → 회사 프로필에서 가져올 값. 여기 없는 자리표시자는 전부
# "[확인 필요: …]" 로 바뀐다.
_PLACEHOLDERS: dict[str, str] = {
    "회사명": "name",
    "대표자": "ceo",
    "업종": "industry",
    "소재지": "region",
    "설립일": "founded",
    "상시근로자수": "employees",
    "매출액": "revenue_krw",
    "사업자등록번호": "biz_no",
    "기업규모": "company_type",
    # ── 같은 값을 가리키는 다른 이름들.
    # 프롬프트에 쓸 수 있는 이름을 나열해 줘도 모델은 {{종업원 수}}, {{연매출}} 처럼
    # 제 나름의 이름을 만들어 쓴다. 그때마다 [확인 필요]로 남으면, 프로필에 값이
    # 멀쩡히 있는데도 담당자가 손으로 채워야 한다.
    "종업원수": "employees", "종업원 수": "employees",
    "상시근로자 수": "employees", "직원수": "employees", "임직원수": "employees",
    "연매출": "revenue_krw", "매출": "revenue_krw", "매출액(연)": "revenue_krw",
    "회사이름": "name", "기업명": "name", "업체명": "name",
    "대표자명": "ceo", "대표이사": "ceo",
    "설립연도": "founded", "설립일자": "founded",
    "본사소재지": "region", "주소": "region",
}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
# 뜻을 담지 않은 자리표시자 이름 (X, A, 값1, 숫자2 …).
_MEANINGLESS_KEY_RE = re.compile(r"[A-Za-z]|[A-Za-z]?\d+|값\s*\d*|숫자\s*\d*|수치\s*\d+")

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title", "body"],
            },
        }
    },
    "required": ["sections"],
}

# 프롬프트에 안내할 이름은 대표 이름만 쓴다. 별칭까지 다 나열하면 지시가 길어지고,
# 같은 값이 여러 이름으로 보여 모델이 오히려 헷갈린다 (별칭은 받아 주기만 하면 된다).
_CANONICAL_PLACEHOLDERS = ["회사명", "대표자", "업종", "소재지", "설립일",
                           "상시근로자수", "매출액", "사업자등록번호", "기업규모"]
_SECTION_SCHEMA_HINT = ", ".join(f"{{{{{k}}}}}" for k in _CANONICAL_PLACEHOLDERS)

# 초안 본문을 쓰는 모델. 기본 모델(HCX-DASH-002)은 개조식 뼈대(대항목/하위항목)를
# 잘 안 지키고 분량도 짧다. HCX-007은 형식을 정확히 따르는 대신 없는 인증·특허를
# 지어내는 경향이 있어, strip_unsupported_claims() 로 코드가 막는 걸 전제로 쓴다.
SECTION_MODEL = "HCX-007"

_DRAFT_SYSTEM = (
    "너는 중소 제조기업의 경영기획 담당자를 돕는 신청서 작성 도우미다. 주어진 공고 "
    "내용과 회사 정보, 그리고 이 회사가 예전에 제출했던 문장을 참고해 신청서 초안을 "
    "쓴다.\n"
    "규칙:\n"
    "1. **숫자를 직접 쓰지 마라.** 매출액·인원 수·설립연도·금액이 필요한 자리에는 "
    f"자리표시자만 넣어라. 쓸 수 있는 자리표시자: {_SECTION_SCHEMA_HINT}. "
    "목록에 없는 값이 필요하면 {{항목이름}} 형태로 새로 만들어 써라. 절대 임의의 "
    "숫자를 지어내지 마라.\n"
    "2. 주어진 [회사 정보]와 [참고: 과거 신청서 문장]에 없는 실적·수상·인증·거래처를 "
    "지어내지 마라. **시장 규모·성장률 같은 통계도 지어내지 마라** — 근거가 필요한 "
    "자리에는 '[확인 필요: 국내 시장 규모]' 처럼 무엇을 채워야 하는지 적는다. 이 사업의 "
    "심사 지침은 허황된 수치를 탈락 사유로 명시하고 있다.\n"
    "3. **개조식으로 쓴다.** 대항목은 'O ', 하위 항목은 ' - ' 로 시작하고 한 항목은 한 "
    "줄로 끝낸다. 심사위원이 10초 안에 요지를 잡을 수 있어야 한다. 수치 비교는 "
    "'항목 | 현재 | 목표' 처럼 세로줄로 구분한 표로 쓴다.\n"
    "4. **분량을 충분히 쓴다.** 한 항목당 600자 이상, 대항목 3~5개에 하위 항목을 붙인다. "
    "개조식이라고 서너 줄로 끝내지 마라.\n"
    "5. **PSST 논리를 지킨다.** 문제(Problem) → 해결(Solution) → 성장(Scale) → 팀(Team)이 "
    "앞뒤로 이어져야 한다. 문제로 지적한 것을 해결에서 다루고, 해결한 것이 성장 근거가 "
    "되어야 한다.\n"
    "6. 추상적인 각오나 구호로 채우지 마라. 현재 상태 → 문제 → 무엇을 어떻게 → 그래서 "
    "무엇이 달라지는지 순으로, 공정·설비·업무 흐름을 구체적으로 쓴다.\n"
    "7. '~한다', '~이다' 같은 문어체 평서형으로 쓴다. 경어체를 섞지 마라.\n"
    "8. 학교명·직장명 같은 개인정보를 쓰지 말고, 예산·일정은 회사 규모에 맞게 현실적으로 "
    "잡는다. 굵게(**) 같은 마크다운 기호는 쓰지 마라 — 한글 서식에 글자로 남는다.\n"
    "9. 요청받은 항목만, 요청받은 제목 그대로 sections에 담아라."
)

# 항목을 하나씩 따로 생성할 때 쓰는 지시. 한 번에 다 쓰게 하면 응답 길이 한도를
# 항목 수로 나눠 쓰게 돼서 항목마다 두세 문장으로 쪼그라든다 — 사업계획서에 넣기엔
# 턱없이 짧다. 항목당 한 번씩 부르면 각 항목이 제 분량을 갖는다.
_SECTION_SYSTEM = (
    "너는 15년 경력의 창업 컨설턴트이자 정부지원사업 심사위원이다. 예비창업패키지·"
    "수출바우처·스마트공장 등 정부지원사업 사업계획서를 500건 이상 검토했고, 합격하는 "
    "사업계획서가 무엇이 다른지 안다.\n"
    "지금은 사업계획서의 **한 항목만** 쓴다. 그 항목의 본문만 출력하고, 제목·머리말·"
    "맺음말·설명은 쓰지 마라.\n"
    "\n"
    "[작성 원칙]\n"
    "1. **개조식 3단으로 쓴다.** 긴 줄글이 아니라 핵심을 앞세운 항목 나열이되, 주장만 "
    "늘어놓지 말고 그 아래에 근거를 붙인다. 심사위원이 10초 만에 요지를 잡고, 더 보면 "
    "근거까지 확인할 수 있어야 한다.\n"
    "   - 1단 대항목은 'O ', 2단 하위 항목은 ' - ', 3단 세부는 '   · ' 로 시작한다.\n"
    "   - 2단은 한 줄로 끝내는 짧은 주장(명사형 '~ 구축', '~ 확보')이다.\n"
    "   - **3단에는 그 주장의 근거를 쓴다** — 어떤 방법으로, 어떤 순서로, 어떤 설비·공정에, "
    "무엇을 기준으로 판단하는지. 2단만 있고 3단이 없는 대항목을 만들지 마라.\n"
    "   - 수치·비교·기간처럼 표로 보여줄 게 있으면 '항목 | 현재 | 목표' 처럼 "
    "세로줄(|)로 구분한 표를 쓴다.\n"
    "2. **PSST 논리를 지킨다.** 이 항목이 무엇을 묻든 문제(Problem) → 해결(Solution) → "
    "성장(Scale) → 팀(Team) 흐름 안에서 앞뒤가 맞아야 한다. 문제로 지적한 것을 해결에서 "
    "다루고, 해결한 것이 성장 근거가 되어야 한다.\n"
    "3. 구호나 각오로 채우지 마라. 현재 상태 → 문제 → 무엇을 어떻게 → 그래서 무엇이 "
    "달라지는지 순으로, 공정·설비·업무 흐름을 들어 구체적으로 쓴다. 단계가 있으면 "
    "1단계·2단계로 나눈다.\n"
    "4. **대항목 4~5개**로 나누고, 각 대항목은 서로 다른 관점을 다룬다. 대항목마다 "
    "하위 항목 2~3개, 하위 항목마다 세부 1~2줄을 붙인다. 아래 뼈대를 그대로 따른다:\n"
    "     O 첫 번째 대항목\n"
    "      - 하위 항목(짧은 주장)\n"
    "         · 근거 — 어떤 방법·순서·설비·기준으로 하는지\n"
    "         · 근거 — 무엇을 기준으로 됐다고 판단하는지\n"
    "      - 하위 항목\n"
    "         · 근거\n"
    "     O 두 번째 대항목\n"
    "      - …\n"
    "   이 사업계획서는 항목 하나가 A4 반 쪽 이상을 채워야 한다. 대항목 세 개에 "
    "한 줄씩 달고 끝내면 심사에서 성의 없다는 평을 받는다.\n"
    "5. **분량을 채우려고 일반론을 늘어놓지 마라.** 다음은 어느 회사에나 해당해서 "
    "심사위원이 곧바로 알아보는 빈 말이다. 한 줄도 쓰지 마라:\n"
    "   윤리 경영, 사회적 책임, 지역사회 상생, 환경 보호, 지속 가능한 성장, "
    "경영 투명성, 고객 만족, 인재 양성, 글로벌 도약, 최선을 다함.\n"
    "   **다만 '쓸 말이 없으니 짧게 끝낸다'는 것도 답이 아니다.** 새로운 사실을 "
    "지어내지 않고 분량을 채우는 길은 하나뿐이다 — **이미 주어진 사실을 더 깊이 "
    "쪼개는 것.** 공정 하나를 들었으면 그 공정의 현재 방식·문제 지점·바꿀 방법·"
    "적용 순서·됐다고 판단할 기준으로 나눠 쓴다. 그렇게 해도 모자라는 자리에만 "
    "'[확인 필요: 무엇]'을 적어라.\n"
    "6. **문체를 끝까지 통일한다.** '~한다', '~이다', '~하고자 한다' 같은 문어체 "
    "평서형으로 끝낸다. '~합니다', '~입니다' 같은 경어체를 한 문장도 섞지 마라.\n"
    "\n"
    "[지어내지 말 것 — 가장 중요하다]\n"
    f"7. **숫자를 직접 쓰지 마라.** 자리표시자만 쓴다: {_SECTION_SCHEMA_HINT}. "
    "목록에 없는 값이 필요하면 {{항목이름}} 형태로 새로 만들어 써라. "
    "**이름은 무엇을 채워야 하는지 알 수 있게 짓는다** — {{목표 불량률}}, "
    "{{연간 절감 시간}} 처럼. {{X}}, {{A}}, {{값1}} 같은 이름은 쓰지 마라. "
    "담당자가 그 자리에 무엇을 넣어야 할지 알 수 없다.\n"
    "8. **시장 규모·성장률·점유율 같은 통계를 지어내지 마라.** 근거가 필요한 자리에는 "
    "숫자 대신 '[확인 필요: 국내 차량용 ECU 시장 규모]' 처럼 무엇을 채워야 하는지 적어라. "
    "그럴듯한 수치를 쓰는 것보다 빈칸이 백 배 낫다 — 이 사업의 심사 지침은 허황된 수치와 "
    "거짓을 탈락 사유로 명시하고 있다.\n"
    "9. [회사 정보]와 [참고 문장]에 없는 실적·수상·인증·특허·거래처를 지어내지 마라.\n"
    "10. 학교명·직장명 같은 개인정보를 쓰지 마라. 예산과 일정은 회사 규모에 맞게 "
    "현실적으로 잡는다.\n"
    "\n"
    "[출력 형식]\n"
    "11. 제목을 다시 쓰지 마라. 굵게(**)·머리글(#) 같은 마크다운 기호도 쓰지 마라 — "
    "이 글은 한글(HWP) 서식에 그대로 붙여 넣기 때문에 기호가 글자로 남는다. "
    "강조는 항목을 앞에 배치하는 것으로 대신한다."
)

# 경어체가 섞여 나올 때 문어체로 되돌리는 표 (긴 어미부터 바꾼다).
# 프롬프트로 지시해도 모델이 항목마다 다른 문체로 흘러서, 한 신청서 안에서 '~합니다'와
# '~한다'가 섞였다. 사람이 읽으면 바로 티가 나므로 코드에서 확정적으로 맞춘다.
_FORMAL_ENDINGS = [
    ("하겠습니다", "하고자 한다"), ("드립니다", "드린다"),
    ("입니다", "이다"), ("합니다", "한다"), ("됩니다", "된다"),
    ("습니다", "다"),          # 있습니다→있다, 하였습니다→하였다 … 를 한꺼번에
]
# 문장 끝에서만 바꾼다. 문장 중간의 인용구까지 건드리면 어색해진다.
_FORMAL_RE = [(re.compile(re.escape(a) + r"(?=[.。]|\s|$)"), b)
              for a, b in _FORMAL_ENDINGS]


def _to_formal(text: str) -> str:
    """경어체 어미를 신청서용 문어체로 되돌린다."""
    for pattern, replacement in _FORMAL_RE:
        text = pattern.sub(replacement, text)
    return text

# ── 튜닝 모델용 평문 경로 ───────────────────────────────────────────────
# 튜닝한 모델은 Structured Outputs를 지원하지 않는다(공식 문서). 그래서 JSON 스키마로
# 강제하지 못하고, 형식을 프롬프트로 정한 뒤 평문을 파싱해야 한다.
#
# 구분자를 '■'로 잡은 이유: 신청서 본문에 거의 안 나오는 글자라 본문과 헷갈리지 않고,
# 튜닝 데이터에도 같은 형식을 쓰면 모델이 이 형식을 그대로 익힌다.
_TUNED_SYSTEM = _DRAFT_SYSTEM + (
    "\n\n출력 형식(반드시 지켜라):\n"
    "항목마다 '■ 항목제목' 한 줄을 먼저 쓰고, 다음 줄부터 그 항목의 문단을 쓴다.\n"
    "항목 사이는 빈 줄로 구분한다. 그 밖의 머리말·맺음말·설명은 쓰지 마라.\n"
    "예시:\n■ 사업 개요\n{{회사명}}은 …\n\n■ 기대효과\n본 사업을 통해 …"
)

_SECTION_MARK_RE = re.compile(r"^\s*[■□▶●]\s*(?P<title>.+?)\s*$", re.M)


def parse_sections(text: str, wanted: list[str]) -> list[tuple[str, str]]:
    """'■ 제목' 형식의 평문을 (제목, 본문) 목록으로 되돌린다.

    구분자를 못 찾으면 통째로 한 덩어리가 되는데, 그때는 요청한 항목 수만큼 쪼개는
    대신 **첫 항목 하나로** 돌려준다. 억지로 나누면 문단이 엉뚱한 항목에 붙어서,
    담당자가 그걸 알아채지 못한 채 제출할 위험이 있다.
    """
    marks = list(_SECTION_MARK_RE.finditer(text or ""))
    if not marks:
        body = (text or "").strip()
        return [(wanted[0] if wanted else "사업 개요", body)] if body else []

    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        title = m.group("title").strip(" .:：-")
        if title and body:
            out.append((title, body))
    return out


# 키가 없을 때 쓰는 뼈대 초안. 백지보다는 낫고, 숫자를 지어내지도 않는다.
_FALLBACK_BODY = {
    "사업 개요": "{{회사명}}은(는) {{소재지}}에 소재한 {{업종}} 기업으로, 본 사업을 통해 "
                 "공고에서 요구하는 지원 내용을 수행하고자 한다. (담당자 확인 후 보완 필요)",
    "추진 배경 및 필요성": "현재 당사는 아래와 같은 현안을 안고 있으며, 자체 역량만으로는 "
                          "해결이 어려워 본 사업의 지원이 필요하다. (담당자 확인 후 보완 필요)",
    "추진 계획": "본 사업 기간 동안 단계별로 과제를 수행하며, 각 단계의 산출물을 확인한 뒤 "
                 "다음 단계로 넘어간다. (담당자 확인 후 보완 필요)",
    "기대효과": "본 사업을 완료하면 당사의 현안이 개선되고, 관련 지표를 근거로 성과를 "
                "확인할 수 있게 된다. (담당자 확인 후 보완 필요)",
}


# ═══════════════════════════════════════════════════════ 항목 뽑기 · 재료 모으기

# 공고문이 "제출서류: 사업계획서(사업 개요, 추진 계획, 기대효과 포함)" 처럼 항목을
# 적어 두는 경우가 있어, 알려진 항목명이 원문에 있으면 그것을 우선한다.
_KNOWN_SECTIONS = [
    "사업 개요", "사업개요", "추진 배경", "추진배경", "필요성", "추진 계획", "추진계획",
    "사업 목표", "사업목표", "기대효과", "기대 효과", "활용 방안", "사업비 집행 계획",
]


def pick_sections(notice: Notice) -> list[str]:
    """이 공고의 신청서가 요구하는 항목 목록."""
    text = notice.full_text()
    found: list[str] = []
    for name in _KNOWN_SECTIONS:
        if name in text:
            canonical = name if " " in name else re.sub(r"(..)(..)", r"\1 \2", name)
            if canonical not in found:
                found.append(canonical)
    return found if len(found) >= 2 else list(DEFAULT_SECTIONS)


def _profile_block(p: CompanyProfile) -> str:
    """LLM에 넘길 회사 정보. 숫자는 여기 있지만, 본문에 그대로 쓰라고는 하지 않는다."""
    lines = [
        f"회사명: {p.name}", f"대표자: {p.ceo}", f"업종: {p.industry} ({p.ksic})",
        f"소재지: {p.region} {p.region_detail}", f"설립일: {p.founded}",
        f"상시근로자: {p.employees}명", f"기업규모: {p.company_type}",
    ]
    if p.tech_services:
        lines.append(f"보유 기술·서비스: {p.tech_services}")
    if p.strengths:
        lines.append(f"회사 강점·현안: {p.strengths}")
    for k, v in (p.extra or {}).items():
        lines.append(f"{k}: {v}")
    return "\n".join(lines)


def _reference_block(section: str, notice: Notice,
                     passages: list) -> tuple[str, list[str]]:
    """항목별 참고 문장 + 어느 파일에서 왔는지."""
    hits = past_search.search(f"{section} {notice.title} {notice.summary}", passages)
    if not hits:
        return "(참고할 과거 문장이 없습니다)", []
    block = "\n\n".join(f"[{p.source} — {p.heading}]\n{p.text[:600]}" for _, p in hits)
    return block, [p.source for _, p in hits]


# ══════════════════════════════════════════════════════════ 자리표시자 채우기

def _format_value(attr: str, p: CompanyProfile) -> str | None:
    value = getattr(p, attr, None)
    if value in (None, "", 0):
        return None
    if attr == "revenue_krw":
        # 억 단위가 신청서에서 읽기 좋다. 딱 떨어지지 않으면 소수 한 자리.
        billions = value / 100_000_000
        return f"{billions:.0f}억원" if billions == int(billions) else f"{billions:.1f}억원"
    if attr == "employees":
        return f"{value}명"
    if attr == "founded":
        y, m, _ = value.split("-")
        return f"{y}년 {int(m)}월"
    return str(value)


def fill_placeholders(text: str, p: CompanyProfile) -> tuple[str, list[str]]:
    """{{자리표시자}}를 프로필 실제 값으로 바꾼다.

    반환: (치환된 본문, 채우지 못한 항목 목록)

    채우지 못한 자리는 지우지 않고 "[확인 필요: 매출액]" 로 남긴다. 조용히 지우면
    문장이 매끄러워져서 담당자가 빠진 걸 못 보고 그대로 제출하게 된다.
    """
    unresolved: list[str] = []

    def replace(m: re.Match) -> str:
        key = m.group(1).strip()
        attr = _PLACEHOLDERS.get(key)
        if attr:
            value = _format_value(attr, p)
            if value is not None:
                return value
        # 이름이 뜻을 담고 있지 않으면(X, A, 값1) 그대로 두지 않는다.
        # "[확인 필요: X]" 는 담당자에게 아무것도 알려 주지 않고 오류처럼 보인다.
        # 프롬프트로 이름을 제대로 짓게 시켜도 모델이 계속 한 글자를 쓴다.
        if _MEANINGLESS_KEY_RE.fullmatch(key):
            key = "수치"
        if key not in unresolved:
            unresolved.append(key)
        return f"[확인 필요: {key}]"

    return _PLACEHOLDER_RE.sub(replace, text), unresolved


# 프로필에 없는 숫자를 LLM이 그냥 써 버린 경우를 잡아내기 위한 패턴.
# (자리표시자 규칙을 어긴 경우 — checker.py의 수치 대조와 함께 이중으로 막는다.)
_SUSPICIOUS_NUMBER_RE = re.compile(
    r"\d[\d,.]*\s*(?:%|퍼센트|억원|천만원|백만원|만원|원|명|건|배|톤|kg|시간|개월)")
# 근거가 있는지 볼 때 쓰는 숫자만 남긴 형태 ("1,200" → "1200").
_DIGITS_RE = re.compile(r"[\d]+")

_NUMBER_MARK = "[확인 필요]"


def flag_unverified_numbers(text: str, grounded: str) -> tuple[str, list[str]]:
    """근거 없는 수치 뒤에 [확인 필요] 를 붙인다. 반환: (본문, 표시한 수치 목록)

    프롬프트로 "숫자를 쓰지 말고 자리표시자를 써라"고 아무리 못 박아도 모델은
    '결함 검출 정확도 최소 95% 이상', '처리 속도 초당 10건' 같은 목표치를 스스로
    만들어 낸다. 그럴듯해서 담당자도 그냥 넘기기 쉬운데, 이 사업의 신청 유의사항은
    허황된 수치를 탈락 사유로 명시하고 있다.

    **지우지 않고 표시만 한다.** 목표 수치 자체는 사업계획서에 필요한 내용이라
    통째로 없애면 글이 앙상해진다. 담당자가 실제 목표로 바꿔 적으면 되는 자리다.

    grounded 에는 회사 프로필에서 채운 값과 공고문 본문을 넣는다 — 공고에 적힌
    수치('총 사업비의 50% 이내')를 옮겨 쓴 것까지 의심하면 표시가 너무 흔해져서
    담당자가 전부 무시하게 된다.
    """
    facts = "".join(_DIGITS_RE.findall(grounded or ""))
    flagged: list[str] = []

    source = text or ""

    def mark(m: re.Match) -> str:
        token = m.group(0)
        digits = "".join(_DIGITS_RE.findall(token))
        # 근거 글에 같은 숫자가 있으면 옮겨 적은 것으로 보고 넘어간다.
        if digits and digits in facts:
            return token
        # 이미 표시가 붙어 있으면 또 붙이지 않는다. 대화로 고칠 때 모델이 앞선 표시가
        # 달린 본문을 그대로 옮겨 적어서 '80%[확인 필요][확인 필요]' 가 됐다.
        if source.startswith(_NUMBER_MARK, m.end()):
            return token
        if token not in flagged:
            flagged.append(token)
        return f"{token}{_NUMBER_MARK}"

    return _SUSPICIOUS_NUMBER_RE.sub(mark, source), flagged


# ═══════════════════════════════════════════════════════════════════ 진입점

# 항목 하나의 최소 분량. 이보다 짧으면 한 번 더 불러 이어 쓰게 한다.
#
# 프롬프트에 "충분히 쓰라"고 적는 것만으로는 안 됐다. 실측하면 같은 프롬프트로도
# 400자에서 끝나는 항목과 1,200자짜리 항목이 섞여 나온다. 분량은 심사에서 성의로
# 읽히는 부분이라, 흔들리게 두지 않고 코드로 하한을 만든다.
MIN_SECTION_CHARS = 700

# 항목별 호출 사이에 두는 간격(초). 호출 한도에 걸리지 않을 만큼만 띄운다.
_CALL_GAP_SEC = 2.5

_EXPAND_SYSTEM = _SECTION_SYSTEM + (
    "\n\n[지금은 이어 쓰는 중이다]\n"
    "이미 쓴 초안이 짧다. **새로운 사실을 지어내지 말고**, 이미 쓴 각 항목을 더 깊이 "
    "쪼개서 다시 써라. 하위 항목마다 세부 근거('   · ')를 붙이고, 근거가 부족한 대항목은 "
    "방법·순서·적용 대상 공정·판단 기준으로 나눈다. 빠진 관점이 있으면 대항목을 "
    "추가해도 좋다. 이미 쓴 내용을 그대로 옮겨 적기만 하지 말고, 늘린 전체 본문을 "
    "처음부터 끝까지 다시 출력해라."
)


def _expand(body: str, user: str, title: str) -> str:
    """짧게 나온 항목을 한 번 더 불러 늘린다. 실패하면 빈 문자열.

    늘린 결과가 원본보다 짧으면 버린다 — 이어 쓰라고 했는데 오히려 요약해 오는 경우가
    있어서, 그때 덮어쓰면 초안이 더 나빠진다.
    """
    prompt = (f"{user}\n\n[이미 쓴 초안 — 이것을 더 깊이 쪼개 늘린다]\n{body}\n\n"
              f"위 초안을 '{title}' 항목으로서 충분한 분량이 되도록 다시 써라.")
    try:
        grown = hyperclova_api.chat(_EXPAND_SYSTEM, prompt, max_tokens=3000,
                                    temperature=0.4, model=SECTION_MODEL).strip()
    except Exception as e:
        print(f"[알림] '{title}' 이어쓰기 실패 — 짧은 초안을 그대로 씁니다. ({e})")
        return ""
    return grown if len(grown) > len(body) else ""


def _generate_per_section(titles: list[str], notice: Notice, profile: CompanyProfile,
                          reference_blocks: list[str],
                          notice_text: str = "") -> tuple[list[tuple[str, str]], str]:
    """항목을 **하나씩** 생성한다. 반환: (생성된 (제목, 본문) 목록, 실패 사유)

    한 번의 호출로 모든 항목을 받으면 응답 길이 한도를 항목 수로 나눠 쓰게 돼서 항목마다
    두세 문장으로 쪼그라든다. 사업계획서에 넣을 글로는 턱없이 짧다. 항목당 한 번씩
    부르면 각 항목이 제 분량(600~1200자)을 갖는다.

    대신 호출 수가 항목 수만큼 늘어난다. 항목이 아무리 많아도 8개까지만 생성해
    한 건의 초안이 하염없이 길어지지 않게 한다.
    """
    made: list[tuple[str, str]] = []
    flagged: list[str] = []
    last_error = ""
    # 공고문 본문(첨부 공고문에서 뽑아낸 글)을 재료로 넣는다. 이게 빠지면 모델이
    # 제목·개요 몇 줄만 보고 쓰게 돼서, 어느 사업에나 해당하는 짧은 글이 나온다.
    # 사업 목적·지원 내용·평가 항목이 다 여기 들어 있어 분량과 구체성이 여기서 갈린다.
    body_excerpt = ""
    if notice_text:
        body_excerpt = (f"\n[공고문 본문 발췌 — 이 사업이 무엇을 요구하는지의 근거]\n"
                        f"{notice_text[:3000]}\n")
    common = (
        f"[공고]\n제목: {notice.title}\n소관기관: {notice.agency}\n"
        f"지원분야: {notice.support_field}\n개요: {notice.summary}\n"
        f"지원대상: {notice.target_text}\n"
        f"{body_excerpt}\n"
        f"[회사 정보]\n{_profile_block(profile)}\n"
    )
    for i, title in enumerate(titles[:8]):
        # 항목마다 따로 부르고 짧으면 이어쓰기까지 하므로, 초안 한 건에 호출이 몰려
        # 호출 한도(429)에 걸린다. 걸린 뒤에 기다리는 것(_post의 재시도)보다 처음부터
        # 간격을 두는 편이 전체로는 더 빠르다 — 재시도는 2·5·12초를 통째로 버린다.
        if i:
            time.sleep(_CALL_GAP_SEC)
        reference = reference_blocks[i] if i < len(reference_blocks) else ""
        # 항목마다 따로 부르다 보니 모델이 같은 회사 소개를 매번 되풀이한다. 앞서 쓴
        # 항목과 그 첫 줄들을 함께 넘겨, 이미 다룬 내용을 다시 쓰지 않게 한다.
        written = ""
        if made:
            done = "\n".join(f"- {t}: {b.splitlines()[0][:60] if b.splitlines() else ''}"
                             for t, b in made)
            written = (f"\n[이미 작성한 항목 — 같은 내용을 되풀이하지 마라]\n{done}\n"
                       f"이 항목에서는 앞에서 다루지 않은 내용을 쓴다.\n")
        remaining = [t for t in titles[:8] if t != title]
        user = (f"{common}\n[참고: 과거 신청서 문장]\n{reference}\n{written}\n"
                f"[전체 항목 구성]\n{', '.join(titles[:8])}\n"
                f"(다른 항목: {', '.join(remaining) or '없음'} — 그쪽에서 다룰 내용은 "
                f"여기 쓰지 마라)\n\n"
                f"[지금 쓸 항목]\n{title}\n\n"
                f"위 항목의 본문만, 개조식 뼈대(O 대항목 / - 하위항목)로 쓰시오. 근거 있는 내용만 쓰고, 일반론으로 분량을 채우지 마시오.")
        try:
            body = hyperclova_api.chat(_SECTION_SYSTEM, user, max_tokens=3000,
                                       temperature=0.4, model=SECTION_MODEL).strip()
            if len(body) < MIN_SECTION_CHARS:
                body = _expand(body, user, title) or body
        except Exception as e:
            last_error = str(e)
            continue
        body = _clean_body(body, title)
        body, dropped = strip_unsupported_claims(body, profile)
        flagged.extend(d for d in dropped if d not in flagged)
        if body:
            made.append((title, body))
    return made, last_error


# 본문에 섞여 나오는 마크다운 강조. 한글(HWP) 서식에 붙여 넣으면 기호가 글자로 남아
# 담당자가 일일이 지워야 하므로 걷어낸다.
_BOLD_RE = re.compile(r"\*{1,3}(.+?)\*{1,3}")
# 마크다운 불릿(*, •, ▪)만 '-' 로 통일하고 **들여쓰기는 살린다**. 개조식에서
# 들여쓰기가 곧 대항목·하위항목의 계층이라, 지우면 구조가 무너진다.
#
# '·'는 여기서 제외한다 — 3단 개조식(O 대항목 / - 하위항목 / · 세부 근거)에서 '·'가
# 3단을 나타내는 기호다. 예전엔 이것도 '-'로 바꿔서, 모델이 3단으로 써 보내도
# 후처리가 2단으로 납작하게 만들어 버렸다.
_BULLET_RE = re.compile(r"^([ \t]*)[*•▪]\s+", re.M)


# 회사가 실제로 갖고 있어야만 쓸 수 있는 주장들. 프로필에 근거가 없는데 모델이
# 써 버리면 그대로 탈락 사유가 된다 — 신청 유의사항이 "허황된 수치 또는 거짓
# (할루시네이션)은 탈락"이라고 못 박고 있다.
#
# 실제로 HCX-007이 "ISO/TS16949 인증 획득", "특허를 다수 보유", "평균 경력 10년 이상의
# 팀"을 지어냈다. 프롬프트로 아무리 금지해도 새어 나오므로 코드가 최종적으로 막는다.
# 개조식이라 주장 하나가 한 줄이어서, 줄 단위로 걷어내면 문장이 깨지지 않는다.
_CLAIM_GUARDS = [
    ("보유 인증", re.compile(r"인증(?:을|서)?\s*(?:획득|취득|보유)|ISO|IATF|HACCP|"
                             r"이노비즈|메인비즈|벤처기업\s*확인")),
    ("보유 특허·지식재산", re.compile(r"특허|실용신안|디자인등록|상표등록|지식재산권")),
    ("수상 이력", re.compile(r"수상|표창|우수기업\s*선정|대상\s*수상")),
    ("주요 거래처", re.compile(r"거래처|납품처|고객사|협력\s*관계|공급\s*계약")),
    ("인력 경력", re.compile(r"평균\s*경력|경력\s*\d+\s*년\s*이상|전문가.{0,6}구성")),
    ("매출·수출 실적", re.compile(r"매출\s*(?:증가|성장|달성)|수출\s*실적|점유율")),
]


# ── 지어낸 사람 이름 막기 ──────────────────────────────────────────────
# '참여인력 현황' 같은 항목에서 모델이 없는 직원을 만들어 낸다. 실제로 '연구원 김철수
# — AI 딥러닝 전문가', '연구원 이영희' 를 지어냈다. 회사에 그런 사람이 없으면 이건
# 허위 기재이고(심사 지침이 탈락 사유로 명시), 실존 인물이면 개인정보 문제가 된다.
#
# 위 _CLAIM_GUARDS 로는 못 잡힌다. 거기 '인력 경력' 규칙은 '평균 경력 N년' 같은 표현을
# 보는데, 이름은 그런 낱말 없이 그냥 나온다.
_ROLE_WORDS = ("대표이사|대표|이사|본부장|팀장|실장|소장|과장|부장|차장|주임|"
               "선임|책임|수석|연구원|연구소장|매니저|담당자|사원|기술사|박사")
# 흔한 성씨로 시작하는 2~4글자만 이름으로 본다. 이 조건이 없으면 '연구원 확보',
# '팀장 배치' 처럼 직책 뒤에 오는 보통 명사까지 이름으로 잡는다.
_SURNAME = ("김|이|박|최|정|강|조|윤|장|임|한|오|서|신|권|황|안|송|류|전|홍|고|문|양|"
            "손|배|백|허|남|심|노|하|곽|성|차|주|우|구|나|민|유|진|지|엄|채|원|천|방|"
            "공|현|함|변|염|여|추|도|소|석|선|설|마|길|연|위|표|명|기|반|왕|금|옥|육|"
            "인|맹|제|모|탁|국|어|은|편|용")
# 이름 후보는 **낱말이 시작하는 자리**에서만 찾는다. 앞에 한글이 붙어 있으면 이름이
# 아니라 딴 말의 꼬리다 — '총괄하며 팀장'의 '하며'가 성씨 '하'로 시작한다는 이유로
# 이름이 됐다. (자격 판정의 시·군·구 인식에서 쓴 것과 같은 방법이다.)
_PERSON_RE = re.compile(
    rf"(?:{_ROLE_WORDS})\s*[|/·,:\-]?\s*(?<![가-힣])((?:{_SURNAME})[가-힣]{{1,3}})(?![가-힣])"
    rf"|(?<![가-힣])((?:{_SURNAME})[가-힣]{{1,3}})\s*[|/·,:\-]?\s*(?:{_ROLE_WORDS})(?![가-힣])")
# 이름 뒤에 붙는 조사. 떼어 내야 '배치를'→'배치', '박민수가'→'박민수' 로 견줄 수 있다.
_JOSA_RE = re.compile(r"(?:이|가|은|는|을|를|과|와|의|에|도|만|께|씨|님)$")
_ROLE_SET = set(_ROLE_WORDS.split("|"))


def _name_or_none(token: str, people: str) -> str | None:
    """이름으로 볼 만한 낱말이면 돌려주고, 아니면 None.

    조사를 뗀 뒤 길이와 낱말 목록으로 거른다. 한글은 사람 이름과 보통명사가 생김새로
    구분되지 않아, 이 정도가 사전 없이 할 수 있는 최선이다.
    """
    if not token:
        return None
    bare = _JOSA_RE.sub("", token)
    if len(bare) < 2 or bare in _NOT_A_NAME or token in _NOT_A_NAME:
        return None
    # 직책 낱말이 이름 자리에 걸리는 경우 — '선임 연구원'의 '연구원'이 성씨 '연'으로
    # 시작한다는 이유로 이름이 됐다. 직책은 두 개가 겹쳐 나오는 일이 흔하다.
    if bare in _ROLE_SET or token in _ROLE_SET:
        return None
    if bare in people or token in people:
        return None
    return bare


# 성씨로 시작하지만 이름이 아닌 말. '팀장 배치를 통해'의 '배치'가 성씨 '배'로 시작해서
# 이름으로 잡혔다. 직책 뒤에 자주 오는 보통명사라 따로 걸러 낸다.
_NOT_A_NAME = {
    "배치", "배정", "배분", "구성", "구축", "구분", "확보", "확대", "확인", "선정",
    "선발", "선임", "채용", "채택", "양성", "교육", "유치", "강화", "신규", "전담",
    "담당", "임명", "보강", "충원", "조직", "운영", "관리", "지원", "참여", "모집",
    "주도", "주관", "협력", "고용", "노력", "역량", "인력", "정규", "기술", "기준",
    "명시", "문제", "방식", "성과", "수행", "안정", "연구", "장비", "전문", "제공",
    "지정", "직접", "최적", "추진", "투입", "하나", "한계", "현황", "활용", "회의",
}


def _known_people(p: CompanyProfile) -> str:
    """이름을 써도 되는 사람 — 프로필에 실제로 적힌 사람뿐이다."""
    return " ".join(str(x) for x in (p.ceo, p.name, p.strengths,
                                     p.tech_services) if x)


def _profile_evidence(p: CompanyProfile) -> str:
    """프로필이 실제로 담고 있는 사실 전체 — 주장의 근거가 여기 있는지 대조한다."""
    parts = [p.name, p.industry, p.ksic, p.company_type, p.strengths, p.tech_services,
             " ".join(f"{k} {v}" for k, v in (p.extra or {}).items())]
    return " ".join(str(x) for x in parts if x)


def strip_unsupported_claims(body: str, profile: CompanyProfile) -> tuple[str, list[str]]:
    """근거 없는 실적 주장을 [확인 필요]로 바꾼다. 반환: (본문, 바꾼 항목 이름들)

    지우지 않고 자리를 남기는 이유는 초안의 자리표시자와 같다 — 조용히 지우면 문장이
    매끄러워져서 담당자가 빠진 걸 못 보고 그대로 제출한다. 눈에 띄게 남겨야 채운다.
    """
    evidence = _profile_evidence(profile)
    people = _known_people(profile)
    flagged: list[str] = []
    out: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()

        # 대항목 제목('O 인증')은 주장이 아니라 이름표다. 제목까지 바꾸면 문단 구조가
        # 무너지고, 아래 하위 항목이 이미 [확인 필요]로 바뀌어 뜻은 충분히 전달된다.
        is_heading = stripped.startswith(("O ", "○", "◯")) and len(stripped) < 24
        if is_heading or not stripped:
            out.append(line)
            continue

        # **같은 갈래의 근거가 프로필에 있는지**로 판단한다. 문장 속 아무 단어나
        # 프로필에 있으면 통과시키던 방식은 "차량용 … 특허를 다수 보유"를 그냥
        # 넘겨 버렸다 — '차량용'이 프로필에 있다는 이유로.
        hit = next((label for label, pattern in _CLAIM_GUARDS
                    if pattern.search(stripped) and not pattern.search(evidence)), None)

        # 프로필에 없는 사람 이름은 이름만 가린다. 줄을 통째로 지우면 직책·역할처럼
        # 쓸모 있는 내용까지 사라져서, 담당자가 표를 처음부터 다시 짜야 한다.
        if not hit:
            names = [name for m in _PERSON_RE.finditer(stripped)
                     for n in m.groups()
                     if (name := _name_or_none(n, people))]
            if names:
                for name in dict.fromkeys(names):
                    line = line.replace(name, "[확인 필요: 실명]")
                if "참여 인력" not in flagged:
                    flagged.append("참여 인력")
                out.append(line)
                continue

        if hit:
            indent = line[:len(line) - len(line.lstrip())]
            # 원래 단계 기호를 그대로 살린다. '·'(3단)을 '-'(2단)로 바꿔 놓으면
            # [확인 필요] 줄만 한 단계 올라와 개조식 계층이 어긋난다.
            marker = f"{stripped[0]} " if stripped[0] in "-·*" else ""
            out.append(f"{indent}{marker}[확인 필요: {hit} — 실제 내역을 적거나 "
                       f"이 줄을 지우세요]")
            if hit not in flagged:
                flagged.append(hit)
            continue
        out.append(line)
    return "\n".join(out), flagged


def _clean_body(body: str, title: str) -> str:
    """생성된 본문을 신청서에 그대로 넣을 수 있게 다듬는다.

    '본문만 쓰라'고 해도 모델이 제목을 다시 쓰고(`**과제 지원의 필요성**`), 마크다운
    강조를 섞는 일이 잦다. 한글 신청서에 `**`가 박혀 있으면 담당자가 일일이 지워야 한다.
    """
    body = _BOLD_RE.sub(r"\1", body)
    body = _BULLET_RE.sub(r"\1- ", body)

    # 첫 줄이 제목을 되풀이한 것이면 떼어낸다.
    lines = body.split("\n")
    while lines:
        head = re.sub(r"[^가-힣A-Za-z0-9]", "", lines[0])
        want = re.sub(r"[^가-힣A-Za-z0-9]", "", title)
        if head and want and (head == want or (head in want and len(head) >= 4)):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
            continue
        break
    body = _to_formal("\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def generate(notice: Notice, profile: CompanyProfile,
             sections: list[str] | None = None,
             notice_text: str = "") -> Draft:
    """공고 1건에 대한 신청서 초안을 만든다."""
    titles = sections or pick_sections(notice)
    passages = past_search.load_passages()

    used_sources: list[str] = []
    reference_blocks: list[str] = []
    for title in titles:
        block, sources = _reference_block(title, notice, passages)
        reference_blocks.append(f"### {title}\n{block}")
        used_sources.append(", ".join(dict.fromkeys(sources)))

    note = ""
    raw_sections: list[tuple[str, str]] = []

    if hyperclova_api.is_configured():
        user = (
            f"[공고]\n제목: {notice.title}\n소관기관: {notice.agency}\n"
            f"지원분야: {notice.support_field}\n개요: {notice.summary}\n"
            f"지원대상: {notice.target_text}\n\n"
            f"[회사 정보]\n{_profile_block(profile)}\n\n"
            f"[참고: 과거 신청서 문장]\n" + "\n\n".join(reference_blocks) + "\n\n"
            f"[작성할 항목]\n" + "\n".join(f"- {t}" for t in titles)
        )

        # 튜닝한 모델이 설정돼 있으면 그쪽을 먼저 쓴다. 실패하면 기본 모델로 내려간다 —
        # 튜닝 작업이 만료·삭제됐거나 학습이 아직 안 끝났을 때 초안 기능이 통째로
        # 죽으면 안 되기 때문이다.
        task_id = hyperclova_api.tuned_task_id()
        if task_id:
            try:
                text = hyperclova_api.chat_tuned(_TUNED_SYSTEM, user, max_tokens=2000)
                raw_sections = parse_sections(text, titles)
                if raw_sections:
                    note = f"튜닝 모델({task_id})로 작성했습니다."
                else:
                    print("[알림] 튜닝 모델 응답에서 항목을 찾지 못해 기본 모델로 넘어갑니다.")
            except Exception as e:
                print(f"[알림] 튜닝 모델 호출 실패 — 기본 모델로 대체합니다. ({e})")
                note = "튜닝 모델을 부르지 못해 기본 모델로 작성했습니다."

        if not raw_sections:
            raw_sections, failed = _generate_per_section(
                titles, notice, profile, reference_blocks, notice_text)
            if failed and not raw_sections:
                print(f"[알림] 초안 생성 LLM 호출 실패 — 뼈대 초안으로 대체합니다. ({failed})")
                note = ("AI를 부르지 못해 뼈대 초안만 넣었습니다. 잠시 후 "
                        "‘같은 항목으로 다시 쓰기’를 눌러 주세요.")
            elif failed:
                print(f"[알림] 일부 항목 생성 실패 — 만들어진 항목만 넘깁니다. ({failed})")
                note = ("일부 항목을 만들지 못했습니다. 지금 화면에 있는 항목은 그대로 "
                        "쓰셔도 됩니다. 나머지가 필요하면 잠시 후 ‘같은 항목으로 다시 "
                        "쓰기’를 눌러 주세요.")
    else:
        note = "AI가 설정되지 않아 뼈대 초안만 넣었습니다."

    if not raw_sections:
        raw_sections = [(t, _FALLBACK_BODY.get(t, _FALLBACK_BODY["사업 개요"]))
                        for t in titles]
        note = note or "AI가 항목을 만들지 못해 뼈대 초안으로 대체했습니다."

    filled: list[DraftSection] = []
    unresolved: list[str] = []
    # 수치의 근거로 인정할 글 — 회사 프로필의 사실과 공고문 본문.
    #
    # **자리표시자를 채운 값도 반드시 넣는다.** _profile_block 에는 매출액이 없어서,
    # {{매출액}} 을 채운 '18.5억원' 이 도리어 "근거 없는 수치"로 표시됐다. 도구가 제
    # 손으로 넣은 값을 스스로 의심하는 꼴이라, 담당자가 표시를 믿지 않게 된다.
    substituted = " ".join(_format_value(attr, profile) or ""
                           for attr in set(_PLACEHOLDERS.values()))
    grounded = (f"{_profile_evidence(profile)} {_profile_block(profile)} "
                f"{substituted} {notice_text}")
    for i, (title, body) in enumerate(raw_sections):
        text, missing = fill_placeholders(body, profile)
        text, numbers = flag_unverified_numbers(text, grounded)
        for key in missing:
            if key not in unresolved:
                unresolved.append(key)
        for num in numbers:
            label = f"근거 없는 수치: {num}"
            if label not in unresolved:
                unresolved.append(label)
        sources = used_sources[i].split(", ") if i < len(used_sources) else []
        filled.append(DraftSection(title=title or titles[min(i, len(titles) - 1)],
                                   body=text,
                                   sources=[s for s in sources if s]))

    return Draft(notice_id=notice.id, sections=filled, unresolved=unresolved, note=note)

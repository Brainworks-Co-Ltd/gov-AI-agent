"""오픈API 어댑터 두 개(기업마당·K-Startup)가 함께 쓰는 잡부 함수들.

여기서 흡수하는 현실의 지저분함:
  1. 기관마다 날짜를 다르게 쓴다 — "20260105", "2026-01-05", "2026.01.05",
     "2026-01-05 ~ 2026-02-10", "예산 소진 시까지"
  2. 같은 뜻의 필드 이름이 문서와 실제 응답에서 다를 때가 있다 → 별칭(alias) 조회
  3. 공고문에 HTML 태그와 &nbsp; 같은 엔티티가 섞여 들어온다
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

_UA = "gov-support-agent/1.0 (solverthon B-08)"

# "2026-01-05", "2026.01.05", "2026/01/05", "20260105", "2026년 1월 5일"
_DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})\s*[-./]\s*(?P<m>\d{1,2})\s*[-./]\s*(?P<d>\d{1,2})"),
    re.compile(r"(?P<y>20\d{2})년\s*(?P<m>\d{1,2})월\s*(?P<d>\d{1,2})일"),
    re.compile(r"\b(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})\b"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")


def get_json(url: str, params: dict, timeout: int = 20) -> dict | list:
    """GET 요청 → JSON 파싱. 실패는 그대로 예외로 올린다(호출부가 폴백 결정)."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{url}?{query}", method="GET")
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} — {detail}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        # 인증키 오류 시 JSON 대신 XML 에러 문서를 주는 기관이 많다.
        raise RuntimeError(f"JSON이 아닌 응답을 받았습니다 — {body[:300]}") from None


def clean_text(value) -> str:
    """HTML 태그·엔티티·중복 공백을 걷어낸 평문.

    공고 본문은 그대로 LLM 프롬프트와 화면에 들어가므로, 태그가 섞여 있으면
    토큰만 잡아먹고 판정 근거 인용도 지저분해진다.
    """
    if value is None:
        return ""
    s = str(value)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def pick(row: dict, *names: str) -> str:
    """별칭 중 먼저 값이 있는 필드를 골라 평문으로 돌려준다.

    K-Startup 응답 필드명은 공공데이터포털 명세가 ZIP 가이드 안에만 있어 문서와
    실제가 어긋날 수 있다. 이름을 여러 개 받아 방어적으로 조회한다.
    """
    for n in names:
        if n in row and row[n] not in (None, "", []):
            return clean_text(row[n])
    return ""


def parse_date(value: str) -> date | None:
    """문자열에서 날짜 하나를 뽑는다. 못 찾으면 None."""
    if not value:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(value)
        if not m:
            continue
        try:
            return date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            continue
    return None


def parse_date_range(value: str) -> tuple[date | None, date | None]:
    """'2026-01-05 ~ 2026-02-10' 같은 기간 문자열을 (시작, 마감)으로 나눈다.

    기업마당 reqstBeginEndDe가 이 형태다. 날짜가 하나만 있으면 그것을 마감으로
    본다 — 담당자에게 중요한 건 '언제까지'이기 때문이다. "예산 소진 시까지" 처럼
    날짜가 아예 없으면 (None, None)이고, 화면에서는 '상시/미상'으로 표시된다.
    """
    if not value:
        return None, None
    found: list[date] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(value):
            try:
                found.append(date(int(m["y"]), int(m["m"]), int(m["d"])))
            except ValueError:
                continue
        if found:
            break
    if not found:
        return None, None
    if len(found) == 1:
        return None, found[0]
    return min(found), max(found)

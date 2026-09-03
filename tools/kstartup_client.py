"""K-Startup(창업진흥원) 사업공고 조회 오픈API 어댑터.

공공데이터포털 15125364 — 창업진흥원_K-Startup(사업소개, 사업공고, 콘텐츠 등)_조회서비스
    GET https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01
    요청: serviceKey(필수, Decoding 키), page, perPage, returnType=json

⚠️ 이 API의 상세 명세는 포털에서 ZIP 가이드 파일로만 배포돼, 웹 문서만으로는 필드명을
확정할 수 없었다. 그래서 아래 원칙으로 방어한다:
    1. 엔드포인트 경로도 후보를 여러 개 두고 순서대로 시도한다 (_ENDPOINTS)
    2. 필드는 apiutil.pick()에 별칭을 여러 개 넘겨 조회한다
    3. 원본 행(raw)을 통째로 보관해, 나중에 필드명이 확인되면 재파싱만 하면 된다
`python -m tools.ingest --dry-run` 으로 실제 응답의 키 목록을 찍어볼 수 있다.
"""
from __future__ import annotations

from agent.schemas import Notice
from tools import apiutil, keys

SOURCE = "K-Startup"

_BASE = "https://apis.data.go.kr/B552735/kisedKstartupService01"
# 포털 운영 이력상 오퍼레이션 이름에 01 접미사가 붙은 버전과 안 붙은 버전이 함께
# 존재한다. 첫 호출에서 성공하는 것을 골라 쓰고, 이후에는 그 경로를 재사용한다.
_ENDPOINTS = [
    f"{_BASE}/getAnnouncementInformation01",
    f"{_BASE}/getAnnouncementInformation",
]
_working_endpoint: str | None = None


def _extract_rows(payload) -> list[dict]:
    """{"data": [...]} / {"response": {"body": {"items": [...]}}} 양쪽을 모두 처리."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "item", "list"):
        v = payload.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
        if isinstance(v, dict):
            inner = _extract_rows(v)
            if inner:
                return inner
    for key in ("response", "body", "result"):
        if isinstance(payload.get(key), dict):
            inner = _extract_rows(payload[key])
            if inner:
                return inner
    return []


def _call(page: int, per_page: int) -> dict | list:
    """엔드포인트 후보를 순서대로 시도하고, 성공한 경로를 기억한다."""
    global _working_endpoint
    key = keys.datagokr_key()
    if not key:
        raise RuntimeError("datagokr_key.txt 에 공공데이터포털 인증키가 없습니다.")

    params = {"serviceKey": key, "page": page, "perPage": per_page,
              "returnType": "json"}
    candidates = [_working_endpoint] if _working_endpoint else _ENDPOINTS
    last_error: Exception | None = None
    for url in candidates:
        try:
            payload = apiutil.get_json(url, params)
        except Exception as e:                 # 404/헤더 오류 → 다음 후보
            last_error = e
            continue
        _working_endpoint = url
        return payload
    raise RuntimeError(f"K-Startup API 호출 실패 — {last_error}")


def to_notice(row: dict) -> Notice | None:
    """응답 1행 → 공용 Notice.

    '신청 제외대상'(aply_excl_trgt_ctnt)은 기업마당에는 없고 K-Startup에만 있는데,
    자격 판정에서 '불가'를 확정 짓는 가장 강력한 근거라 반드시 살려서 넘긴다.
    """
    source_id = apiutil.pick(row, "pbanc_sn", "pbancSn", "id")
    title = apiutil.pick(row, "biz_pbanc_nm", "bizPbancNm", "intg_pbanc_biz_nm",
                         "pbanc_nm", "title")
    if not source_id or not title:
        return None

    begin = apiutil.parse_date(
        apiutil.pick(row, "pbanc_rcpt_bgng_dt", "pbancRcptBgngDt"))
    end = apiutil.parse_date(
        apiutil.pick(row, "pbanc_rcpt_end_dt", "pbancRcptEndDt"))

    # 신청대상 관련 필드가 여러 개로 쪼개져 오므로 합쳐서 판정 재료로 쓴다.
    # 값이 "3년미만,5년미만,7년미만,10년미만" 처럼 **선택지 나열**로 오는 필드가 있어
    # (biz_enyy, biz_trgt_age) 라벨을 붙여 준다. 라벨 없이 넘기면 자격 판정이 이걸
    # 조건 문장으로 오인한다.
    target_parts = [
        apiutil.pick(row, "aply_trgt_ctnt", "aplyTrgtCtnt"),
        apiutil.pick(row, "aply_trgt", "aplyTrgt"),
    ]
    for label, *names in (("신청 가능 업력", "biz_enyy", "bizEnyy"),
                          ("대상 연령", "biz_trgt_age", "bizTrgtAge"),
                          ("우대사항", "prfn_matr", "prfnMatr")):
        value = apiutil.pick(row, *names)
        if value:
            target_parts.append(f"{label}: {value}")

    return Notice(
        source=SOURCE,
        source_id=source_id,
        title=title,
        agency=apiutil.pick(row, "pbanc_ntrp_nm", "biz_prch_dprt_nm",
                            "bizPrchDprtNm", "spnsr_organ_nm"),
        url=apiutil.pick(row, "detl_pg_url", "detlPgUrl", "biz_gdnc_url"),
        summary=apiutil.pick(row, "pbanc_ctnt", "pbancCtnt", "biz_gdnc_url"),
        target_text="\n".join(p for p in target_parts if p),
        exclude_text=apiutil.pick(row, "aply_excl_trgt_ctnt", "aplyExclTrgtCtnt"),
        support_field=apiutil.pick(row, "supt_biz_clsfc", "suptBizClsfc"),
        region_text=apiutil.pick(row, "supt_regin", "suptRegin"),
        apply_begin=begin,
        apply_end=end,
        # K-Startup에는 제출서류 목록 필드가 없다. 신청 '방법'만 있으므로 그것만 담고,
        # 연락처(prch_cnpl_no)는 절대 넣지 않는다 — 전화번호가 제출서류 목록으로
        # 파싱돼서 "010-…을 준비하세요" 같은 엉뚱한 점검 결과가 나온다.
        docs_text=apiutil.pick(row, "aply_mthd_etc_istc", "aply_mthd_vst_rcpt_istc",
                               "aply_mthd_eml_rcpt_istc", "aplyMthdEtcCtnt"),
        raw=row,
    )


def fetch(per_page: int = 100, max_pages: int = 5) -> list[Notice]:
    """공고를 수집한다. 키가 없거나 호출이 실패하면 RuntimeError."""
    seen: dict[str, Notice] = {}
    for page in range(1, max_pages + 1):
        rows = _extract_rows(_call(page, per_page))
        if not rows:
            break
        before = len(seen)
        for row in rows:
            n = to_notice(row)
            if n:
                seen.setdefault(n.id, n)
        if len(seen) == before:
            break
    return list(seen.values())


def fetch_raw_sample(n: int = 1) -> list[dict]:
    """--dry-run 용: 응답 원본 몇 행 (실제 필드명 확인이 목적)."""
    return _extract_rows(_call(1, max(n, 5)))[:n]

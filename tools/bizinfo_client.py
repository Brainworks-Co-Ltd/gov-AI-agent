"""기업마당(bizinfo.go.kr) 지원사업정보 오픈API 어댑터.

공식 명세 (https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi):
    GET https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do
    요청: crtfcKey(필수), dataType(rss|json), searchCnt, searchLclasId,
          hashtags, pageUnit, pageIndex
    응답: pblancId, pblancNm, pblancUrl, jrsdInsttNm, reqstBeginEndDe,
          bsnsSumryCn, pldirSportRealmLclasCodeNm, trgetNm, creatPnttm,
          reqstMthPapersCn

※ 과제 규칙상 수집은 이 공식 오픈API로만 한다 — 누리집 직접 크롤링 금지.
"""
from __future__ import annotations

from agent.schemas import Notice
from tools import apiutil, keys

SOURCE = "기업마당"
API_URL = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"

# 기업마당 분야 코드 (searchLclasId). 전자부품 제조업 담당자가 주로 보는 분야를
# 기본값으로 둔다 — 전체를 긁으면 응답이 크고 판정할 것도 많아진다.
FIELD_CODES = {
    "01": "금융", "02": "기술", "03": "인력", "04": "수출", "05": "내수",
    "06": "창업", "07": "경영", "08": "제도", "09": "기타",
}


def _extract_rows(payload) -> list[dict]:
    """응답 껍데기에서 공고 배열만 꺼낸다.

    기업마당 JSON은 보통 {"jsonArray": [...]} 형태지만, 껍데기 이름이 바뀐 사례가
    있어 배열이 들어 있는 첫 번째 키를 찾는 방식으로 방어한다.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("jsonArray", "items", "item", "list", "data"):
        v = payload.get(key)
        if isinstance(v, list):
            return [r for r in v if isinstance(r, dict)]
    for v in payload.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
        if isinstance(v, dict):
            inner = _extract_rows(v)
            if inner:
                return inner
    return []


def _attachments(row: dict) -> list[dict]:
    """공고 첨부파일 목록을 뽑는다.

    기업마당은 첨부를 두 쌍으로 준다.
        fileNm / flpthNm            신청서 **서식** (한글 양식 등)  — 388/500건
        printFileNm / printFlpthNm  **공고문** 본문 파일             — 500/500건

    한 공고에 파일이 여러 개면 이름도 경로도 `@`로 이어 붙여서 한 문자열로 온다
    (실제로 388건 중 171건). 같은 자리끼리 짝지어야 이름과 링크가 어긋나지 않는다.

        fileNm  : "신청서류(서식).hwpx@농업발전기금 융자시행계획(안).pdf"
        flpthNm : "...atchFileId=FILE_...&fileSn=0@...atchFileId=FILE_...&fileSn=1"

    '서식'을 따로 구분해 두는 게 이 함수의 요점이다. 담당자가 초안을 옮겨 담을 대상이
    바로 그 파일이라, 화면에서 눈에 띄게 보여줘야 한다.
    """
    found: list[dict] = []
    for kind, name_field, path_field in (("서식", "fileNm", "flpthNm"),
                                         ("공고문", "printFileNm", "printFlpthNm")):
        names = [n.strip() for n in apiutil.clean_text(row.get(name_field)).split("@")]
        paths = [p.strip() for p in apiutil.clean_text(row.get(path_field)).split("@")]
        for name, url in zip(names, paths):
            if name and url.startswith("http"):
                found.append({"kind": kind, "name": name, "url": url})
    return found


def to_notice(row: dict) -> Notice | None:
    """응답 1행 → 공용 Notice. 공고ID나 제목이 없으면 버린다."""
    source_id = apiutil.pick(row, "pblancId", "pblancSn", "pblnId")
    title = apiutil.pick(row, "pblancNm", "pblancNm1")
    if not source_id or not title:
        return None

    begin, end = apiutil.parse_date_range(
        apiutil.pick(row, "reqstBeginEndDe", "reqstBeginEndDe1"))

    url = apiutil.pick(row, "pblancUrl", "rceptEngnHmpgUrl")
    # 기업마당은 상대 경로("/cmm/fms/...")를 주는 경우가 있다.
    if url.startswith("/"):
        url = "https://www.bizinfo.go.kr" + url

    return Notice(
        source=SOURCE,
        source_id=source_id,
        title=title,
        agency=apiutil.pick(row, "jrsdInsttNm", "excInsttNm"),
        url=url,
        summary=apiutil.pick(row, "bsnsSumryCn"),
        target_text=apiutil.pick(row, "trgetNm"),
        support_field=apiutil.pick(row, "pldirSportRealmLclasCodeNm"),
        apply_begin=begin,
        apply_end=end,
        docs_text=apiutil.pick(row, "reqstMthPapersCn"),
        attachments=_attachments(row),
        raw=row,
    )


def fetch(field_codes: list[str] | None = None, page_unit: int = 100,
          max_pages: int = 5) -> list[Notice]:
    """공고를 수집한다. 키가 없으면 RuntimeError (호출부가 캐시로 폴백).

    searchCnt=0(전체)은 응답이 매우 커서 타임아웃 위험이 있다. pageUnit/pageIndex로
    나눠 받고, 더 이상 새 공고ID가 안 나오면 조기 종료한다.
    """
    key = keys.bizinfo_key()
    if not key:
        raise RuntimeError("bizinfo_key.txt 에 기업마당 인증키가 없습니다.")

    codes = field_codes if field_codes is not None else [None]  # None = 전체 분야
    seen: dict[str, Notice] = {}
    for code in codes:
        for page in range(1, max_pages + 1):
            payload = apiutil.get_json(API_URL, {
                "crtfcKey": key,
                "dataType": "json",
                "searchLclasId": code,
                "pageUnit": page_unit,
                "pageIndex": page,
            })
            rows = _extract_rows(payload)
            if not rows:
                break
            before = len(seen)
            for row in rows:
                n = to_notice(row)
                if n:
                    seen.setdefault(n.id, n)
            if len(seen) == before:   # 새로 들어온 게 없으면 같은 페이지 반복 중
                break
    return list(seen.values())


def fetch_raw_sample(n: int = 1) -> list[dict]:
    """--dry-run 용: 응답 원본 몇 행을 그대로 돌려준다 (필드명 눈으로 확인)."""
    key = keys.bizinfo_key()
    if not key:
        raise RuntimeError("bizinfo_key.txt 에 기업마당 인증키가 없습니다.")
    payload = apiutil.get_json(API_URL, {
        "crtfcKey": key, "dataType": "json", "pageUnit": max(n, 5), "pageIndex": 1})
    return _extract_rows(payload)[:n]

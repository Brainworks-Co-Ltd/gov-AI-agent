"""웹 서버 — 표준 라이브러리 http.server만 사용 (pip install 불필요).

실행:
    python serve.py            그 다음 브라우저에서 http://localhost:8000
종료: Ctrl+C

**API 키는 절대 브라우저로 내려보내지 않는다.** 하이퍼클로바X 호출도, 오픈API
호출도 전부 이 서버가 대신한다. 키는 서버 쪽 *_key.txt 파일(또는 환경변수)에만
존재한다.

수집·판정·초안은 LLM과 외부 API를 부르는 느린 작업이라, 화면이 뜨는 순간에는
아무것도 부르지 않는다. 담당자가 버튼을 눌렀을 때만 움직인다 — 안 그러면 첫 화면이
멈춘 것처럼 보인다.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from agent import editor, orchestrator, recommend
from agent.schemas import CompanyProfile, Draft, DraftSection
from tools import (hyperclova_api, keys, past_store, profile_store, refresh,
                   store)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 배포 플랫폼(Render 등)은 $PORT 로 포트를 지정하고 0.0.0.0 바인딩을 요구한다.
# 환경변수가 없으면 예전처럼 8000번 루프백으로만 뜬다 — 로컬에서 이 PC 밖으로
# 열리는 일이 없어야 하기 때문이다.
PORT = int(os.environ.get("PORT", 8000))
_HOSTED = "PORT" in os.environ
_WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
# web/ 폴더 안의 파일만 정적으로 내보낸다 (그 외 경로는 파일시스템에 접근하지 않음).
_STATIC_TYPES = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".woff2": "font/woff2"}

# 판정 순 정렬. 담당자가 먼저 볼 것부터 — 가능 → 확인필요 → 불가 → 미판정.
_VERDICT_SORT = {"가능": 0, "확인필요": 1, "불가": 2}
_REFRESH = refresh.RefreshCoordinator()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass                    # 요청마다 콘솔에 찍지 않는다 (조용히 동작)

    # ─────────────────────────────────────────────────────────── 공통 도구

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _static(self, path: str) -> bool:
        name = os.path.basename(path) or "index.html"
        ext = os.path.splitext(name)[1]
        if ext not in _STATIC_TYPES:
            return False
        full = os.path.join(_WEB_DIR, name)      # basename만 써서 상위 폴더 접근 차단
        if not os.path.isfile(full):
            return False
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _STATIC_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        # 캐시 헤더가 없으면 브라우저가 제멋대로 오래 들고 있어서, 배포한 뒤에도
        # 옛 화면이 그대로 보인다. 파일 몇 개짜리 데모라 매번 새로 받게 한다.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)
        return True

    # ─────────────────────────────────────────────────────────────── GET

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            path = "/index.html"
        try:
            if path.startswith("/api/"):
                self._api_get(path, parse_qs(parsed.query))
                return
            if self._static(path):
                return
            self.send_error(404, "Not Found")
        except Exception:
            traceback.print_exc()
            self._json({"error": "서버 처리 중 오류가 발생했습니다."}, 500)

    def _api_get(self, path: str, query: dict) -> None:
        if path == "/api/status":
            self._json({
                "ai": hyperclova_api.is_configured(),
                "bizinfo": keys.bizinfo_key() is not None,
                "datagokr": keys.datagokr_key() is not None,
            })
            return

        if path == "/api/profile":
            self._json(profile_store.load().to_dict())
            return

        if path == "/api/past-applications":
            self._json({"items": past_store.listing()})
            return

        if path == "/api/refresh":
            self._json(_REFRESH.status())
            return

        conn = store.connect()
        try:
            if path == "/api/notices":
                self._json(self._notices(conn, query))
                return

            notice_id, tail = _split_notice_path(path)
            if notice_id:
                notice = store.get_notice(conn, notice_id)
                if notice is None:
                    self._json({"error": "공고를 찾을 수 없습니다."}, 404)
                    return
                if tail == "":
                    self._json(orchestrator.notice_view(conn, notice))
                    return
                if tail == "eligibility":
                    refresh = query.get("refresh", ["0"])[0] == "1"
                    report = orchestrator.eligibility_of(conn, notice, refresh=refresh)
                    data = report.to_dict()
                    # 제출서류 목록은 오픈API 응답에 사실상 없고 첨부(공고문·서식) 안에
                    # 있다. 그 추출은 파일을 내려받아 여는 비싼 작업이라 판정 자체에는
                    # 넣지 않는다(공고함 일괄 판정이 느려진다). 담당자가 공고 하나를
                    # 열어 볼 때, 즉 여기서만 합친다.
                    spec = orchestrator.form_spec_of(conn, notice)
                    docs = list(data.get("required_docs", []))
                    for doc in spec.get("documents", []):
                        if doc not in docs:
                            docs.append(doc)
                    data["required_docs"] = docs
                    data["docs_source"] = spec.get("source_file", "")
                    self._json(data)
                    return
                if tail == "draft":
                    saved = store.get_draft(conn, notice.id)
                    self._json(saved or {"draft": None, "issues": []})
                    return
                if tail == "checklist":
                    self._json(orchestrator.checklist_of(conn, notice))
                    return
            self._json({"error": "알 수 없는 경로입니다."}, 404)
        finally:
            conn.close()

    def _filtered(self, conn, query: dict, skip_verdict: bool = False) -> list[dict]:
        """필터가 적용된 공고함 목록.

        **사업 단위**(중복 통합 후 대표 1건)로 내보낸다 — 목록에 기업마당 공고와
        K-Startup 공고가 나란히 뜨면 중복을 없앤 의미가 사라진다.
        필터를 서버에서 처리해야 화면 코드가 단순해지고, '일괄 판정'이 지금 보이는
        목록과 정확히 같은 대상을 돌 수 있다.
        """
        include_closed = query.get("closed", ["0"])[0] == "1"
        notices = store.open_businesses(conn, include_closed=include_closed)

        profile = profile_store.load()
        verdicts = orchestrator.valid_verdicts(conn, notices, profile)
        items = [orchestrator.notice_view(conn, n, verdicts) for n in notices]

        region = query.get("region", [""])[0].strip()
        if region:
            items = [i for i in items
                     if region in (i.get("region_text") or "")
                     or region in (i.get("title") or "")
                     or region in (i.get("agency") or "")]
        within = query.get("within", [""])[0].strip()
        if within.isdigit():
            limit = int(within)
            items = [i for i in items
                     if i.get("d_day") is not None and 0 <= i["d_day"] <= limit]
        verdict = query.get("verdict", [""])[0].strip()
        if verdict and not skip_verdict:
            items = [i for i in items if i.get("verdict") == verdict]

        # 키워드 검색. 낱말을 모두 포함해야 통과시킨다 — "광주 제조" 처럼 좁혀 가는
        # 쪽이 "둘 중 아무거나"보다 목록을 줄이는 데 쓸모 있다.
        q = query.get("q", [""])[0].strip().lower()
        if q:
            words = q.split()
            items = [i for i in items
                     if all(w in (f"{i.get('title') or ''} {i.get('agency') or ''} "
                                  f"{i.get('summary') or ''}").lower() for w in words)]

        # 정렬. 기본(마감 임박순)은 store.open_businesses 가 이미 해 두었다.
        # 파이썬 sort 는 안정 정렬이라 값이 같으면 그 순서가 남는다.
        sort = query.get("sort", [""])[0].strip()
        if sort == "far":
            # 마감이 먼 것부터. 마감 미상은 여기서도 맨 뒤에 둔다.
            items.sort(key=lambda i: (i.get("d_day") is None, -(i.get("d_day") or 0)))
        elif sort == "recent":
            # 접수를 늦게 시작한 것부터. first_seen 은 전건이 수집일이라 못 쓴다.
            items.sort(key=lambda i: i.get("apply_begin") or "", reverse=True)
        elif sort == "verdict":
            items.sort(key=lambda i: (_VERDICT_SORT.get(i.get("verdict"), 3),
                                      i.get("d_day") is None, i.get("d_day") or 0))
        return items

    def _notices(self, conn, query: dict) -> dict:
        # 집계는 **판정 필터를 걸기 전** 목록에서 낸다. 걸린 뒤에 세면 '가능'을 고른
        # 순간 나머지가 0으로 보여, 화면의 집계를 눌러 다른 판정으로 옮겨갈 수 없다.
        base = self._filtered(conn, query, skip_verdict=True)
        tally = {"가능": 0, "확인필요": 0, "불가": 0}
        for i in base:
            if i.get("verdict") in tally:
                tally[i["verdict"]] += 1
        pending = len(base) - sum(tally.values())

        verdict = query.get("verdict", [""])[0].strip()
        items = [i for i in base if i.get("verdict") == verdict] if verdict else base

        # 추천은 **지금 걸린 필터 안에서** 고른다. 화면에 안 보이는 공고를 추천하면
        # 눌렀을 때 목록에서 못 찾아 혼란스럽다.
        profile = profile_store.load()
        verdicts = {i["id"]: i["verdict"] for i in items if i.get("verdict")}
        by_id = {i["id"]: i for i in items}
        picks = recommend.top(
            [store.get_notice(conn, i["id"]) for i in items[:400]],
            profile, verdicts, limit=5)
        for p in picks:
            p["item"] = by_id.get(p["notice_id"])

        return {"items": items, "total": len(items),
                "judged": sum(tally.values()), "tally": tally,
                # 집계는 필터 전 기준이라 목록 길이와 다를 수 있다. 화면이 뱃지를
                # 그릴 때 이 값을 쓰고, total 은 '지금 보고 있는 건수'로 둔다.
                "scope": len(base), "pending": pending,
                "recommended": [p for p in picks if p["item"]]}

    # ────────────────────────────────────────────────────────────── POST

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/profile":
                self._save_profile()
                return
            if path == "/api/ingest":
                body = self._body()
                self._json(_REFRESH.start(
                    collect_first=True, use_llm=body.get("use_llm", True),
                    offline=body.get("offline", False)), 202)
                return
            if path == "/api/past-applications":
                # 파일을 그대로 본문으로 받는다. multipart 를 손으로 파싱하지 않으려고
                # 이름은 헤더(X-Filename)에 담아 보낸다 — 표준 라이브러리 서버라
                # multipart 파서가 없고, 직접 짜면 경계 문자열 처리에서 사고가 난다.
                length = int(self.headers.get("Content-Length") or 0)
                if length > 20 * 1024 * 1024:
                    self._json({"ok": False, "note": "파일이 너무 큽니다(20MB 초과)."}, 413)
                    return
                filename = unquote(self.headers.get("X-Filename") or "")
                data = self.rfile.read(length) if length else b""
                if not filename or not data:
                    self._json({"ok": False, "note": "파일을 받지 못했습니다."}, 400)
                    return
                result = past_store.save(filename, data)
                result["items"] = past_store.listing()
                self._json(result)
                return

            if path == "/api/past-applications/delete":
                name = str(self._body().get("name", ""))
                ok = past_store.remove(name)
                self._json({"ok": ok, "items": past_store.listing()})
                return

            if path == "/api/refresh/stop":
                # 진행 중인 갱신을 멈춘다. 이미 끝난 판정은 저장돼 있어,
                # 다시 시작하면 남은 것부터 이어서 돈다.
                self._json(_REFRESH.stop())
                return
            if path == "/api/judge":
                self._json(_REFRESH.start(collect_first=False), 202)
                return

            conn = store.connect()
            try:
                notice_id, tail = _split_notice_path(path)
                notice = store.get_notice(conn, notice_id) if notice_id else None
                if notice is None:
                    self._json({"error": "공고를 찾을 수 없습니다."}, 404)
                    return
                if tail == "draft":
                    body = self._body()
                    # 담당자가 직접 고친 항목 목록. 첨부에서 서식을 못 읽었거나
                    # 읽어낸 항목이 실제 양식과 다를 때, 여기로 넘겨 다시 쓴다.
                    sections = [str(s).strip() for s in (body.get("sections") or [])
                                if str(s).strip()]
                    draft = orchestrator.draft_of(
                        conn, notice, refresh=body.get("refresh", False),
                        sections=sections or None)
                    self._json(draft.to_dict())
                    return
                if tail == "save":
                    # 담당자가 textarea 에 직접 친 글을 저장한다. 지금까지는
                    # save_edited_draft 가 대화 경로에서만 불려서, 손으로 고치고
                    # 탭을 옮기거나 새로고침하면 그대로 날아갔다.
                    body = self._body()
                    sections = [s for s in (body.get("sections") or [])
                                if isinstance(s, dict)]
                    if not sections:
                        self._json({"error": "저장할 초안이 없습니다."}, 400)
                        return
                    # 점검에서 나온 '확인 필요' 목록은 본문을 손봤다고 사라지지
                    # 않는다. 저장돼 있던 것을 그대로 이어 쓴다.
                    saved = store.get_draft(conn, notice.id) or {}
                    unresolved = (saved.get("draft") or {}).get("unresolved") or []
                    orchestrator.save_edited_draft(conn, notice, sections, unresolved)
                    self._json({"saved": len(sections)})
                    return
                if tail == "chat":
                    # 대화로 초안 고치기. **화면에 보이는 글**을 받아서 고친다 —
                    # 저장된 초안을 고치면 담당자가 방금 손으로 쓴 문장이 날아간다.
                    body = self._body()
                    sections = [s for s in (body.get("sections") or [])
                                if isinstance(s, dict)]
                    if not sections:
                        self._json({"error": "고칠 초안이 없습니다."}, 400)
                        return
                    spec = orchestrator.form_spec_of(conn, notice)
                    result = editor.chat(
                        notice, profile_store.load(), sections,
                        str(body.get("message") or ""),
                        history=body.get("history") or [],
                        notice_text=spec.get("notice_text", ""),
                        target=str(body.get("target") or ""))
                    # 고친 결과를 저장해 둔다. 화면을 닫았다 열어도 남아 있어야 한다.
                    if result.get("changed"):
                        orchestrator.save_edited_draft(conn, notice, result["sections"],
                                                       result.get("unresolved") or [])
                    self._json(result)
                    return
                if tail == "checklist":
                    # 담당자가 체크한 상태를 그대로 저장한다. 서류를 실제로 뗐는지는
                    # 사람만 아는 정보라 도구가 판단하지 않는다.
                    checks = {str(k): bool(v)
                              for k, v in (self._body().get("checks") or {}).items()}
                    store.save_doc_checks(conn, notice.id, checks)
                    self._json(orchestrator.checklist_of(conn, notice))
                    return
                if tail == "check":
                    draft = _draft_from_body(self._body(), notice.id) \
                        or orchestrator.draft_of(conn, notice)
                    issues = orchestrator.check_of(conn, notice, draft)
                    self._json({"issues": [i.__dict__ for i in issues]})
                    return
                self._json({"error": "알 수 없는 경로입니다."}, 404)
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            self._json({"error": "서버 처리 중 오류가 발생했습니다."}, 500)

    def _save_profile(self) -> None:
        """프로필 저장. 판정 캐시는 프로필 해시가 바뀌면 자동으로 무효가 된다."""
        data = self._body()
        current = profile_store.load().to_dict()
        current.pop("years", None)
        current.update({k: v for k, v in data.items() if k != "years"})
        profile = CompanyProfile.from_dict(current)
        profile_store.save(profile)
        self._json(profile.to_dict())


def _split_notice_path(path: str) -> tuple[str | None, str]:
    """/api/notices/<공고ID>/<동작> 을 (공고ID, 동작)으로 나눈다.

    공고ID는 "기업마당:PBLN-000..." 처럼 콜론과 한글이 들어 있어 URL 인코딩된 채로
    온다. 그래서 마지막 조각만 동작으로 떼어내고 나머지를 디코딩한다.
    """
    prefix = "/api/notices/"
    if not path.startswith(prefix):
        return None, ""
    rest = path[len(prefix):]
    if not rest:
        return None, ""
    known = ("eligibility", "draft", "check", "checklist", "chat", "save")
    head, _, tail = rest.rpartition("/")
    if tail in known and head:
        return unquote(head), tail
    return unquote(rest.rstrip("/")), ""


def _draft_from_body(body: dict, notice_id: str) -> Draft | None:
    """화면에서 담당자가 고친 초안을 그대로 받아 점검한다.

    점검 버튼은 '저장된 초안'이 아니라 '지금 화면에 보이는 글'을 봐야 한다.
    안 그러면 방금 고친 숫자가 점검에 반영되지 않는다.
    """
    sections = body.get("sections")
    if not isinstance(sections, list) or not sections:
        return None
    return Draft(
        notice_id=notice_id,
        sections=[DraftSection(title=str(s.get("title", "")),
                               body=str(s.get("body", "")),
                               sources=list(s.get("sources") or []))
                  for s in sections],
        unresolved=list(body.get("unresolved") or []),
        note=str(body.get("note") or ""))


class _Server(ThreadingHTTPServer):
    """IPv4 루프백 서버.

    allow_reuse_address 를 끄는 게 핵심이다. 파이썬 기본값(True)은 윈도우에서
    SO_REUSEADDR로 동작해서, **이미 서버가 떠 있는데도 두 번째 인스턴스가 조용히 같은
    포트에 함께 붙는다.** 그러면 요청이 옛 프로세스로도 가서, 코드를 고치고 다시 켰는데
    바뀐 게 반영 안 된 것처럼 보인다(실제로 그렇게 헤맸다). 꺼 두면 "이미 사용 중"
    이라고 곧바로 알려 준다.
    """
    allow_reuse_address = False


class _IPv6Server(_Server):
    """IPv6 루프백(::1) 전용 서버."""
    address_family = socket.AF_INET6


class _HostedServer(ThreadingHTTPServer):
    """배포 환경(0.0.0.0)용 서버.

    여기서는 allow_reuse_address 를 켜 둔다. 로컬에서 끄는 이유(같은 포트에 두 번째
    인스턴스가 조용히 붙는 사고)는 컨테이너에 없고, 반대로 재배포 직후 이전 소켓이
    TIME_WAIT 에 남아 바인딩이 실패하는 쪽이 실제 위험이다.
    """
    allow_reuse_address = True


def _start_servers() -> list[ThreadingHTTPServer]:
    """로컬은 IPv4(127.0.0.1)·IPv6(::1) 양쪽, 배포 환경은 0.0.0.0 하나에 띄운다.

    윈도우에서 브라우저에 http://localhost:8000 을 치면 `localhost`가 IPv6 `::1`로
    먼저 해석된다. 127.0.0.1에만 붙여 두면 그 시도가 연결 거부로 끝나서, 서버는
    분명히 떠 있는데 브라우저에서는 안 열리는 상황이 생긴다(실제로 그랬다).

    두 주소에 각각 붙이고, 둘 다 루프백이라 이 PC 밖에서는 접속할 수 없다.
    한쪽 바인딩이 실패해도(IPv6가 꺼져 있는 환경 등) 나머지 하나로 계속 동작한다.
    """
    targets = (((_HostedServer, "0.0.0.0"),) if _HOSTED
               else ((_Server, "127.0.0.1"), (_IPv6Server, "::1")))
    servers: list[ThreadingHTTPServer] = []
    for cls, host in targets:
        try:
            servers.append(cls((host, PORT), Handler))
        except OSError as e:
            if not servers:                 # 첫 바인딩부터 실패하면 원인을 알려야 한다
                print(f"  [오류] {host}:{PORT} 에 서버를 띄우지 못했습니다 — {e}")
                print(f"         이미 서버가 떠 있을 수 있습니다. 그 창에서 Ctrl+C 로 "
                      f"끄고 다시 실행하세요.")
            else:
                print(f"  [알림] {host} 는 사용할 수 없어 건너뜁니다. ({e})")
    return servers


def main() -> None:
    conn = store.connect()      # DB 파일·테이블을 미리 만들어 둔다
    count = len(store.all_notices(conn))
    conn.close()

    print(f"\n  정부지원사업 공고 분석·신청서 초안 도구")
    print(f"  {'-' * 52}")
    print(f"  하이퍼클로바X : {'연결됨' if hyperclova_api.is_configured() else '미설정 (규칙 판정으로 동작)'}")
    print(f"  기업마당 API  : {'연결됨' if keys.bizinfo_key() else '미설정'}")
    print(f"  K-Startup API : {'연결됨' if keys.datagokr_key() else '미설정'}")
    print(f"  저장된 공고    : {count}건")
    print(f"  {'-' * 52}")

    servers = _start_servers()
    if not servers:
        return

    if _HOSTED:
        print(f"  0.0.0.0:{PORT} 에서 대기 중입니다.\n")
    else:
        print(f"  브라우저에서 아래 주소를 여세요. (종료: Ctrl+C)")
        print(f"     http://localhost:{PORT}")
        print(f"     http://127.0.0.1:{PORT}   (localhost 가 안 열릴 때)\n")

    # 첫 서버는 이 스레드에서 돌리고, 나머지는 배경 스레드에 맡긴다.
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\n  종료합니다.\n")
    finally:
        for extra in servers[1:]:
            extra.shutdown()        # 배경 스레드의 serve_forever 를 멈춘다
        for s in servers:
            s.server_close()


if __name__ == "__main__":
    main()

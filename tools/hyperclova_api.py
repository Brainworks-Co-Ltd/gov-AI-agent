"""하이퍼클로바X(CLOVA Studio) 실제 호출 부품.

네이버클라우드 CLOVA Studio Chat Completions v3 REST API를 부른다.
하이퍼클로바X는 네이버 서버에서 돌아가므로, 우리는 인터넷으로 요청만 보낸다
(우리 GPU 불필요). 표준 라이브러리(urllib)만 사용 — 추가 설치 없음.

API 키는 프로젝트 폴더의 clova_api_key.txt 파일에서 읽는다.
그 파일은 절대 외부에 공유하지 말 것 (.gitignore로 제외됨).
"""
from __future__ import annotations
import json
import os
import time
import uuid
import urllib.request
import urllib.error

# 사용할 모델. 테스트 앱에서 다른 모델만 켜져 있으면 이 값을 바꾼다.
#   - HCX-DASH-002 : 가볍고 빠름 (테스트에 적합) — Structured Outputs 미지원
#   - HCX-005      : 상위 모델(이미지 이해 가능)
MODEL = "HCX-DASH-002"
API_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions/" + MODEL

# Structured Outputs(JSON 스키마 강제) 전용 모델 — DASH-002는 responseFormat을 지원하지
# 않아 400 에러가 난다(직접 테스트 확인). HCX-007만 지원, thinking을 꺼야 같이 쓸 수 있다.
STRUCTURED_MODEL = "HCX-007"
STRUCTURED_API_URL = "https://clovastudio.stream.ntruss.com/v3/chat-completions/" + STRUCTURED_MODEL

_KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "clova_api_key.txt")
_PLACEHOLDER = "여기에-발급받은-API-키를-붙여넣고-저장하세요"

# ── 튜닝(학습)한 모델 ────────────────────────────────────────────────────
# CLOVA Studio '학습' 메뉴에서 튜닝을 끝내면 작업 ID(taskId)가 나온다. 그 ID를
# clova_tuned_task.txt 에 적어 두면 신청서 초안 생성에 그 모델을 쓴다.
#
# 호출 경로가 기본 모델과 다르다:
#     기본   /v3/chat-completions/{모델명}
#     튜닝   /v3/tasks/{taskId}/chat-completions
#
# **튜닝 모델은 Structured Outputs(responseFormat)를 지원하지 않는다**
# (공식 문서: "이미지 입력, 추론, Function calling, Structured Outputs 미지원").
# 그래서 튜닝 모델을 쓸 때는 JSON 스키마로 강제하지 못하고 평문을 받아 파싱해야 한다
# — agent/drafter.py 가 그 경로를 따로 갖고 있다.
_TUNED_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "clova_tuned_task.txt")
TUNED_API_URL = "https://clovastudio.stream.ntruss.com/v3/tasks/{task_id}/chat-completions"


def _load_key() -> str | None:
    # 컨테이너 배포 시에는 이미지에 키 파일을 넣지 않고 환경변수로 주입한다
    # (docker run -e CLOVA_API_KEY=... ). 로컬 개발은 기존 파일 방식 그대로 동작.
    env_key = os.environ.get("CLOVA_API_KEY")
    if env_key:
        return env_key
    try:
        with open(_KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    except FileNotFoundError:
        return None
    # 안내 문구가 그대로 남아 있으면 '키 없음'으로 본다. 완전 일치가 아니라 접두사로
    # 보는 이유: 안내 문구를 조금 고쳐 두면 그 한글이 그대로 Authorization 헤더에 실려
    # 'latin-1 codec' 오류라는 엉뚱한 메시지로 터진다. tools/keys.py와 같은 규칙.
    if not key or key.startswith("여기에") or key == _PLACEHOLDER:
        return None
    return key


def is_configured() -> bool:
    """API 키가 준비돼 있으면 True (없으면 프로그램은 가짜 답으로 동작)."""
    return _load_key() is not None


def get_key() -> str | None:
    """다른 모듈(예: 임베딩 호출기)에서 같은 키를 재사용할 때 쓴다."""
    return _load_key()


def tuned_task_id() -> str | None:
    """튜닝한 모델의 작업 ID. 없으면 None (기본 모델을 쓴다)."""
    env = os.environ.get("CLOVA_TUNED_TASK_ID")
    if env and env.strip():
        return env.strip()
    try:
        with open(_TUNED_FILE, encoding="utf-8") as f:
            task_id = f.read().strip()
    except FileNotFoundError:
        return None
    if not task_id or task_id.startswith("여기에") or task_id.startswith("#"):
        return None
    return task_id


# 429(호출 한도 초과)를 만났을 때 기다렸다 다시 부르는 횟수와 간격.
#
# 초안은 항목마다 따로 부르고 짧으면 이어쓰기까지 하므로, 초안 한 건에 호출이 열 번
# 가까이 몰린다. 실제로 이 때문에 4개 항목 중 2개만 생성되고 나머지는 429로 조용히
# 빠졌다. 담당자 눈에는 "초안이 짧다"로만 보여서 원인을 알 수 없다.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_WAITS = (2.0, 5.0, 12.0)


def _post(url: str, body: dict, timeout: int = 60) -> dict:
    """CLOVA Studio 공통 호출부. 상태코드 20000만 성공으로 본다.

    호출 한도(429)와 일시적인 서버 오류(5xx)는 잠시 기다렸다 다시 부른다. 그 밖의
    오류(키 오류·잘못된 요청)는 다시 불러도 같은 결과라 바로 올린다.
    """
    key = _load_key()
    if not key:
        raise RuntimeError("clova_api_key.txt 에 API 키가 없습니다.")

    data = json.dumps(body).encode("utf-8")

    last: Exception | None = None
    for attempt in range(len(_RETRY_WAITS) + 1):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        # 요청 ID는 호출마다 새로 만든다 — 재시도까지 같은 ID로 보내면 중복 요청으로
        # 처리될 수 있다.
        req.add_header("X-NCP-CLOVASTUDIO-REQUEST-ID", uuid.uuid4().hex)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            last = RuntimeError(f"HTTP {e.code} — {detail}")
            if e.code not in _RETRY_STATUS or attempt == len(_RETRY_WAITS):
                raise last from None
            wait = _RETRY_WAITS[attempt]
            # 서버가 알려 주는 대기 시간이 있으면 그쪽을 따른다.
            try:
                wait = max(wait, float(e.headers.get("Retry-After") or 0))
            except (TypeError, ValueError):
                pass
            print(f"[알림] HTTP {e.code} — {wait:.0f}초 뒤 다시 시도합니다 "
                  f"({attempt + 1}/{len(_RETRY_WAITS)})")
            time.sleep(wait)
    else:                                   # pragma: no cover - 위 break로 끝난다
        raise last or RuntimeError("호출에 실패했습니다.")

    status = payload.get("status", {})
    if status.get("code") != "20000":
        raise RuntimeError(f"{status.get('code')}: {status.get('message')}")
    return payload


def chat_tuned(system: str, user: str, max_tokens: int = 1500,
               temperature: float = 0.3, task_id: str | None = None) -> str:
    """**튜닝한 모델**에 요청을 보내고 평문을 돌려받는다.

    Structured Outputs를 못 쓰므로 결과는 평문이다. 형식은 프롬프트로 정하고
    호출부(agent/drafter.py)가 파싱한다.
    """
    task_id = task_id or tuned_task_id()
    if not task_id:
        raise RuntimeError("clova_tuned_task.txt 에 튜닝 작업 ID가 없습니다.")

    payload = _post(TUNED_API_URL.format(task_id=task_id), {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "maxTokens": max_tokens,
        "temperature": temperature,
        "topP": 0.8,
        "repeatPenalty": 1.2,
    })
    return payload["result"]["message"]["content"]


def chat(system: str, user: str, max_tokens: int = 512,
         temperature: float = 0.2, model: str | None = None) -> str:
    """하이퍼클로바X에 요청을 보내고 생성된 텍스트를 돌려받는다.

    model 을 주면 그 모델로 부른다. HCX-007 계열은 v3 규격이 달라
    maxTokens 대신 maxCompletionTokens 를 쓰고 thinking 설정이 필요하다
    (maxTokens 로 부르면 400 Bad Request 가 난다).
    """
    key = _load_key()
    if not key:
        raise RuntimeError("clova_api_key.txt 에 API 키가 없습니다.")

    if model and model != MODEL:
        payload = _post(
            "https://clovastudio.stream.ntruss.com/v3/chat-completions/" + model,
            {"messages": [{"role": "system", "content": system},
                          {"role": "user", "content": user}],
             "maxCompletionTokens": max_tokens, "temperature": temperature,
             "topP": 0.8, "thinking": {"effort": "none"}}, timeout=90)
        return payload["result"]["message"]["content"]

    body = json.dumps({
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "maxTokens": max_tokens,
        "temperature": temperature,
        "topP": 0.8,
    }).encode("utf-8")

    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-NCP-CLOVASTUDIO-REQUEST-ID", uuid.uuid4().hex)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} — {detail}") from None

    # 상태 코드 20000 이 성공. 그 외에는 원인 메시지를 그대로 알려준다.
    status = data.get("status", {})
    if status.get("code") != "20000":
        raise RuntimeError(f"{status.get('code')}: {status.get('message')}")

    # 생성된 텍스트 위치: result.message.content
    return data["result"]["message"]["content"]


def chat_structured(system: str, user: str, schema: dict, max_tokens: int = 512,
                     temperature: float = 0.2) -> dict:
    """Structured Outputs(JSON 스키마 강제)로 호출한다.

    번호·머리기호·군더더기 문장이 섞여 나오는 문제를 프롬프트가 아니라 API 차원에서
    막는다 — 응답이 schema 형태의 JSON으로 강제되므로, 텍스트 파싱/정규식 후처리가
    필요 없다. HCX-007 전용(§STRUCTURED_MODEL 주석 참고).
    """
    key = _load_key()
    if not key:
        raise RuntimeError("clova_api_key.txt 에 API 키가 없습니다.")

    body = json.dumps({
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "responseFormat": {"type": "json", "schema": schema},
        "thinking": {"effort": "none"},  # Structured Outputs와 추론 모드는 병행 불가
        "maxCompletionTokens": max_tokens,
        "temperature": temperature,
        "topP": 0.8,
    }).encode("utf-8")

    req = urllib.request.Request(STRUCTURED_API_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-NCP-CLOVASTUDIO-REQUEST-ID", uuid.uuid4().hex)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} — {detail}") from None

    status = data.get("status", {})
    if status.get("code") != "20000":
        raise RuntimeError(f"{status.get('code')}: {status.get('message')}")

    content = data["result"]["message"]["content"]
    return json.loads(content)

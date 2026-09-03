"""오픈API 인증키 로딩 (파일 우선순위: 환경변수 → 프로젝트 폴더의 *_key.txt).

하이퍼클로바X 키를 다루는 tools/hyperclova_api.py와 같은 방식이다. 키를 코드나
웹 화면에 절대 넣지 않고 서버 쪽 파일/환경변수에만 두기 위한 공통 부품.
"""
from __future__ import annotations

import os
import urllib.parse

_ROOT = os.path.dirname(os.path.dirname(__file__))

# 파일 안에 안내 문구가 그대로 남아 있으면 '키 없음'으로 취급한다.
_PLACEHOLDER_MARK = "여기에"


def _read(filename: str, env_name: str) -> str | None:
    env = os.environ.get(env_name)
    if env:
        return env.strip()
    try:
        with open(os.path.join(_ROOT, filename), encoding="utf-8") as f:
            key = f.read().strip()
    except FileNotFoundError:
        return None
    if not key or key.startswith(_PLACEHOLDER_MARK):
        return None
    return key


def bizinfo_key() -> str | None:
    """기업마당 서비스 인증키 (요청 파라미터명 crtfcKey)."""
    return _read("bizinfo_key.txt", "BIZINFO_KEY")


def datagokr_key() -> str | None:
    """공공데이터포털 일반 인증키 (요청 파라미터명 serviceKey).

    포털은 같은 키를 Encoding형과 Decoding형 두 가지로 보여준다. 우리는 urlencode로
    파라미터를 만들기 때문에 **Decoding 키**가 필요하다. Encoding 키를 그대로 쓰면
    그 안의 `%2B` 같은 조각이 `%252B`로 이중 인코딩돼서, 키가 멀쩡한데도
    SERVICE_KEY_IS_NOT_REGISTERED_ERROR(403)가 난다.

    포털 화면에서 둘이 나란히 붙어 있어 헷갈리기 쉬우므로(실제로 이것 때문에 막혔다),
    어느 쪽을 붙여 넣어도 되도록 여기서 알아서 되돌린다. `%`가 들어 있고 디코딩했을 때
    문자열이 달라지면 Encoding 키로 보고 원래 값으로 풀어서 쓴다.
    """
    key = _read("datagokr_key.txt", "DATA_GO_KR_KEY")
    if not key:
        return None
    decoded = urllib.parse.unquote(key)
    return decoded if decoded != key else key

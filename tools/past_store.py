"""과거 사업계획서 보관 — 올린 파일을 열어 문항 단위 글로 바꿔 저장한다.

초안 품질은 참고 자료의 양에 정직하게 비례한다. 회사 프로필만 보고 쓰면 어느 회사에나
해당하는 밋밋한 글이 나오고, 실제로 제출했던 신청서가 있으면 그 회사의 말투와 사업
내용이 살아난다. 그래서 담당자가 갖고 있는 파일을 그대로 올릴 수 있게 한다.

파일을 그대로 두지 않고 **글로 풀어 저장하는** 이유: tools/past_search.py 가 문항
단위로 검색하기 때문이다. 새 공고의 '기대효과' 항목을 쓸 때 필요한 건 예전 신청서의
'기대효과' 문단이지 신청서 전체가 아니다.

hwp·hwpx·docx·zip 은 tools/formdoc.py 로 연다 — 공고 첨부를 읽으려고 만든 것을
그대로 쓴다.
"""
from __future__ import annotations

import os
import re
import unicodedata

from tools import formdoc

_PAST_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                         "data", "past_applications")

# 파일에서 뽑은 글에서 '문항 제목'으로 볼 줄. 번호·기호가 붙은 짧은 줄이 제목이다.
_HEADING_RE = re.compile(
    r"^\s*(?:[□■○●◇◆▶Ⅰ-Ⅹ]\s*|\(?\d{1,2}\s*[.)]\s*|제?\s*\d{1,2}\s*장\s*[.)]?\s*)"
    r"(?P<title>[가-힣A-Za-z][^\n:：]{1,30})\s*$")
# 표에서 풀린 줄(구분자 포함)이나 너무 긴 줄은 제목이 아니다.
_MAX_HEADING = 34


def _safe_name(name: str) -> str:
    """저장할 파일명. 경로 조작과 한글 정규화 문제를 함께 막는다.

    **원본 확장자를 이름에 남긴다.** 확장자만 떼면 'A.hwp'와 'A.zip'이 둘 다 'A.md'가
    되어 먼저 올린 파일을 조용히 덮어쓴다(실제로 그랬다). 같은 파일을 다시 올리면
    덮어쓰는 게 맞지만, 다른 파일끼리 부딪히면 안 된다.
    """
    filename = os.path.basename(name)
    base, _, ext = filename.rpartition(".")
    base = unicodedata.normalize("NFC", base or filename)
    base = re.sub(r"[^0-9A-Za-z가-힣 _\-()]", "", base).strip() or "과거_신청서"
    suffix = f"({ext.lower()})" if ext else ""
    return f"{base[:56]}{suffix}.md"


def to_markdown(title: str, text: str) -> str:
    """뽑아낸 글을 '## 문항제목' 구조의 마크다운으로 바꾼다.

    제목을 못 찾으면 통째로 한 덩어리가 된다. 그래도 쓸모는 있지만(내용 검색은 됨),
    문항별 검색이 안 되므로 담당자에게 직접 나눠 달라고 안내한다.
    """
    lines: list[str] = [f"# {title}", ""]
    found = 0
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            lines.append("")
            continue
        m = _HEADING_RE.match(line)
        if m and len(line.strip()) <= _MAX_HEADING and "|" not in line:
            lines.append("")
            lines.append(f"## {m.group('title').strip()}")
            found += 1
        else:
            lines.append(line)
    return "\n".join(lines).strip() + "\n", found


def save(filename: str, data: bytes) -> dict:
    """올린 파일을 열어 저장한다. 반환: {ok, name, chars, sections, note}"""
    text = ""
    if filename.lower().endswith((".md", ".txt")):
        for encoding in ("utf-8", "cp949", "utf-16"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    elif formdoc.is_supported(filename):
        text = formdoc.extract_text(filename, data)
    else:
        return {"ok": False, "note": f"'{filename}' 은 열 수 없는 형식입니다. "
                                     f"hwp·hwpx·docx·zip·txt·md 를 올려 주세요. "
                                     f"(PDF는 아직 읽지 못합니다)"}

    if not text.strip():
        return {"ok": False, "note": f"'{filename}' 에서 글자를 읽지 못했습니다. "
                                     f"스캔본이거나 이미지로만 된 문서일 수 있습니다."}

    title = os.path.basename(filename).rsplit(".", 1)[0]
    markdown, sections = to_markdown(title, text)

    os.makedirs(_PAST_DIR, exist_ok=True)
    name = _safe_name(filename)
    with open(os.path.join(_PAST_DIR, name), "w", encoding="utf-8") as f:
        f.write(markdown)

    note = f"{len(text):,}자를 읽었습니다."
    if sections:
        note += f" 문항 {sections}개로 나눴습니다."
    else:
        note += (" 문항 제목을 찾지 못해 한 덩어리로 저장했습니다 — "
                 "파일에서 '## 문항제목' 으로 나누면 초안이 더 정확해집니다.")
    return {"ok": True, "name": name, "chars": len(text),
            "sections": sections, "note": note}


def listing() -> list[dict]:
    """보관 중인 과거 신청서 목록. (안내용 README는 뺀다)"""
    if not os.path.isdir(_PAST_DIR):
        return []
    out = []
    for name in sorted(os.listdir(_PAST_DIR)):
        if not name.lower().endswith((".md", ".txt")):
            continue
        if name.lower().startswith(("readme", "_")):
            continue
        path = os.path.join(_PAST_DIR, name)
        with open(path, encoding="utf-8") as f:
            body = f.read()
        out.append({"name": name,
                    "chars": len(body),
                    "sections": body.count("\n## ")})
    return out


def remove(name: str) -> bool:
    """올린 파일을 지운다. 이름만 받고 경로는 절대 받지 않는다."""
    safe = os.path.basename(name)
    if not safe.lower().endswith((".md", ".txt")) or safe.lower().startswith("readme"):
        return False
    path = os.path.join(_PAST_DIR, safe)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True

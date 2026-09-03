"""공고 첨부파일(신청서 서식·공고문)에서 글자를 뽑아낸다 — 표준 라이브러리만.

왜 필요한가. 신청서 초안을 만들 때 '어떤 항목을 써야 하는지'는 공고 본문이 아니라
**첨부된 서식 파일** 안에 있다. 그걸 못 읽으면 항목을 추측할 수밖에 없고, 담당자는
결국 서식을 따로 열어 항목을 맞춰 옮겨 적어야 한다. 그 왕복을 없애는 게 목적이다.

지원 형식 (실제 수집한 서식 624개 기준):
    .hwpx  162건  ZIP + XML          → zipfile 로 연다
    .hwp   258건  OLE 복합문서(CFB)   → 아래 _CFB 로 직접 파싱 (표준 라이브러리에 리더가 없다)
    .docx    3건  ZIP + XML
    .zip    73건  안을 풀어 재귀
    .pdf   118건  **미지원** — 라이브러리 없이 신뢰할 만한 추출이 어렵다
"""
from __future__ import annotations

import html
import io
import os
import re
import struct
import urllib.request
import zipfile
import zlib

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "forms")

# 기관 서버가 기본 User-Agent를 막는 경우가 있어 브라우저처럼 요청한다.
# (HEAD는 403으로 막혀 있고 GET만 열려 있다 — 링크 점검에 HEAD를 쓰면 안 된다.)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

# 서식 파일은 보통 수백 KB다. 이보다 큰 건 사진이 잔뜩 든 자료집일 가능성이 높아
# 받지 않는다 — 초안 한 번 만들자고 수십 MB를 내려받을 이유가 없다.
MAX_BYTES = 12 * 1024 * 1024

SUPPORTED = (".hwpx", ".hwp", ".docx", ".zip")


def _ext(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def is_supported(name: str) -> bool:
    return _ext(name) in SUPPORTED


# ════════════════════════════════════════════════════ OLE 복합문서(.hwp)

_ENDOFCHAIN, _FREESECT = 0xFFFFFFFE, 0xFFFFFFFF


class _CFB:
    """OLE 복합 문서(Compound File Binary) 최소 리더.

    .hwp 5.0은 OLE 컨테이너 안에 zlib으로 압축된 본문 스트림을 담는다. 파이썬
    표준 라이브러리에는 OLE 리더가 없고, 이 프로젝트는 pip 설치를 하지 않기로 했다.
    그래서 필요한 만큼만(스트림 하나 꺼내기) 직접 구현한다.
    """

    def __init__(self, data: bytes):
        if data[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            raise ValueError("OLE 복합문서가 아닙니다")
        self.d = data
        self.ssz = 1 << struct.unpack_from("<H", data, 0x1E)[0]      # 섹터 크기
        self.mssz = 1 << struct.unpack_from("<H", data, 0x20)[0]     # 미니섹터 크기
        n_fat = struct.unpack_from("<I", data, 0x2C)[0]
        dir_start = struct.unpack_from("<I", data, 0x30)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
        mini_start = struct.unpack_from("<I", data, 0x3C)[0]
        difat_start = struct.unpack_from("<I", data, 0x44)[0]
        n_difat = struct.unpack_from("<I", data, 0x48)[0]

        # DIFAT는 헤더에 109개까지만 들어가고, 넘치면 별도 섹터로 이어진다.
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sec = difat_start
        for _ in range(min(n_difat, 4096)):
            if sec in (_ENDOFCHAIN, _FREESECT):
                break
            vals = struct.unpack_from(f"<{self.ssz // 4}I", data, self._off(sec))
            difat += list(vals[:-1])
            sec = vals[-1]

        self.fat: list[int] = []
        for s in difat[:n_fat]:
            if s in (_FREESECT, _ENDOFCHAIN):
                continue
            self.fat += list(struct.unpack_from(f"<{self.ssz // 4}I", data, self._off(s)))

        self.dir = self._chain(dir_start)
        root = self._entry(0)
        self.minifat: list[int] = []
        if mini_start not in (_ENDOFCHAIN, _FREESECT):
            mf = self._chain(mini_start)
            self.minifat = list(struct.unpack_from(f"<{len(mf) // 4}I", mf, 0))
        self.ministream = self._chain(root["start"]) if root["size"] else b""

    def _off(self, sector: int) -> int:
        return 512 + sector * self.ssz

    def _chain(self, start: int, size: int | None = None) -> bytes:
        out, sec, guard = [], start, 0
        while sec not in (_ENDOFCHAIN, _FREESECT) and guard < 500_000:
            off = self._off(sec)
            out.append(self.d[off:off + self.ssz])
            sec = self.fat[sec] if sec < len(self.fat) else _ENDOFCHAIN
            guard += 1
        blob = b"".join(out)
        return blob[:size] if size else blob

    def _mini(self, start: int, size: int) -> bytes:
        out, sec, guard = [], start, 0
        while sec not in (_ENDOFCHAIN, _FREESECT) and guard < 500_000:
            off = sec * self.mssz
            out.append(self.ministream[off:off + self.mssz])
            sec = self.minifat[sec] if sec < len(self.minifat) else _ENDOFCHAIN
            guard += 1
        return b"".join(out)[:size]

    def _entry(self, i: int) -> dict:
        b = self.dir[i * 128:(i + 1) * 128]
        if len(b) < 128:
            return {}
        nlen = struct.unpack_from("<H", b, 64)[0]
        return {"name": b[:max(0, nlen - 2)].decode("utf-16-le", "replace"),
                "type": b[66],
                "start": struct.unpack_from("<I", b, 116)[0],
                "size": struct.unpack_from("<Q", b, 120)[0]}

    def names(self) -> list[str]:
        return [e["name"] for i in range(len(self.dir) // 128)
                if (e := self._entry(i)).get("name")]

    def stream(self, name: str) -> bytes | None:
        for i in range(len(self.dir) // 128):
            e = self._entry(i)
            if e.get("name") == name and e.get("type") == 2:
                if e["size"] < self.mini_cutoff:
                    return self._mini(e["start"], e["size"])
                return self._chain(e["start"], e["size"])
        return None


# 본문 레코드에서 문단 글자를 담고 있는 태그.
_HWPTAG_PARA_TEXT = 67

# 제어문자 중 **뒤에 파라미터가 붙어 총 8워드(16바이트)를 차지하는** 것들.
#
# HWP 스펙은 이들을 'inline'(4~9,19,20)과 'extended'(1~3,11,12,14~18,21~23)로 나누는데,
# 'inline'은 크기가 아니라 '본문 흐름 안에 놓인다'는 뜻이라 **둘 다 8워드다**.
# inline을 1워드로 잘못 처리했더니 뒤따르는 파라미터를 글자로 읽어서
# "…필수 첨부 浥%！ÿ" 같은 깨진 꼬리가 붙었다.
_CTRL_WIDE = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
_CTRL_TAB = 9


def _hwp_text(data: bytes) -> str:
    cfb = _CFB(data)
    header = cfb.stream("FileHeader") or b""
    compressed = bool(header[36] & 1) if len(header) > 36 else True

    lines: list[str] = []
    for name in sorted(n for n in cfb.names() if n.startswith("Section")):
        raw = cfb.stream(name)
        if not raw:
            continue
        try:
            body = zlib.decompress(raw, -15) if compressed else raw
        except zlib.error:
            continue

        pos, end = 0, len(body)
        while pos + 4 <= end:
            hdr = struct.unpack_from("<I", body, pos)[0]
            pos += 4
            tag, size = hdr & 0x3FF, (hdr >> 20) & 0xFFF
            if size == 0xFFF:                       # 크기가 넘치면 다음 4바이트가 실제 크기
                if pos + 4 > end:
                    break
                size = struct.unpack_from("<I", body, pos)[0]
                pos += 4
            chunk = body[pos:pos + size]
            pos += size
            if tag != _HWPTAG_PARA_TEXT:
                continue

            buf, i, limit = [], 0, len(chunk) - 1
            while i < limit:
                c = struct.unpack_from("<H", chunk, i)[0]
                i += 2
                if c in _CTRL_WIDE:
                    if c == _CTRL_TAB:
                        buf.append("\t")             # 표 안 칸 구분은 살려 둔다
                    i += 14                          # 남은 7워드를 건너뛴다
                elif c in (10, 13):
                    buf.append("\n")
                elif c >= 32:
                    buf.append(chr(c))
            text = "".join(buf).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


# ═══════════════════════════════════════════════ ZIP 계열(.hwpx/.docx/.zip)

# hwpx는 <hp:t>, docx는 <w:t> 안에 글자가 들어 있다.
_TEXT_TAG_RE = re.compile(r"<(?:hp|w):t(?:\s[^>]*)?>(.*?)</(?:hp|w):t>", re.S)
_PARA_SPLIT_RE = re.compile(r"</(?:hp:p|w:p)>")
_TAG_RE = re.compile(r"<[^>]+>")


def _xml_text(xml: str) -> str:
    """문단(<hp:p>/<w:p>) 단위로 끊어서 줄을 살린다.

    글자는 <hp:t> 안에만 있고 문단 경계는 그 **바깥**이라, 경계를 줄바꿈 문자로
    바꿔치기해 봐야 추출에서 그대로 버려진다(그래서 문서 전체가 한 줄로 뭉쳤다).
    먼저 문단으로 쪼갠 뒤 각 조각에서 글자를 모아야 항목 구분이 남는다.
    """
    lines = []
    for para in _PARA_SPLIT_RE.split(xml):
        text = "".join(html.unescape(_TAG_RE.sub("", m.group(1)))
                       for m in _TEXT_TAG_RE.finditer(para))
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def _zip_text(data: bytes, depth: int = 0) -> str:
    """hwpx/docx/zip 공통 처리. zip 안에 서식이 또 들어 있으면 한 단계만 더 들어간다."""
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = z.namelist()

        # hwpx: Contents/section0.xml …   docx: word/document.xml
        targets = sorted(n for n in members
                         if re.search(r"(section\d+\.xml|word/document\.xml)$", n, re.I))
        for name in targets:
            out.append(_xml_text(z.read(name).decode("utf-8", "replace")))

        if not targets and depth < 1:               # 순수 .zip 묶음 → 안을 본다
            for name in members:
                if not is_supported(name) or name.endswith("/"):
                    continue
                info = z.getinfo(name)
                if info.file_size > MAX_BYTES:
                    continue
                try:
                    inner = extract_text(name, z.read(name), depth + 1)
                except Exception:
                    continue
                if inner:
                    out.append(f"[{os.path.basename(name)}]\n{inner}")
    return "\n".join(p for p in out if p)


# ═══════════════════════════════════════════════════════════════ 진입점

# "융 자 신 청 서" 처럼 글자 사이를 벌려 쓴 제목을 되돌린다 (한글 서식에서 매우 흔하다).
_SPACED_RE = re.compile(r"(?:(?<=^)|(?<=[\s(\[]))((?:[가-힣] ){2,}[가-힣])(?=[\s):\]]|$)",
                        re.M)


def _tidy(text: str) -> str:
    text = _SPACED_RE.sub(lambda m: m.group(1).replace(" ", ""), text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(name: str, data: bytes, depth: int = 0) -> str:
    """파일 이름(확장자)과 바이트로 글자를 뽑는다. 못 여는 형식이면 빈 문자열."""
    ext = _ext(name)
    try:
        if ext == ".hwp":
            return _tidy(_hwp_text(data))
        if ext in (".hwpx", ".docx", ".zip"):
            return _tidy(_zip_text(data, depth))
    except Exception as e:
        print(f"[알림] 첨부 '{name}' 을 열지 못했습니다. ({type(e).__name__}: {e})")
    return ""


def _cache_path(url: str, name: str) -> str:
    import hashlib
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, f"{key}{_ext(name)}")


def download(url: str, name: str) -> bytes | None:
    """첨부를 받아 온다. 한 번 받은 파일은 data/forms/ 에 캐시해 다시 받지 않는다.

    초안을 만들 때 그 공고의 서식 하나만 받는다 — 전체 공고의 첨부를 미리 긁어오지
    않는다(기관 서버에 부담이고, 대부분은 쓰이지도 않는다).
    """
    path = _cache_path(url, name)
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_BYTES + 1)
    except Exception as e:
        print(f"[알림] 첨부 내려받기 실패 — {name} ({e})")
        return None
    if len(data) > MAX_BYTES:
        print(f"[알림] 첨부가 너무 커서 건너뜁니다 — {name}")
        return None

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return data


def read_attachment(attachment: dict) -> str:
    """{kind, name, url} 첨부 하나 → 글자. 실패하거나 미지원 형식이면 빈 문자열."""
    name = attachment.get("name", "")
    if not is_supported(name):
        return ""
    data = download(attachment.get("url", ""), name)
    return extract_text(name, data) if data else ""

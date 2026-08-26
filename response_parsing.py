"""上游响应解析工具。

灵绘工坊需要对接大量形态各异的绘图中转站，它们的响应可能是：

* 标准 JSON（OpenAI Images / Chat Completions / Gemini）；
* 直接吐出的裸图片字节（NovelAI 官方接口、部分自建网关）；
* 打包成 ZIP 的图片（NovelAI /ai/generate-image）；
* 纯 base64 文本，既没有 data: 前缀也没有 JSON 包装；
* 掺杂心跳注释、event: / id: 字段的 SSE 流。

本模块把「读原始字节 → 判断类型 → 归一化」这套逻辑收敛到一处，
让 api_manager 只需要关心业务分支。

设计要点：所有 base64 探测都以图片 magic 前缀作为正则锚点。
若使用 [A-Za-z0-9+/]{1000,} 之类的宽泛模式去扫描数 MB 文本，
CPython 的回溯会长时间持有 GIL 并卡死事件循环。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import zipfile
from typing import Any, Dict, Iterator, List, Optional, Tuple

# 单张图片的安全上限，避免异常响应把内存吃光。
MAX_IMAGE_BYTES = 32 * 1024 * 1024

_ZIP_MAGIC = b"PK\x03\x04"

_IMAGE_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

# data:image/...;base64,xxxx
_DATA_IMAGE_RE = re.compile(
    r"data:image/[\w.+-]+;base64,\s*[A-Za-z0-9+/=\s\\]{80,}",
    re.IGNORECASE,
)

# 以图片 magic 的 base64 前缀锚定，杜绝灾难性回溯：
#   iVBORw0KGgo -> PNG        /9j/ -> JPEG
#   R0lGOD      -> GIF        UklGR -> WEBP(RIFF)
#   Qk0         -> BMP
_RAW_B64_RE = re.compile(
    r"(?:iVBORw0KGgo|/9j/|R0lGOD|UklGR|Qk0[A-Za-z0-9+/])[A-Za-z0-9+/_=-]{80,}"
)

_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+")

_IMAGE_URL_HINT = re.compile(r"\.(?:png|jpe?g|webp|gif|bmp)(?:[?#]|$)", re.IGNORECASE)

_IMAGE_PATH_HINT = re.compile(
    r"(?:/images?/|/img/|/file/|/download|/generat|/output|/result|/blob/|/cdn)",
    re.IGNORECASE,
)


def image_mime_from_bytes(blob: bytes) -> Optional[str]:
    """根据 magic bytes 推断图片 MIME，不是图片则返回 None。"""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    data = bytes(blob[:32])
    if len(data) < 4:
        return None
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def looks_like_binary_image(blob: bytes) -> bool:
    """响应体本身就是一张图片。"""
    return image_mime_from_bytes(blob) is not None


def looks_like_zip(blob: bytes) -> bool:
    """响应体是 ZIP 包（NovelAI 官方接口会这样返回）。"""
    return isinstance(blob, (bytes, bytearray)) and bytes(blob[:4]) == _ZIP_MAGIC


def decode_base64_image(text: str) -> Optional[bytes]:
    """把一段 base64 文本解码成图片字节，失败返回 None。

    同时兼容 URL-safe 字母表、缺失的等号补位，以及被换行或反斜杠
    污染过的内容（部分中转站会把 JSON 里的换行原样透出）。
    """
    if not text or not isinstance(text, str):
        return None
    payload = text.strip()
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[-1]
    payload = re.sub(r"[\s\\]+", "", payload)
    if len(payload) < 64:
        return None
    payload = payload.replace("-", "+").replace("_", "/")
    payload = re.sub(r"[^A-Za-z0-9+/=]", "", payload)
    payload = payload.rstrip("=")
    padding = (-len(payload)) % 4
    try:
        blob = base64.b64decode(payload + "=" * padding, validate=False)
    except (binascii.Error, ValueError):
        return None
    if not blob or len(blob) > MAX_IMAGE_BYTES:
        return None
    return blob if looks_like_binary_image(blob) else None


def looks_like_raw_base64_image(text: str) -> bool:
    """整段文本就是一张裸 base64 图片（没有 JSON、没有 data: 前缀）。"""
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < 128:
        return False
    if stripped[0] in "{[<":
        return False
    head = re.sub(r"[\s\\]+", "", stripped[:64])
    if not _RAW_B64_RE.match(head + "A" * 96):
        return False
    return decode_base64_image(stripped) is not None


def extract_zip_image(blob: bytes) -> Optional[bytes]:
    """从 ZIP 响应里取出第一张图片。"""
    if not looks_like_zip(blob):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(blob))) as bundle:
            for name in bundle.namelist():
                if name.endswith("/"):
                    continue
                try:
                    inner = bundle.read(name)
                except Exception:
                    continue
                if len(inner) > MAX_IMAGE_BYTES:
                    continue
                if looks_like_binary_image(inner):
                    return inner
    except Exception:
        return None
    return None


def image_bytes_from_body(blob: bytes) -> Optional[bytes]:
    """响应体是裸图片或图片 ZIP 时直接返回图片字节。"""
    if looks_like_binary_image(blob):
        return bytes(blob)
    return extract_zip_image(blob)


def _looks_like_image_url(url: str) -> bool:
    if not url:
        return False
    if _IMAGE_URL_HINT.search(url):
        return True
    return bool(_IMAGE_PATH_HINT.search(url))


def extract_mixed_image_payload(
    text: str, *, allow_bare_base64: bool = True
) -> Optional[Dict[str, Any]]:
    """从半结构化文本里抢救图片，返回伪 OpenAI Images 信封。

    适用于：Markdown 图片、夹在日志里的 data:image/... 片段、
    直接拼在文本中的裸 base64，或只给了一个图片直链的响应。
    """
    if not text or not isinstance(text, str):
        return None

    items: List[Dict[str, str]] = []
    seen: set = set()

    for match in _DATA_IMAGE_RE.finditer(text):
        blob = decode_base64_image(match.group(0))
        if blob is None:
            continue
        digest = blob[:64]
        if digest in seen:
            continue
        seen.add(digest)
        items.append({"b64_json": base64.b64encode(blob).decode()})
        if len(items) >= 4:
            break

    if not items and allow_bare_base64:
        for match in _RAW_B64_RE.finditer(text):
            blob = decode_base64_image(match.group(0))
            if blob is None:
                continue
            digest = blob[:64]
            if digest in seen:
                continue
            seen.add(digest)
            items.append({"b64_json": base64.b64encode(blob).decode()})
            if len(items) >= 4:
                break

    if not items:
        for match in _HTTP_URL_RE.finditer(text):
            url = match.group(0).rstrip(".,;)")
            if not _looks_like_image_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            items.append({"url": url})
            if len(items) >= 4:
                break

    if not items:
        return None
    return {"data": items}


def iter_sse_payloads(text: str) -> Iterator[str]:
    """遍历 SSE 文本中的 data 负载。

    比朴素的按 data 冒号空格切分更宽容：

    * 允许 data 冒号后没有空格；
    * 跳过冒号开头的注释心跳行，例如 ping / keep-alive；
    * 忽略 event / id / retry 字段；
    * 支持多行 data 拼接成一条事件（SSE 规范行为）。
    """
    if not text:
        return
    buffer: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            if buffer:
                joined = "\n".join(buffer).strip()
                buffer = []
                if joined and joined != "[DONE]":
                    yield joined
            continue
        if line.startswith(":"):
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip()
        if value.startswith(" "):
            value = value[1:]
        if field != "data":
            continue
        buffer.append(value)
    if buffer:
        joined = "\n".join(buffer).strip()
        if joined and joined != "[DONE]":
            yield joined


def looks_like_sse(text: str) -> bool:
    """判断文本是否为 SSE 流（含只有心跳、冒号后无空格等变体）。"""
    if not text or not isinstance(text, str):
        return False
    for raw_line in text.splitlines()[:80]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("data:") or line.startswith("event:"):
            return True
        if line.startswith(":"):
            continue
        return False
    return False


def parse_response_body(
    blob: bytes,
    charset: Optional[str] = None,
    *,
    allow_binary: bool = True,
    allow_bare_base64: bool = True,
) -> Tuple[Any, str, str]:
    """把原始响应字节归一化成三元组 (payload, text, kind)。

    kind 取值：

    binary  响应体本身是图片（或图片 ZIP），payload 为图片字节；
    json    标准 JSON，payload 为解析后的对象；
    base64  整段是裸 base64 图片，payload 为图片字节；
    mixed   文本里夹着图片，payload 为伪 Images 信封；
    sse     SSE 流，payload 为 None，交由上层逐条解析；
    empty   空响应；
    text    纯文本或无法识别，payload 为 None。

    text 始终是可安全打印的文本形式；二进制响应只给出简短描述，
    避免把图片字节塞进日志或错误消息里。
    """
    if blob is None:
        return None, "", "empty"
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        blob = str(blob).encode("utf-8", errors="replace")
    data = bytes(blob)
    if not data:
        return None, "", "empty"

    if allow_binary:
        image = image_bytes_from_body(data)
        if image is not None:
            mime = image_mime_from_bytes(image) or "image/png"
            return image, f"<binary {mime} {len(image)} bytes>", "binary"

    encoding = (charset or "utf-8").strip() or "utf-8"
    try:
        text = data.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        text = data.decode("utf-8", errors="replace")

    stripped = text.strip()
    if not stripped:
        return None, text, "empty"

    if stripped[0] in "{[":
        try:
            return json.loads(stripped), text, "json"
        except json.JSONDecodeError:
            pass

    if looks_like_sse(text):
        return None, text, "sse"

    if allow_bare_base64 and looks_like_raw_base64_image(stripped):
        image = decode_base64_image(stripped)
        if image is not None:
            return image, f"<bare base64 image {len(image)} bytes>", "base64"

    mixed = extract_mixed_image_payload(text, allow_bare_base64=allow_bare_base64)
    if mixed is not None:
        return mixed, text, "mixed"

    return None, text, "text"

import re
from typing import Any, List
from urllib.parse import urlsplit, urlunsplit


_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$", re.IGNORECASE)


def norm_id(raw_id: Any) -> str:
    """标准化 ID 为字符串"""
    if raw_id is None:
        return ""
    return str(raw_id).strip()


def normalize_model_list(raw_models: Any) -> List[str]:
    """兼容字符串列表和旧版字典模型配置，并跳过无效项。"""
    if not isinstance(raw_models, (list, tuple, set)):
        return []

    models = []
    for item in raw_models:
        value = ""
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("id") or item.get("model") or item.get("name") or ""

        value = str(value).strip()
        if value and value not in models:
            models.append(value)
    return models


def is_custom_drawing_command(command: Any, configured_command: Any) -> bool:
    """Return whether a command invokes custom-prompt drawing.

    ``bnn`` is the upstream public command and remains available even when a
    deployment changes the preferred short command from the default ``画``.
    """
    normalized = str(command or "").strip().casefold()
    if not normalized:
        return False

    aliases = {"bnn"}
    configured = str(configured_command or "").strip().casefold()
    if configured:
        aliases.add(configured)
    return normalized in aliases


def append_negative_prompt(prompt: Any, negative_prompt: Any) -> str:
    """Append one portable negative-prompt clause without duplicating it.

    OpenAI Images, Gemini, and chat-compatible image models do not share a
    common ``negative_prompt`` request field. Keeping this as a prompt clause
    lets the drawing-channel router send the same constraint to every route.
    """
    final_prompt = str(prompt or "").strip()
    negative = str(negative_prompt or "").strip()
    if not final_prompt or not negative:
        return final_prompt

    clause = f"Negative prompt: {negative}"
    if clause.casefold() in final_prompt.casefold():
        return final_prompt
    return f"{final_prompt}\n\n{clause}"


def normalize_api_root(raw_url: Any) -> str:
    """提取 API 基础地址，忽略用户填写的版本段和已拼接接口路径。

    例如：
    - https://api.example.com/v1 -> https://api.example.com
    - https://api.example.com/api/v1beta/models/x:generateContent -> https://api.example.com/api
    - https://api.example.com/openai/v1/chat/completions -> https://api.example.com/openai
    """
    url = str(raw_url or "").strip().rstrip("/")
    if not url:
        return ""

    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        # 配置项正常应为绝对 URL；这里仍对异常输入做保守的字符串清理。
        clean = re.split(r"[?#]", url, maxsplit=1)[0].rstrip("/")
        clean = re.sub(
            r"/(?:v\d+(?:(?:alpha|beta)\d*)?)(?:/.*)?$",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        clean = re.sub(
            r"/(?:chat/completions|images/(?:generations|edits)|models(?:/.*)?)$",
            "",
            clean,
            flags=re.IGNORECASE,
        )
        return clean.rstrip("/")

    segments = [segment for segment in parsed.path.split("/") if segment]
    cut_at = len(segments)

    for index, segment in enumerate(segments):
        lower_segment = segment.lower()
        if _API_VERSION_SEGMENT.fullmatch(segment):
            cut_at = index
            break
        if lower_segment == "models":
            cut_at = index
            break
        if lower_segment == "chat" and index + 1 < len(segments) and segments[index + 1].lower() == "completions":
            cut_at = index
            break
        if lower_segment == "images" and index + 1 < len(segments) and segments[index + 1].lower() in {
            "generations", "edits"
        }:
            cut_at = index
            break

    root_path = "/" + "/".join(segments[:cut_at]) if cut_at else ""
    return urlunsplit((parsed.scheme, parsed.netloc, root_path.rstrip("/"), "", "")).rstrip("/")


def extract_image_urls_from_text(text: str) -> List[str]:
    """从文本中提取图片链接和本地文件路径"""
    image_urls = []

    # 本地文件路径 (Windows)
    local_patterns = [r'[a-zA-Z]:\\[^\s,，。！？\n]+\.(?:jpg|jpeg|png|gif|bmp|webp)']
    for pattern in local_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if match and match not in image_urls:
                image_urls.append(match)

    # 网络 URL
    url_patterns = [
        r'https?://[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
        r'https?://[^\s<>"\'\)]+/(?:s\d+/|upload/|image/|img/|pic/)[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])',
        r'https?://youke\d+\.picui\.cn/[^\s<>"\'\)]+\.(?:jpg|jpeg|png|gif|bmp|webp)(?:\?[^\s<>"\'\)]*)?(?=[\s<>"\'\)|$])'
    ]
    for pattern in url_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            if match and match not in image_urls:
                image_urls.append(match)

    return image_urls

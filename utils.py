import re
from typing import Any, List
from urllib.parse import urlsplit, urlunsplit


_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d*)?$", re.IGNORECASE)
_QQ_AMBIGUOUS_DELIVERY_TIMEOUT_MARKERS = (
    "sendmsg",
    "nodeikernelmsgservice",
)


# QQ mentions are input selectors, not visual text instructions.  This is a
# final, direct instruction rather than an ordinary negative-prompt item:
# several image gateways give a user-authored sentence in the main prompt more
# weight than a generic ``Negative prompt:`` section.  Keeping the guard
# independent from the optional administrator setting also prevents accidental
# exposure when that setting is empty.
MENTION_AVATAR_PRIVACY_INSTRUCTION = (
    "【必须严格遵守】图片不带艾特对象的名字、昵称、群名、QQ 号和任何 ID；"
    "不要出现 @ 文本、资料卡文字、账号信息、聊天界面、标签、字幕或水印。"
    "艾特和 QQ 头像只用于选择参考人物、提供人物外观特征，不是要写进图片的内容；"
    "即使参考头像或输入素材里含有这些身份文字，也必须忽略并移除。 "
    "Mandatory final instruction: do not include any mentioned person's name, "
    "display name, group name, QQ number, account ID, @mention text, profile-card "
    "text, chat UI, label, caption, or watermark in the image. Use the supplied "
    "avatars only as visual appearance references and omit all identifying text."
)

# Compatibility alias for code/tests importing the earlier internal name.
MENTION_AVATAR_PRIVACY_NEGATIVE_PROMPT = MENTION_AVATAR_PRIVACY_INSTRUCTION


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


def append_final_instruction(prompt: Any, instruction: Any) -> str:
    """Append a high-priority instruction at the very end of a prompt.

    Unlike :func:`append_negative_prompt`, this deliberately avoids a generic
    negative-prompt label.  It is used for constraints that must survive prompt
    translation/optimization and remain the last instruction sent to every
    fallback channel.
    """
    final_prompt = str(prompt or "").strip()
    suffix = str(instruction or "").strip()
    if not suffix:
        return final_prompt
    if not final_prompt:
        return suffix
    if suffix.casefold() in final_prompt.casefold():
        return final_prompt
    return f"{final_prompt}\n\n{suffix}"


def combine_negative_prompts(*values: Any) -> str:
    """Join optional portable negative clauses without duplicate entries."""
    combined = []
    seen = set()
    for value in values:
        text = str(value or "").strip().strip(",; ")
        if not text:
            continue
        folded = text.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        combined.append(text)
    return "; ".join(combined)


def has_mention_privacy_instruction(prompt: Any) -> bool:
    """Return whether the user already forbids mention names and account IDs.

    The automatic avatar privacy guard should remain a fallback, not produce
    a second copy of an instruction the user has already written explicitly.
    Keep the check deliberately conservative: both a name-like identifier and
    an account/ID-like identifier must appear near a negative instruction.
    """
    text = re.sub(r"\s+", " ", str(prompt or "")).strip().casefold()
    if not text:
        return False

    negative_hit = bool(re.search(
        r"(?:不带|不要|不得|禁止|避免|别|去掉|移除|不能出现|不要出现|不要显示|"
        r"do\s+not|don't|without|must\s+not|never|no\s+visible)",
        text,
    ))
    if not negative_hit:
        return False

    name_hit = bool(re.search(
        r"(?:艾特名|提及名|名字|名称|昵称|用户名|群名|群聊名|display\s+names?|"
        r"usernames?|group\s+names?|mention\s+names?)",
        text,
    ))
    id_hit = bool(re.search(
        r"(?:qq\s*(?:号|号码|id)?|帐号|账号|账户|用户\s*id|艾特\s*id|提及\s*id|"
        r"account\s+(?:ids?|numbers?)|qq\s*ids?|user\s*ids?|\bids?\b)",
        text,
        flags=re.IGNORECASE,
    ))
    return name_hit and id_hit


def is_ambiguous_message_delivery_timeout(error: Any) -> bool:
    """Identify QQ send-receipt timeouts whose delivery state is unknown.

    The QQ adapter can report ``ActionFailed ... Timeout`` after the image
    payload has already reached QQ.  Retrying that case sends duplicate
    pictures, while treating every timeout as successful would hide real
    delivery failures.  Restrict the exception to the adapter's send-message
    markers only.
    """
    text = str(error or "").casefold()
    return "timeout" in text and any(
        marker in text for marker in _QQ_AMBIGUOUS_DELIVERY_TIMEOUT_MARKERS
    )


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

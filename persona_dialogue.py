from __future__ import annotations

import re

_ACTION_DESCRIPTIONS = {
    "draw": "你准备按用户的要求制作图片，完成后再发给对方",
    "edit": "你准备根据用户给出的参考图完成修改，完成后再发给对方",
    "auto_text": "你准备按用户的文字要求制作图片，完成后再发给对方",
    "auto_image": "你准备根据用户给出的图片完成修改，完成后再发给对方",
    "batch": "你准备处理用户给出的多张图片，结果会随后陆续发给对方",
    "pack_pdf": "你准备把用户给出的图片整理成 PDF，完成后再发给对方",
    "persona": "用户正在向你索要你自己的照片或自拍；你准备以角色本人身份去拍，完成后再发给对方",
}

_FORBIDDEN_PROGRESS_TERMS = (
    "[tool_",
    "tool call",
    "api",
    "系统",
    "工具",
    "插件",
    "模型",
    "参数",
    "配置",
    "生成",
    "绘制",
    "绘图",
    "渲染",
    "任务完成",
    "已发送",
)


def build_persona_progress_prompts(
    persona_prompt: str,
    action: str,
    *,
    count: int = 1,
    total_images: int = 0,
    has_user_images: bool = False,
    is_clothing_request: bool = False,
) -> tuple[str, str]:
    """Build a small, tool-free request for an in-character progress reply."""

    description = _ACTION_DESCRIPTIONS.get(action, "你准备开始完成用户刚才提出的图片相关请求")
    details: list[str] = []
    if total_images > 1:
        details.append(f"这次需要处理 {total_images} 张输入图")
    elif count > 1:
        details.append(f"用户明确要 {count} 张结果")
    if has_user_images:
        details.append("用户还提供了参考图")
    if is_clothing_request:
        details.append("请求里包含换装或穿搭要求")

    system_prompt = (
        f"{str(persona_prompt or '').strip()}\n\n"
        "# 本次临时发言要求\n"
        "你只负责在真正开始前说一句很短的自然承接话。必须严格沿用上方 AstrBot 当前人格、口吻、称呼习惯和与用户的关系，"
        "不要采用任何插件预设性格，也不要像客服播报进度。\n"
        "- 只输出一句可直接发给用户的话，不要解释，不要使用 Markdown 或引号。\n"
        "- 不要提 AI、系统、插件、工具、API、模型、参数、配置，也不要说生成、绘制、绘图、渲染、任务完成或已发送。\n"
        "- 这是开始前的承接话，不能假装事情已经做完。\n"
        "- 若是索要 Bot 自己的照片，要以当前人格本人身份自然回应。\n"
        "- 尽量控制在 30 个汉字以内。"
    )

    user_lines = [f"当前情境：{description}。"]
    if details:
        user_lines.append("补充情况：" + "；".join(details) + "。")
    user_lines.append("现在只输出一句符合当前人格的开始前承接话。")
    return system_prompt, "\n".join(user_lines)


def sanitize_persona_progress_reply(value: object, *, max_chars: int = 80) -> str:
    """Keep only a short, user-facing persona line; unsafe output becomes silent."""

    text = str(value or "").strip()
    if not text:
        return ""

    text = re.sub(r"<\s*think\b[^>]*>.*?<\s*/\s*think\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<\s*analysis\b[^>]*>.*?<\s*/\s*analysis\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:回复|回答|承接话|进度提示)\s*[：:]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("\"'“”‘’` ")

    lowered = text.lower()
    if not text or any(term in lowered for term in _FORBIDDEN_PROGRESS_TERMS):
        return ""
    if text.startswith("[") or len(text) > max(1, int(max_chars)):
        return ""
    return text

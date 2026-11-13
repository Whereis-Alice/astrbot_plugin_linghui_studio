"""Short, line-separated generation failures for chat and LLM tool replies."""

from __future__ import annotations

import re
from typing import Any

from .error_classify import classify_generation_error, safe_error_summary


_ROUTE_FAILURE_RE = re.compile(
    r"(?:^|\s+\|\s+)([A-Za-z0-9_-]{1,48})\[[^\]\n]+\]:\s*"
)
_SIMPLE_LOCAL_RE = re.compile(r"^[\u3400-\u9fffA-Za-z0-9 ，,。.!！?？；;：:、（）()\[\]【】_/-]+$")
_SAFETY_STOP_MARKER = "（内容审核拦截，未继续切换渠道）"
_SAFETY_STOP_ADVICE = "内容审核已拦截，本次未继续切换渠道。请调整提示词或参考图后重试。"
_INTERNAL_MARKERS = (
    "traceback", "exception", "typeerror", "valueerror", "keyerror", "attributeerror",
    "runtimeerror", "http://", "https://", "/root/", "\\users\\",
)
_REASONS = {
    "authentication": "渠道鉴权失败",
    "rate_limit": "渠道限流或额度不足",
    "safety": "内容审核未通过",
    "unsupported": "接口或模型不支持此请求",
    "bad_request": "请求参数或图片格式不受支持",
    "timeout": "请求超时",
    "network": "网络连接失败",
    "upstream": "上游服务异常",
    "bad_response": "返回的图片或响应无效",
    "configuration": "渠道配置不完整",
    "reference_echo": "渠道原样返回了参考图",
    "unknown": "渠道返回了未识别的错误",
}


def _route_failures(error: str) -> list[tuple[str, str]]:
    """Read the router's legacy diagnostic string without splitting URL fields."""
    if not error.startswith("绘图渠道均失败"):
        return []
    # Use the channel marker itself as the boundary: provider payloads may
    # contain colons, newlines, URLs and vertical bars.
    first = re.search(r"[A-Za-z0-9_-]{1,48}\[[^\]\n]+\]:\s*", error)
    if first is None:
        return []
    body = error[first.start():]
    matches = list(_ROUTE_FAILURE_RE.finditer(body))
    rows = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        rows.append((match.group(1), body[match.end():end].strip()))
    return rows


def _reason(error: Any, include_status: bool) -> tuple[str, str]:
    classification = classify_generation_error(error)
    reason = _REASONS.get(classification.category, _REASONS["unknown"])
    if "未返回有效的 GIF 动画" in str(error):
        reason = "渠道未返回有效的 GIF 动画"
    if include_status and classification.status_code:
        reason += f"（HTTP {classification.status_code}）"
    return classification.category, reason


def _next_step(categories: set[str]) -> str:
    if "safety" in categories:
        if categories & {"timeout", "network", "upstream", "rate_limit"}:
            return "请调整提示词或参考图后重试；超时或异常渠道可稍后再试。"
        return "请调整提示词或参考图后重试。"
    if categories & {"authentication", "configuration"}:
        return "请联系管理员检查渠道设置。"
    if categories & {"bad_request", "unsupported", "bad_response", "reference_echo"}:
        return "请检查图片、请求参数或所选渠道后重试。"
    return "请稍后重试；持续失败可查看任务详情。"


def format_generation_failure(
    error: Any,
    default_message: str = "这次没弄好，请稍后再试。",
    *,
    include_status: bool = True,
) -> str:
    """Explain each failed channel without leaking JSON, HTML or credentials.

    The router and task history retain their original diagnostic strings. This
    is only a presentation boundary; it does not change retry/fallback policy.
    """
    text = str(error or "").strip()
    safety_stopped = text.endswith(_SAFETY_STOP_MARKER)
    if safety_stopped:
        # Routing metadata must not affect the classification of the final
        # channel's own diagnostic payload.
        text = text[:-len(_SAFETY_STOP_MARKER)].rstrip()
    if not text:
        return _SAFETY_STOP_ADVICE if safety_stopped else default_message
    failures = _route_failures(text)
    if failures:
        categories: set[str] = set()
        rows = []
        for channel, detail in failures[:8]:
            category, reason = _reason(detail, include_status)
            categories.add(category)
            rows.append(f"• {channel}：{reason}")
        advice = _SAFETY_STOP_ADVICE if safety_stopped else _next_step(categories)
        return "\n".join([*rows, advice])

    category, reason = _reason(text, include_status)
    if category == "unknown":
        if safety_stopped:
            return _SAFETY_STOP_ADVICE
        # Local validation messages are already useful prose. Never echo an
        # arbitrary upstream body, traceback or formatted diagnostic object.
        safe = safe_error_summary(text, 180)
        if (not any(marker in text.lower() for marker in _INTERNAL_MARKERS)
                and _SIMPLE_LOCAL_RE.fullmatch(safe) and re.search(r"[\u3400-\u9fff]", safe)):
            return safe
        return default_message
    if "未返回有效的 GIF 动画" in text and not safety_stopped:
        return f"{reason}\n请确认所选渠道支持 GIF，或更换支持 GIF 的渠道。"
    advice = _SAFETY_STOP_ADVICE if safety_stopped else _next_step({category})
    return f"{reason}\n{advice}"

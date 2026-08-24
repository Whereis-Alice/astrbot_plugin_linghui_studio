"""Structured drawing error classification used by routing and Dashboard metrics."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Optional


_STATUS_RE = re.compile(
    r"(?:\bHTTP\s*|\bAPI(?:\s+Error|\s+Multipart\s+Error)?\s*)([1-5]\d{2})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GenerationError:
    """A safe, policy-oriented view of a provider failure."""

    category: str
    label: str
    retryable: bool
    try_next_key: bool
    try_next_channel: bool
    cooldown_recommended: bool
    status_code: Optional[int] = None
    safe_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _status_code(text: str) -> Optional[int]:
    match = _STATUS_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def classify_generation_error(error: Any) -> GenerationError:
    """Classify exceptions and the plugin's existing string-error contract.

    The classifier intentionally avoids parsing or exposing API keys.  Its
    output is suitable for route decisions, task history, and administrator
    diagnostics.
    """

    raw = str(error or "").strip()
    lower = raw.lower()
    status = _status_code(raw)

    if _contains(lower, "reference image", "参考图相同", "reference_echo", "无效结果"):
        return GenerationError(
            "reference_echo", "参考图回显", True, False, True, False, status,
            "渠道原样返回了参考图，已自动拒绝该结果。",
        )

    if status in {401, 403} or _contains(
        lower,
        "invalid_api_key", "incorrect api key", "unauthorized", "authentication",
        "permission denied", "forbidden", "鉴权", "认证失败", "无可用 api key",
    ):
        return GenerationError(
            "authentication", "鉴权失败", True, True, True, False, status,
            "渠道鉴权失败，请检查该渠道保存的密钥或账户权限。",
        )

    if status == 429 or _contains(
        lower, "rate limit", "rate_limit", "too many requests", "quota exceeded",
        "限流", "请求过多", "额度已用尽",
    ):
        return GenerationError(
            "rate_limit", "限流或上游额度不足", True, True, True, True, status,
            "渠道暂时限流或上游额度不足，已尝试轮换密钥或回退渠道。",
        )

    if _contains(
        lower, "content_filter", "safety filter", "safety", "blocked", "moderation",
        "安全过滤", "内容过滤", "生成被拦截", "sexually_explicit",
    ):
        return GenerationError(
            "safety", "内容安全限制", False, False, False, False, status,
            "请求被内容安全规则拦截，请调整提示词后重试。",
        )

    if _contains(
        lower, "endpoint unsupported", "unsupported", "not support", "does not support",
        "不支持", "模型不存在", "model_not_found", "unknown model", "edits endpoint",
    ) or status in {404, 405}:
        return GenerationError(
            "unsupported", "接口或模型不支持", False, False, True, False, status,
            "当前渠道的接口或模型不支持这类请求，已尝试其他可用渠道。",
        )

    if status in {400, 409, 413, 415, 422} or _contains(
        lower, "invalid parameter", "invalid_request", "bad request", "malformed",
        "参数错误", "请求格式", "payload", "image size", "尺寸不支持",
    ):
        return GenerationError(
            "bad_request", "请求参数或格式错误", False, False, True, False, status,
            "当前渠道不接受这组请求参数或图片格式，已尝试其他可用渠道。",
        )

    if status in {408, 504} or _contains(
        lower, "timeout", "timed out", "请求超时", "连接超时", "read timeout",
    ):
        return GenerationError(
            "timeout", "请求超时", True, False, True, True, status,
            "渠道响应超时，已尝试其他可用渠道。",
        )

    if _contains(
        lower, "clientconnector", "connection reset", "connection refused", "cannot connect",
        "dns", "ssl", "network", "server disconnected", "连接失败", "网络错误",
        "连接被重置", "无法连接", "name or service not known",
    ):
        return GenerationError(
            "network", "网络连接失败", True, False, True, True, status,
            "渠道网络连接失败，已尝试其他可用渠道。",
        )

    if status is not None and status >= 500 or _contains(
        lower, "bad_response_status_code", "openai_error", "service unavailable",
        "bad gateway", "gateway timeout", "upstream", "上游错误", "服务不可用",
    ):
        return GenerationError(
            "upstream", "上游服务异常", True, False, True, True, status,
            "上游服务暂时异常，已尝试其他可用渠道。",
        )

    if _contains(
        lower, "数据解析失败", "未找到图片数据", "返回内容为空", "无法解析出图片",
        "result image download", "结果图片下载失败", "下载结果图失败",
    ):
        return GenerationError(
            "bad_response", "响应或结果图异常", True, False, True, True, status,
            "渠道返回的内容无法识别为有效图片，已尝试其他可用渠道。",
        )

    if _contains(lower, "url 未配置", "api url", "未配置", "no available", "没有可用于"):
        return GenerationError(
            "configuration", "渠道配置不完整", False, False, True, False, status,
            "渠道配置不完整，请检查接口地址、模型和密钥。",
        )

    return GenerationError(
        "unknown", "未知错误", True, False, True, False, status,
        "渠道返回了未识别的错误，已按回退规则继续尝试。",
    )


def safe_error_summary(error: Any, limit: int = 240) -> str:
    """Return a bounded diagnostic string without accidental key leakage."""

    text = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-***", str(error or ""))
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:key|api[_-]?key|access[_-]?token)=)[^&\s]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)((?:x-goog-api-key|api[_ -]?keys?|access[_ -]?tokens?)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\"',;}\s]+",
        r"\1***",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(40, int(limit or 240))]

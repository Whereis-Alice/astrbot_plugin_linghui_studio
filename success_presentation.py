"""Compact success receipts shared by direct, background and batch drawing."""

from collections.abc import Mapping
from typing import Any


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def format_generation_success(
    timing_text: str,
    preset_name: str,
    *,
    requester_prefix: str = "",
    quota_text: str = "",
    route_metrics: Mapping[str, Any] | None = None,
    requested_model: str = "",
    show_model_info: bool = False,
    extra_rules: str = "",
    position: str = "",
    version: bool = False,
) -> str:
    """Use the completed request's model and keep quota details on a new line."""
    action = "版本生成成功" if version else "生成成功"
    position = _single_line(position)
    heading = f"{requester_prefix}✅ {position + ' ' if position else ''}{action}"
    timing_text = _single_line(timing_text)
    if timing_text:
        heading += f" ({timing_text})"
    fields = [heading, f"预设: {_single_line(preset_name)}"]
    extra_rules = _single_line(extra_rules)
    if extra_rules:
        fields.append(f"规则: {extra_rules[:20]}{'...' if len(extra_rules) > 20 else ''}")
    metrics = route_metrics if isinstance(route_metrics, Mapping) else {}
    model = _single_line(metrics.get("model") or requested_model)
    if show_model_info and model:
        fields.append(f"模型: {model}")
    quota_summary, _, quota_details = str(quota_text or "").strip().partition("\n")
    if quota_summary:
        fields.append(f"剩余: {quota_summary}")
    receipt = " | ".join(fields)
    if quota_details.strip():
        receipt += "\n" + quota_details.strip()
    return receipt

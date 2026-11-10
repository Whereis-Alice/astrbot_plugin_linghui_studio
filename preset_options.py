"""Per-preset image options, separate from the service channel configuration."""

from collections.abc import Mapping
from typing import Any

from .channel_policy import normalize_policy


IMAGE_OPTION_KEYS = (
    "image_output_format", "image_frame_count", "image_frame_duration_ms", "image_complexity",
)
DEFAULT_PRESET_GENERATION_OPTIONS = [{
    "__template_key": "preset_generation_option", "preset": "GIF表情",
    "image_output_format": "gif", "image_frame_count": 9,
    "image_frame_duration_ms": 200, "image_complexity": "auto",
}]


def normalize_image_options(raw: Any) -> dict[str, Any]:
    """Validate only image fields, without accepting service credentials or models."""
    if not isinstance(raw, Mapping):
        raise ValueError("预设图片参数必须是配置对象。")
    values = normalize_policy({key: raw[key] for key in IMAGE_OPTION_KEYS if key in raw})
    return {key: values[key] for key in IMAGE_OPTION_KEYS}


def normalize_preset_generation_options(incoming: Any, names: set[str]) -> list[dict[str, Any]]:
    """Reject bad entries before any live preset or route state is modified."""
    if not isinstance(incoming, list) or len(incoming) > 200:
        raise ValueError("预设图片参数必须是列表，最多 200 项。")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in incoming:
        if not isinstance(row, dict) or not isinstance(row.get("preset"), str):
            raise ValueError("预设图片参数必须包含文本类型的 preset。")
        name = row["preset"].strip()
        if not name or name not in names:
            raise ValueError(f"图片参数对应的预设不存在：{name or '（空）'}。")
        if name in seen:
            raise ValueError(f"预设图片参数重复：{name}。")
        seen.add(name)
        result.append({"__template_key": "preset_generation_option", "preset": name,
                       **normalize_image_options(row)})
    return result


def preset_options_map(config: Any) -> dict[str, dict[str, Any]]:
    """Read saved options; a missing key supplies the built-in animation default."""
    entries = config.get("preset_generation_options", DEFAULT_PRESET_GENERATION_OPTIONS)
    if not isinstance(entries, list):
        raise ValueError("预设图片参数配置无效，请在 WebUI 重新保存。")
    result: dict[str, dict[str, Any]] = {}
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("preset"), str) or not row["preset"].strip():
            raise ValueError("预设图片参数配置无效，请在 WebUI 重新保存。")
        name = row["preset"].strip()
        if name in result:
            raise ValueError(f"预设 [{name}] 存在重复的图片参数，请在 WebUI 修正。")
        result[name] = normalize_image_options(row)
    return result


def resolve_preset_generation_options(config: Any, preset_name: str) -> dict[str, Any]:
    """An explicit format overrides this request; auto inherits the channel."""
    if not preset_name:
        return {}
    options = preset_options_map(config).get(preset_name, {})
    return dict(options) if options.get("image_output_format", "auto") != "auto" else {}

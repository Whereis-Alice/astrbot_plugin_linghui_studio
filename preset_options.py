"""Unified per-preset channel selections and image options, with legacy migration."""

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .channel_policy import normalize_policy
from .configuration_cleanup import LEGACY_ACCESS_KEYS


IMAGE_OPTION_KEYS = (
    "image_output_format", "image_frame_count", "image_frame_duration_ms", "image_complexity",
)
DEFAULT_PRESET_GENERATION_OPTIONS = [{
    "__template_key": "preset_generation_option", "preset": "GIF表情",
    "image_output_format": "gif", "image_frame_count": 9,
    "image_frame_duration_ms": 200, "image_complexity": "auto",
}]

PRESET_SETTINGS_KEYS = ["preset_generation_options", "preset_channel_bindings", "preset_config_version"]


def snapshot_native_preset_settings(path: Any) -> dict[str, Any]:
    """Read only migration fields before AstrBot removes obsolete schema keys."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return {key: raw[key] for key in (*PRESET_SETTINGS_KEYS, *LEGACY_ACCESS_KEYS) if key in raw} if isinstance(raw, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def restore_legacy_preset_snapshot(config: Any, snapshot: Mapping[str, Any]) -> None:
    """Restore a removed legacy input only until the first successful migration."""
    if (str(config.get("preset_config_version", 0)) != "1"
            and str(snapshot.get("preset_config_version", 0)) != "1"
            and "preset_channel_bindings" not in config
            and "preset_channel_bindings" in snapshot):
        config["preset_channel_bindings"] = snapshot["preset_channel_bindings"]


def _channel_ids(raw: Any, name: str) -> list[str]:
    if (not isinstance(raw, list) or len(raw) > 8
            or any(not isinstance(value, str) or not value.strip() for value in raw)):
        raise ValueError(f"预设 [{name}] 的渠道列表无效，请重新选择渠道。")
    result = [value.strip() for value in raw]
    if len(set(result)) != len(result):
        raise ValueError(f"预设 [{name}] 的渠道列表重复，请在 WebUI 修正。")
    return result


def _legacy_channels(incoming: Any) -> dict[str, list[str]]:
    if not isinstance(incoming, list):
        raise ValueError("预设渠道绑定配置无效，请在 WebUI 重新保存预设。")
    result: dict[str, list[str]] = {}
    for row in incoming:
        if not isinstance(row, Mapping) or not isinstance(row.get("preset"), str) or not row["preset"].strip():
            raise ValueError("预设渠道绑定配置无效，请在 WebUI 重新保存预设。")
        name = row["preset"].strip()
        if name in result:
            raise ValueError(f"预设 [{name}] 存在重复的渠道绑定，请在 WebUI 修正。")
        if "channel_ids" in row and row["channel_ids"] != []:
            result[name] = _channel_ids(row["channel_ids"], name)
        else:
            single = row.get("channel_id", "")
            if not isinstance(single, str) or not single.strip():
                raise ValueError(f"预设 [{name}] 的绑定渠道为空或无效，请重新选择渠道。")
            result[name] = [single.strip()]
    return result


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
        normalized = {"__template_key": "preset_generation_option", "preset": name,
                      **normalize_image_options(row)}
        if "channel_ids" in row:
            normalized["channel_ids"] = _channel_ids(row["channel_ids"], name)
        result.append(normalized)
    return result


def effective_preset_settings(config: Any) -> list[dict[str, Any]]:
    """Read both generations of config without discarding schema-injected defaults."""
    entries = config.get("preset_generation_options", DEFAULT_PRESET_GENERATION_OPTIONS)
    if not isinstance(entries, list):
        raise ValueError("预设图片参数配置无效，请在 WebUI 重新保存。")
    names: set[str] = set()
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get("preset"), str) or not row["preset"].strip():
            raise ValueError("预设图片参数配置无效，请在 WebUI 重新保存。")
        name = row["preset"].strip()
        if name in names:
            raise ValueError(f"预设 [{name}] 存在重复的图片参数，请在 WebUI 修正。")
        names.add(name)
    rows = normalize_preset_generation_options(entries, names)
    legacy = _legacy_channels(config.get("preset_channel_bindings", []))
    migrated = str(config.get("preset_config_version", 0)).strip() == "1"
    for row in rows:
        name = row["preset"]
        if name in legacy and (not migrated or "channel_ids" not in row):
            row["channel_ids"] = legacy[name]
        else:
            row.setdefault("channel_ids", [])
    for name, ids in legacy.items():
        if name not in names:
            rows.append({"__template_key": "preset_generation_option", "preset": name,
                         "channel_ids": ids, **normalize_image_options({})})
    return rows


def preset_options_map(config: Any) -> dict[str, dict[str, Any]]:
    """Return only image fields to the request-local transport override."""
    return {row["preset"]: {key: row[key] for key in IMAGE_OPTION_KEYS}
            for row in effective_preset_settings(config)}


def preset_channels_map(config: Any) -> dict[str, list[str]]:
    return {row["preset"]: list(row["channel_ids"])
            for row in effective_preset_settings(config) if row["channel_ids"]}


def store_preset_settings(config: Any, options: Any = None, bindings: Any = None) -> list[str]:
    """Validate then atomically replace both old stores with one canonical list."""
    current = effective_preset_settings(config)
    selected = {row["preset"]: row["channel_ids"] for row in current}
    if options is None:
        rows = [dict(row) for row in current]
    else:
        if not isinstance(options, list):
            raise ValueError("预设图片参数必须是列表，最多 200 项。")
        names = {row["preset"].strip() for row in options
                 if isinstance(row, dict) and isinstance(row.get("preset"), str)}
        rows = normalize_preset_generation_options(options, names)
        for row in rows:
            row.setdefault("channel_ids", list(selected.get(row["preset"], [])))
        # Old image-only payloads may omit a preset that still has a channel selection.
        for name, ids in selected.items():
            if bindings is None and name not in names and ids:
                rows.append({"__template_key": "preset_generation_option", "preset": name,
                             "channel_ids": list(ids), **normalize_image_options({})})
    if bindings is not None:
        selection = _legacy_channels(bindings)
        present = set()
        for row in rows:
            name = row["preset"]
            row["channel_ids"] = selection.get(name, [])
            present.add(name)
        for name, ids in selection.items():
            if name not in present:
                rows.append({"__template_key": "preset_generation_option", "preset": name,
                             "channel_ids": ids, **normalize_image_options({})})
    config["preset_generation_options"] = rows
    config.pop("preset_channel_bindings", None)
    config["preset_config_version"] = 1
    return list(PRESET_SETTINGS_KEYS)


def resolve_preset_generation_options(config: Any, preset_name: str) -> dict[str, Any]:
    """An explicit format overrides this request; auto inherits the channel."""
    if not preset_name:
        return {}
    options = preset_options_map(config).get(preset_name, {})
    return dict(options) if options.get("image_output_format", "auto") != "auto" else {}

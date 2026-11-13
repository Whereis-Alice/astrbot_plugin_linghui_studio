"""Remove retired top-level settings; drawing services live in named channels."""

from collections.abc import MutableMapping
from typing import Any


LEGACY_ACCESS_KEYS = ("user_whitelist", "user_blacklist")
REMOVED_CONFIG_KEYS = (
    "prefix", *LEGACY_ACCESS_KEYS,
    "interface_mode", "image_edit_transport", "base_url", "api_keys",
    "text_to_image_api_url", "text_to_image_api_keys", "model_list", "model", "text_to_image_model",
    "image_resolution", "image_aspect_ratio", "timeout", "use_stream",
    "enable_binary_image_response", "enable_bare_base64_response", "stream_heartbeat_tolerant",
    "use_proxy", "proxy_url", "image_output_format", "image_frame_count",
    "image_frame_duration_ms", "image_complexity", "image_quality",
    "api_mode", "generic_api_url", "generic_api_keys", "gemini_api_url", "gemini_api_keys",
    "generic_prefer_images_api",
)


def cleanup_removed_config(config: MutableMapping[str, Any]) -> list[str]:
    """Remove top-level residues without touching any configured channel or preset."""
    removed = [key for key in REMOVED_CONFIG_KEYS if key in config]
    for key in removed:
        config.pop(key, None)
    return removed


def restore_legacy_access_snapshot(config: MutableMapping[str, Any], snapshot: Any) -> None:
    """Recover old access lists only long enough to merge them into their new names."""
    if not isinstance(snapshot, dict):
        return
    for key in LEGACY_ACCESS_KEYS:
        if key not in config and key in snapshot:
            config[key] = snapshot[key]

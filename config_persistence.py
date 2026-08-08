"""Rules for resolving native AstrBot configuration and local fallbacks."""

from collections.abc import Collection, Mapping
from typing import Any


def is_empty_config_value(value: Any) -> bool:
    """Return whether a value represents an unset configuration value."""
    return value is None or value == "" or value == [] or value == {}


def merge_native_fallback_snapshots(
    previous_snapshots: Mapping[str, Any],
    *,
    override_keys: Collection[str],
    changed_dynamic_keys: Collection[str],
    native_values_before_save: Mapping[str, Any],
    native_save_succeeded: bool,
) -> dict[str, Any]:
    """Build the native-value snapshots retained with active fallbacks.

    Only values obtained from the persisted native file are recorded. If that
    file could not be read, the fallback remains compatible with the older
    freshness-based recovery path instead of inventing a misleading snapshot.
    """
    if native_save_succeeded:
        return {}

    active_overrides = set(override_keys)
    snapshots = {
        key: value
        for key, value in previous_snapshots.items()
        if key in active_overrides
    }
    for key in changed_dynamic_keys:
        if key in active_overrides and key in native_values_before_save:
            snapshots[key] = native_values_before_save[key]
    return snapshots


def should_restore_fallback_value(
    key: str,
    dynamic_value: Any,
    *,
    dynamic_keys: Collection[str],
    legacy_restore_keys: Collection[str],
    current_config: Mapping[str, Any],
    has_override_meta: bool,
    schema_defaults: Mapping[str, Any],
    has_native_snapshot: bool = False,
    native_value_at_fallback: Any = None,
    native_config_is_newer: bool | None = None,
) -> tuple[bool, str]:
    """Decide whether a local dynamic-config fallback may replace one value.

    The fallback records the native value that existed when a native save
    failed. If the current native value differs from that snapshot, it was
    subsequently saved by an administrator and wins. If it is unchanged, the
    fallback is newer and must win, including an empty string that means
    "automatic". Old fallback files without a snapshot use file freshness as
    a conservative compatibility path.
    """
    if key not in dynamic_keys:
        return False, "unknown-key"

    has_default = key in schema_defaults
    current_exists = key in current_config
    current_value = current_config.get(key) if current_exists else None
    # AstrBot normalizes missing/null schema entries to their default during
    # config loading. Treat a missing value captured before that normalization
    # the same way, otherwise a schema migration can look like a user save.
    if (not current_exists or current_value is None) and has_default:
        current_value = schema_defaults.get(key)
    current_is_default = has_default and current_value == schema_defaults.get(key)

    if has_override_meta:
        if has_native_snapshot:
            snapshot_value = native_value_at_fallback
            if snapshot_value is None and has_default:
                snapshot_value = schema_defaults.get(key)
            if current_value != snapshot_value:
                return False, "native-changed"
            if current_exists and current_value == dynamic_value:
                return False, "same-value"
            return True, "fallback-value"

        if native_config_is_newer is True:
            return False, "newer-native-config"
        if native_config_is_newer is False:
            return True, "fallback-newer"
        if current_exists and not current_is_default:
            return False, "keep-native-value"
        return True, "fallback-value"

    if key not in legacy_restore_keys:
        return False, "legacy-url-or-unsupported"
    if is_empty_config_value(dynamic_value):
        return False, "legacy-empty"
    if has_default and dynamic_value == schema_defaults.get(key):
        return False, "legacy-default"
    if native_config_is_newer is True:
        return False, "newer-native-config"
    if native_config_is_newer is False:
        return True, "fallback-newer"
    if current_exists and not current_is_default:
        return False, "keep-native-value"
    return True, "legacy-compatible"

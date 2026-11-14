"""Build platform-scoped whitelist keys from AstrBot's standard event API."""

from typing import Any


def access_session_key(event: Any, group_id: str = "") -> str:
    """Identify a room independently of per-member conversation isolation.

    Group/channel messages use the actual room ID. Private messages retain
    their exact AstrBot origin, so allowing one DM never enables every DM.
    IDs are opaque strings: QQ Official OpenIDs need no numeric conversion.
    """
    origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
    parts = origin.split(":", 2)
    try:
        platform = str(event.get_platform_id() or "").strip()
    except (AttributeError, TypeError):
        platform = ""
    if not platform and len(parts) == 3:
        platform = parts[0]
    try:
        message_type = event.get_message_type()
        message_type = str(getattr(message_type, "value", message_type) or "").strip()
    except (AttributeError, TypeError):
        message_type = parts[1] if len(parts) == 3 else ""
    room = str(group_id or "").strip()
    if room and platform and message_type:
        return f"{platform}:{message_type}:{room}"
    if len(parts) == 3 and all(parts):
        return origin
    return ""

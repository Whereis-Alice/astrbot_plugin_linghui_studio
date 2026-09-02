"""Requester attribution helpers for group drawing results.

In a busy QQ group several people race for the drawing queue at once, which
makes it impossible to tell whose picture (or whose failure notice) just landed.
These helpers build a short, display-only "nickname(QQ)" tag so every outgoing
notice can carry its owner.  Identity itself stays keyed by QQ id everywhere
else, because group members rename themselves constantly; the nickname is
decoration only.
"""

from __future__ import annotations

import re
from typing import Any

TAG_MODES = ("off", "name", "at")
DEFAULT_TAG_MODE = "name"
NICKNAME_LIMIT = 16

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_tag_mode(value: Any) -> str:
    """Coerce a configured value into one of :data:`TAG_MODES`."""
    mode = str(value or "").strip().lower()
    return mode if mode in TAG_MODES else DEFAULT_TAG_MODE


def shorten_nickname(nickname: Any, limit: int = NICKNAME_LIMIT) -> str:
    """Collapse whitespace and clip long nicknames so tags stay one line."""
    text = _WHITESPACE_RE.sub(" ", str(nickname or "")).strip()
    if limit > 0 and len(text) > limit:
        return f"{text[:limit]}…"
    return text


def build_requester_label(nickname: Any, user_id: Any, group_id: Any = "") -> str:
    """Build the display label.  Private chats have one requester, so no label."""
    if not str(group_id or "").strip():
        return ""
    uid = str(user_id or "").strip()
    name = shorten_nickname(nickname)
    if name and uid:
        return f"{name}({uid})"
    return name or uid


def build_prefix_text(mode: Any, nickname: Any, user_id: Any, group_id: Any) -> str:
    """Return the ``[nickname(QQ)] `` prefix, or an empty string when disabled."""
    if normalize_tag_mode(mode) == "off":
        return ""
    label = build_requester_label(nickname, user_id, group_id)
    return f"[{label}] " if label else ""


def should_mention_requester(mode: Any, user_id: Any, group_id: Any) -> bool:
    """Whether an ``At`` node should be prepended for this requester."""
    if normalize_tag_mode(mode) != "at":
        return False
    return bool(str(group_id or "").strip() and str(user_id or "").strip())


def apply_prefix(text: Any, prefix: str) -> str:
    """Insert the prefix after leading newlines so it shares the first visible line."""
    body = str(text)
    if not prefix:
        return body
    stripped = body.lstrip("\n")
    leading = body[: len(body) - len(stripped)]
    return f"{leading}{prefix}{stripped}"

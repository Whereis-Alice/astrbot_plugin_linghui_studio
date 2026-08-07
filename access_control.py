"""Access and quota-exemption rules for Linghui Studio.

The policy deliberately keeps "may use the plugin" separate from "does not
consume credits".  This prevents an allowed group from accidentally becoming
an unlimited group, which was a confusing behavior in the upstream plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


def _ids(value: Any) -> set[str]:
    """Normalize dashboard/config list inputs into stable string IDs."""
    if value is None:
        return set()
    if isinstance(value, str):
        values: Iterable[Any] = re.split(r"[\s,;，；]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = (value,)
    return {str(item).strip() for item in values if str(item).strip()}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    unlimited: bool = False
    level: str = "limited"
    message: str = ""


class AccessPolicy:
    """Evaluate access before any quota balance is considered."""

    def __init__(self, config: Any):
        self.config = config

    def _list(self, primary: str, *legacy: str) -> set[str]:
        values = _ids(self.config.get(primary, []))
        for key in legacy:
            values.update(_ids(self.config.get(key, [])))
        return values

    def evaluate(
        self,
        user_id: str,
        group_id: str,
        *,
        is_admin: bool = False,
    ) -> AccessDecision:
        user_id = str(user_id or "").strip()
        group_id = str(group_id or "").strip()

        blocked_users = self._list("blocked_users", "user_blacklist")
        blocked_groups = self._list("group_blacklist")
        if user_id and user_id in blocked_users:
            return AccessDecision(False, level="blocked_user", message="你没有使用灵绘的权限。")
        if group_id and group_id in blocked_groups:
            return AccessDecision(False, level="blocked_group", message="这个群没有使用灵绘的权限。")

        allowed_users = self._list("allowed_users", "user_whitelist")
        # Administrators may be quota-exempt, but that must not silently turn
        # an unapproved user or group into an allowed drawing target.
        if allowed_users and user_id not in allowed_users:
            return AccessDecision(False, level="user_not_allowed", message="你不在灵绘用户白名单中。")

        if not group_id:
            if not bool(self.config.get("allow_private_messages", False)):
                return AccessDecision(False, level="private_disabled", message="灵绘当前只在已授权群聊中开放。")
        else:
            group_mode = str(self.config.get("group_access_mode", "whitelist") or "whitelist").strip().lower()
            group_whitelist = self._list("group_whitelist")
            if group_mode == "whitelist" and group_id not in group_whitelist:
                return AccessDecision(False, level="group_not_allowed", message="这个群不在灵绘群白名单中。")

        if is_admin and bool(self.config.get("admins_unlimited", True)):
            return AccessDecision(True, unlimited=True, level="admin", message="管理员额度豁免")

        unlimited_users = self._list("unlimited_users")
        unlimited_groups = self._list("unlimited_groups")
        if user_id and user_id in unlimited_users:
            return AccessDecision(True, unlimited=True, level="unlimited_user", message="用户无限次数名单")
        if group_id and group_id in unlimited_groups:
            return AccessDecision(True, unlimited=True, level="unlimited_group", message="群无限次数名单")

        return AccessDecision(True)

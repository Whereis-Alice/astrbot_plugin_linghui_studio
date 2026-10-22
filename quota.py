"""Shared quota decisions for every drawing entry point."""

from typing import Any


class QuotaDeniedError(RuntimeError):
    """The balance changed between the initial check and task submission."""


def global_quota_status(config: Any, used: int) -> dict[str, Any]:
    limit = int(config.get("global_total_limit", -1))
    used = max(0, int(used))
    return {
        "enabled": limit >= 0,
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used) if limit >= 0 else None,
    }


def quota_decision(
    config: Any, uid: str, gid: str, cost: int,
    user_balance: int, group_balance: int, global_used: int,
    *, unlimited: bool = False,
) -> dict[str, Any]:
    """A global cap applies even when local balances are exempt or disabled."""
    status = global_quota_status(config, global_used)
    if status["enabled"] and cost > status["remaining"]:
        return {
            "allowed": False, "source": None, "access_level": "global_quota_exhausted",
            "msg": (
                f"生成失败：全局总次数不足（本次需要 {cost} 次，"
                f"所有会话合计已用 {status['used']}/{status['limit']} 次，"
                f"全局剩余 {status['remaining']} 次）。"
                "请联系管理员增加总次数；签到或补充个人/群额度不会增加全局总次数。"
            ),
        }
    enable_user = bool(config.get("enable_user_limit", True))
    enable_group = bool(config.get("enable_group_limit", False))
    source = None
    if unlimited or not (enable_user or enable_group):
        source = "free"
    elif enable_user and user_balance >= cost:
        source = "user"
    elif gid and enable_group and group_balance >= cost:
        source = "group"
    if source:
        return {"allowed": True, "source": source, "msg": ""}
    return {
        "allowed": False, "source": None,
        "msg": f"额度不足（需要 {cost} 次，个人剩余 {user_balance}，群剩余 {group_balance if gid and enable_group else 0}），请联系管理员补充。",
    }

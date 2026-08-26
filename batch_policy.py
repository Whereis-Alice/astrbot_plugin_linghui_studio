"""Batch failure policy guard for Linghui Studio.

Provides a small, asyncio-safe helper that decides whether a batch drawing run
should keep going after individual images fail.

Policies
--------
``skip``
    Ignore failures and keep processing every remaining item (legacy behaviour).
``stop``
    Abort the whole batch as soon as one item fails.
``skip_limit``
    Tolerate up to ``max_skips`` failures, then abort the remaining items.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

POLICY_SKIP = "skip"
POLICY_STOP = "stop"
POLICY_SKIP_LIMIT = "skip_limit"

POLICY_CHOICES = (POLICY_SKIP, POLICY_STOP, POLICY_SKIP_LIMIT)

POLICY_LABELS = {
    POLICY_SKIP: "跳过继续",
    POLICY_STOP: "立即停止",
    POLICY_SKIP_LIMIT: "限量跳过",
}

_ALIASES = {
    "skip": POLICY_SKIP,
    "continue": POLICY_SKIP,
    "ignore": POLICY_SKIP,
    "跳过": POLICY_SKIP,
    "跳过继续": POLICY_SKIP,
    "stop": POLICY_STOP,
    "abort": POLICY_STOP,
    "fail_fast": POLICY_STOP,
    "立即停止": POLICY_STOP,
    "停止": POLICY_STOP,
    "skip_limit": POLICY_SKIP_LIMIT,
    "skip-limit": POLICY_SKIP_LIMIT,
    "limit": POLICY_SKIP_LIMIT,
    "限量跳过": POLICY_SKIP_LIMIT,
    "最多跳过": POLICY_SKIP_LIMIT,
}


def normalize_policy(value: Any) -> str:
    """Return a canonical policy name, defaulting to ``skip``."""

    text = str(value or "").strip().lower()
    if not text:
        return POLICY_SKIP
    return _ALIASES.get(text, POLICY_SKIP)


def policy_label(value: Any) -> str:
    policy = normalize_policy(value)
    return POLICY_LABELS.get(policy, POLICY_LABELS[POLICY_SKIP])


class BatchFailureGuard:
    """Track batch failures and tell callers whether to continue."""

    def __init__(self, policy: Any = POLICY_SKIP, max_skips: Any = 3) -> None:
        self._policy = normalize_policy(policy)
        try:
            limit = int(max_skips)
        except (TypeError, ValueError):
            limit = 3
        self._max_skips = max(0, min(200, limit))
        self._failures = 0
        self._skipped = 0
        self._aborted = False
        self._abort_reason = ""
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def max_skips(self) -> int:
        return self._max_skips

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def abort_reason(self) -> str:
        return self._abort_reason

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def skipped(self) -> int:
        return self._skipped

    async def note_failure(self, detail: str = "") -> str:
        """Register a failed item; returns ``"continue"`` or ``"abort"``."""

        async with self._lock:
            self._failures += 1
            if self._policy == POLICY_STOP:
                self._aborted = True
                self._abort_reason = detail or "已按“立即停止”策略中止剩余任务"
                return "abort"
            if self._policy == POLICY_SKIP_LIMIT and self._failures > self._max_skips:
                self._aborted = True
                self._abort_reason = (
                    detail
                    or f"失败数已超过允许跳过的上限（{self._max_skips} 张），中止剩余任务"
                )
                return "abort"
            return "continue"

    async def note_skip(self) -> None:
        """Register an item that was skipped because the batch already aborted."""

        async with self._lock:
            self._skipped += 1

    def describe(self) -> str:
        if self._policy == POLICY_STOP:
            return "失败即停止"
        if self._policy == POLICY_SKIP_LIMIT:
            return f"最多跳过 {self._max_skips} 张失败"
        return "失败跳过并继续"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "policy": self._policy,
            "policy_label": POLICY_LABELS.get(self._policy, self._policy),
            "max_skips": self._max_skips,
            "failures": self._failures,
            "skipped": self._skipped,
            "aborted": self._aborted,
            "abort_reason": self._abort_reason,
        }

    def summary_suffix(self) -> str:
        """Short human readable note appended to batch result messages."""

        if not self._aborted:
            return ""
        parts = [self._abort_reason or "批量任务已提前结束"]
        if self._skipped:
            parts.append(f"跳过 {self._skipped} 张未执行")
        return "；".join(parts)

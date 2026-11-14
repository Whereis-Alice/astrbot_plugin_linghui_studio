"""Small persistent aggregate of real channel outcomes, independent of configuration."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

from astrbot import logger

from .error_classify import safe_error_summary


def empty_health_state() -> dict[str, Any]:
    return {
        "successes": 0, "failures": 0, "fallback_successes": 0,
        "consecutive_failures": 0, "cooldown_until": 0.0,
        "last_error_category": "", "last_error": "",
        "last_success_at": 0.0, "last_failure_at": 0.0, "durations": [],
    }


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


def _count(value: Any) -> int:
    parsed = _number(value)
    return min(int(parsed), 2**63 - 1)


class ChannelHealthStore:
    """Atomically save bounded aggregates after each completed channel route.

    Runtime cooldown deadlines are monotonic. On disk they are epoch deadlines,
    so a process restart cannot reset the cooldown or extend it by its uptime.
    A statistics write failure never changes a successful generation's result.
    """

    def __init__(self, path: Path | None = None, *, wall_clock=None, monotonic_clock=None):
        self.path = Path(path) if path else None
        self.wall_clock = wall_clock or time.time
        self.monotonic_clock = monotonic_clock or time.monotonic
        self.channels: dict[str, dict[str, Any]] = {}
        self.statistics_since = 0.0
        self.history_seeded = False
        self.history_records = 0
        self.last_updated_at = 0.0
        self.persistence_error = ""
        self.load_warning = ""
        self.dirty = False
        self._loaded = False
        self._lock = threading.RLock()
        self._load()

    @property
    def needs_history_seed(self) -> bool:
        return not self._loaded and not self.history_seeded

    def state(self, channel_id: str) -> dict[str, Any]:
        return self.channels.setdefault(channel_id, empty_health_state())

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("channels"), dict):
                raise ValueError("invalid health document")
        except (OSError, UnicodeError, ValueError):
            self.load_warning = "历史渠道统计文件无法读取，已等待从保留任务恢复。"
            logger.warning("Linghui channel health could not be loaded; retained task history can recover known outcomes.")
            return

        bad_rows = 0
        now, monotonic = self.wall_clock(), self.monotonic_clock()
        for channel_id, raw in payload["channels"].items():
            if not isinstance(channel_id, str) or not channel_id.strip() or len(channel_id) > 160 or not isinstance(raw, dict):
                bad_rows += 1
                continue
            state = empty_health_state()
            for key in ("successes", "failures", "fallback_successes", "consecutive_failures"):
                state[key] = _count(raw.get(key))
            state["fallback_successes"] = min(state["fallback_successes"], state["successes"])
            state["consecutive_failures"] = min(state["consecutive_failures"], state["failures"])
            for key in ("last_success_at", "last_failure_at"):
                state[key] = _number(raw.get(key))
            state["last_error_category"] = str(raw.get("last_error_category", "") or "")[:80]
            state["last_error"] = safe_error_summary(raw.get("last_error", ""), 240)
            remaining = min(3600.0, max(0.0, _number(raw.get("cooldown_until")) - now))
            state["cooldown_until"] = monotonic + remaining if remaining else 0.0
            durations = raw.get("durations", [])
            if isinstance(durations, list):
                state["durations"] = [value for item in durations[-200:]
                                      if (value := _number(item, -1.0)) >= 0]
            self.channels[channel_id] = state
        self._loaded = not payload["channels"] or bool(self.channels)
        self.statistics_since = _number(payload.get("statistics_since"))
        self.history_records = _count(payload.get("history_records"))
        self.history_seeded = self._loaded and bool(payload.get("history_seeded", True))
        self.last_updated_at = _number(payload.get("last_updated_at"))
        if bad_rows:
            self.load_warning = f"已保留可读渠道统计，跳过 {bad_rows} 项损坏记录。"
            logger.warning("Linghui retained readable channel statistics; skipped %s malformed rows.", bad_rows)

    def changed(self, *, timestamp: float | None = None) -> None:
        """Mark a live outcome and save it before reporting the route result."""
        with self._lock:
            at = self.wall_clock() if timestamp is None else _number(timestamp)
            if not self.statistics_since:
                self.statistics_since = at
            self.last_updated_at = self.wall_clock()
            self.history_seeded = True
            self.dirty = True
            self.save()

    def save(self) -> bool:
        if self.path is None or not self.dirty:
            return True
        temporary: str | None = None
        with self._lock:
            try:
                now, monotonic = self.wall_clock(), self.monotonic_clock()
                channels = {}
                for channel_id, state in self.channels.items():
                    row = dict(state)
                    remaining = max(0.0, _number(state.get("cooldown_until")) - monotonic)
                    row["cooldown_until"] = now + remaining if remaining else 0.0
                    row["durations"] = list(state.get("durations", []))[-200:]
                    channels[channel_id] = row
                payload = {
                    "version": 1, "statistics_since": self.statistics_since,
                    "last_updated_at": self.last_updated_at, "history_seeded": self.history_seeded,
                    "history_records": self.history_records, "channels": channels,
                }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=self.path.name + ".", suffix=".tmp", delete=False) as stream:
                    temporary = stream.name
                    json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self.dirty = False
                self.persistence_error = ""
                self._loaded = True
                return True
            except (OSError, TypeError, ValueError):
                if not self.persistence_error:
                    logger.exception("Linghui could not persist channel health; keeping outcomes in memory for retry.")
                self.persistence_error = "渠道统计暂未保存到磁盘，正在保留内存记录并重试。"
                return False
            finally:
                if temporary:
                    try:
                        Path(temporary).unlink(missing_ok=True)
                    except OSError:
                        pass

    def seed_history(self, recovery: dict[str, Any]) -> bool:
        """Persist an evidenced historical aggregate exactly once, without cooldown."""
        with self._lock:
            if not self.needs_history_seed:
                return False
            for channel_id, raw in recovery.get("channels", {}).items():
                if not isinstance(channel_id, str) or not channel_id or not isinstance(raw, dict):
                    continue
                state = self.state(channel_id)
                for key in ("successes", "failures", "fallback_successes", "consecutive_failures"):
                    state[key] = _count(raw.get(key))
                for key in ("last_success_at", "last_failure_at"):
                    state[key] = _number(raw.get(key))
                state["last_error_category"] = str(raw.get("last_error_category", "") or "")[:80]
                state["last_error"] = safe_error_summary(raw.get("last_error", ""), 240)
                durations = raw.get("durations", [])
                if isinstance(durations, list):
                    state["durations"] = [value for item in durations[-200:]
                                          if (value := _number(item, -1.0)) >= 0]
                state["cooldown_until"] = 0.0
            self.statistics_since = _number(recovery.get("recovered_since")) or self.wall_clock()
            self.history_records = _count(recovery.get("recovered_routes"))
            self.history_seeded = True
            self.last_updated_at = self.wall_clock()
            self.dirty = True
            self.save()
            return True

    def metadata(self) -> dict[str, Any]:
        self.save()
        return {
            "persistent": self.path is not None,
            "persistence_ok": not bool(self.persistence_error),
            "persistence_error": self.persistence_error,
            "load_warning": self.load_warning,
            "statistics_since": self.statistics_since,
            "history_seeded": self.history_seeded,
            "history_records": self.history_records,
            "history_recovered": self.history_records > 0,
            "last_updated_at": self.last_updated_at,
            "duration_sample_limit": 200,
        }

"""会话级绘图覆盖（模型 / 渠道）。

管理员在某个群里说「模型 gpt-image-1」时，通常只是想让这个群换模型，
而不是改动全局默认值。这个模块把「谁在哪个会话里临时选了什么」单独存盘，
并带 TTL 自动过期，避免一次临时切换永久影响其它会话。

存储放在插件数据目录下的 session_overrides.json，写入使用临时文件 +
os.replace 原子替换，防止进程被杀时留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_DEFAULT_TTL_MINUTES = 720
_MIN_TTL_MINUTES = 1
_MAX_TTL_MINUTES = 60 * 24 * 30
_MAX_SESSIONS = 2000


def _now() -> float:
    return time.time()


def _clamp_ttl(minutes: Any) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        value = _DEFAULT_TTL_MINUTES
    return min(max(value, _MIN_TTL_MINUTES), _MAX_TTL_MINUTES)


class SessionOverrideStore:
    """线程安全的会话覆盖存储。"""

    def __init__(self, path: str, ttl_minutes: Any = _DEFAULT_TTL_MINUTES):
        self._path = str(path)
        self._ttl_minutes = _clamp_ttl(ttl_minutes)
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            return
        for session_id, entry in sessions.items():
            if not isinstance(entry, dict):
                continue
            self._sessions[str(session_id)] = {
                "model": str(entry.get("model", "") or ""),
                "channel_id": str(entry.get("channel_id", "") or ""),
                "label": str(entry.get("label", "") or ""),
                "scope": str(entry.get("scope", "") or ""),
                "updated_at": float(entry.get("updated_at", 0.0) or 0.0),
                "expires_at": float(entry.get("expires_at", 0.0) or 0.0),
            }

    def _flush(self) -> None:
        payload = {"version": 1, "ttl_minutes": self._ttl_minutes, "sessions": self._sessions}
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ---------------------------------------------------------------------- 内部

    def _prune(self) -> bool:
        now = _now()
        expired = [sid for sid, entry in self._sessions.items() if 0 < entry.get("expires_at", 0.0) <= now]
        for sid in expired:
            self._sessions.pop(sid, None)
        if len(self._sessions) > _MAX_SESSIONS:
            ordered = sorted(self._sessions.items(), key=lambda item: item[1].get("updated_at", 0.0))
            for sid, _entry in ordered[: len(self._sessions) - _MAX_SESSIONS]:
                self._sessions.pop(sid, None)
                expired.append(sid)
        return bool(expired)

    def _entry(self, session_id: str) -> dict[str, Any]:
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = {
                "model": "",
                "channel_id": "",
                "label": "",
                "scope": "",
                "updated_at": 0.0,
                "expires_at": 0.0,
            }
            self._sessions[session_id] = entry
        return entry

    def _touch(self, entry: dict[str, Any], label: str, scope: str) -> None:
        now = _now()
        entry["updated_at"] = now
        entry["expires_at"] = now + self._ttl_minutes * 60
        if label:
            entry["label"] = label
        if scope:
            entry["scope"] = scope

    # ---------------------------------------------------------------------- 对外

    @property
    def ttl_minutes(self) -> int:
        return self._ttl_minutes

    def set_ttl_minutes(self, minutes: Any) -> int:
        with self._lock:
            self._load()
            resolved = _clamp_ttl(minutes)
            if resolved == self._ttl_minutes:
                return resolved
            self._ttl_minutes = resolved
            # 立刻按新 TTL 重排已有条目的到期时间，避免旧值长期残留。
            for entry in self._sessions.values():
                base = float(entry.get("updated_at", 0.0) or _now())
                entry["expires_at"] = base + resolved * 60
            self._prune()
            self._flush()
            return resolved

    def get(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {}
        with self._lock:
            self._load()
            if self._prune():
                self._flush()
            entry = self._sessions.get(session_id)
            return dict(entry) if entry else {}

    def get_model(self, session_id: str) -> str:
        return str(self.get(session_id).get("model", "") or "")

    def get_channel(self, session_id: str) -> str:
        return str(self.get(session_id).get("channel_id", "") or "")

    def set_model(self, session_id: str, model: str, label: str = "", scope: str = "") -> bool:
        session_id = str(session_id or "").strip()
        model = str(model or "").strip()
        if not session_id:
            return False
        with self._lock:
            self._load()
            self._prune()
            if not model:
                return self._clear_field(session_id, "model")
            entry = self._entry(session_id)
            entry["model"] = model
            self._touch(entry, label, scope)
            self._flush()
            return True

    def set_channel(self, session_id: str, channel_id: str, label: str = "", scope: str = "") -> bool:
        session_id = str(session_id or "").strip()
        channel_id = str(channel_id or "").strip()
        if not session_id:
            return False
        with self._lock:
            self._load()
            self._prune()
            if not channel_id:
                return self._clear_field(session_id, "channel_id")
            entry = self._entry(session_id)
            entry["channel_id"] = channel_id
            self._touch(entry, label, scope)
            self._flush()
            return True

    def _clear_field(self, session_id: str, field: str) -> bool:
        entry = self._sessions.get(session_id)
        if not entry or not entry.get(field):
            return False
        entry[field] = ""
        if not entry.get("model") and not entry.get("channel_id"):
            self._sessions.pop(session_id, None)
        else:
            entry["updated_at"] = _now()
        self._flush()
        return True

    def clear(self, session_id: str) -> bool:
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        with self._lock:
            self._load()
            removed = self._sessions.pop(session_id, None) is not None
            self._prune()
            if removed:
                self._flush()
            return removed

    def clear_all(self) -> int:
        with self._lock:
            self._load()
            count = len(self._sessions)
            self._sessions = {}
            self._flush()
            return count

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load()
            if self._prune():
                self._flush()
            items = []
            for session_id, entry in self._sessions.items():
                item = dict(entry)
                item["session_id"] = session_id
                items.append(item)
        items.sort(key=lambda item: item.get("updated_at", 0.0), reverse=True)
        return items


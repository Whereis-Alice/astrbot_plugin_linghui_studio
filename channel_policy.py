"""Channel configuration validation and persistent per-server daily quotas."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .generation_params import normalize_aspect_ratio, normalize_resolution
from .utils import normalize_model_list


def normalize_policy(raw: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    old = old or {}
    result: dict[str, Any] = {}
    for key, default, minimum, maximum in (
        ("priority", 0, -1000000, 1000000),
        ("daily_limit", -1, -1, 2147483647),
    ):
        value = raw.get(key, old.get(key, default))
        if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
            raise ValueError(f"渠道 {key} 必须为整数。")
        value = int(value)
        if not minimum <= value <= maximum:
            raise ValueError(f"渠道 {key} 超出允许范围 {minimum} 至 {maximum}。")
        result[key] = value
    models = raw.get("models", old.get("models", []))
    if not isinstance(models, (str, list)) or (isinstance(models, list) and any(not isinstance(m, str) for m in models)):
        raise ValueError("渠道回退模型必须是字符串列表或每行一个模型。")
    result["models"] = normalize_model_list(models.splitlines() if isinstance(models, str) else models)
    if len(result["models"]) > 32 or any(len(m) > 160 for m in result["models"]):
        raise ValueError("每个渠道最多配置 32 个模型，模型名最多 160 字符。")
    for key, normalizer in (("image_resolution", normalize_resolution), ("image_aspect_ratio", normalize_aspect_ratio)):
        value = str(raw.get(key, old.get(key, "")) or "").strip()
        normalized = normalizer(value) if value else ""
        if value and ((key == "image_resolution" and value.upper() not in {"1K", "2K", "4K"}) or
                      (key == "image_aspect_ratio" and not normalize_aspect_ratio(value, ""))):
            raise ValueError(f"渠道 {key} 无效。")
        result[key] = normalized
    return result


class ChannelDailyQuota:
    """Reserve before dispatch; failures refund, crashes conservatively stay spent.

    Mutations contain no await and are atomic within a process. Each server owns
    its own state; configuration export never contains usage counters.
    """

    def __init__(self, path: Path | None = None, clock=None):
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.now(timezone(timedelta(hours=8))))
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {"day": self.day(), "used": {}}
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("used"), dict) or not isinstance(loaded.get("day"), str):
                raise ValueError("渠道日额度文件损坏，请恢复备份后重载插件。")
            datetime.strptime(loaded["day"], "%Y-%m-%d")
            if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in loaded["used"].values()):
                raise ValueError("渠道日额度计数无效。")
            self.state = loaded

    def day(self) -> str:
        return self.clock().astimezone(timezone(timedelta(hours=8))).date().isoformat()

    def _rollover(self) -> None:
        day = self.day()
        if self.state["day"] != day:
            self.state = {"day": day, "used": {}}

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)

    def snapshot(self, channel: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._rollover()
            limit = int(channel.get("daily_limit", -1))
            used = self.state["used"].get(channel["id"], 0)
            return {"quota_day": self.state["day"], "daily_limit": limit,
                    "daily_used": used, "daily_remaining": max(0, limit - used) if limit >= 0 else -1,
                    "quota_exhausted": limit >= 0 and used >= limit,
                    "quota_reset": "北京时间次日 00:00"}

    def reserve(self, channel: dict[str, Any]) -> tuple[str, str] | None:
        with self.lock:
            if self.snapshot(channel)["quota_exhausted"]:
                return None
            channel_id = channel["id"]
            self.state["used"][channel_id] = self.state["used"].get(channel_id, 0) + 1
            try:
                self._save()
            except Exception:
                self.state["used"][channel_id] -= 1
                raise
            return self.state["day"], channel_id

    def refund(self, token: tuple[str, str]) -> None:
        with self.lock:
            self._rollover()
            day, channel_id = token
            if day == self.state["day"]:
                previous = self.state["used"].get(channel_id, 0)
                self.state["used"][channel_id] = max(0, previous - 1)
                try:
                    self._save()
                except Exception:
                    self.state["used"][channel_id] = previous
                    raise


def extract_channel_option(prompt: str) -> tuple[str, str]:
    """Only a leading option is syntax; prose and quoted prompts remain intact."""
    match = re.match(r"^--(?:渠道|channel)(?:=|\s+)([A-Za-z0-9_-]+)(?:\s+|$)", prompt.strip(), re.I)
    if match:
        return prompt.strip()[match.end():].strip(), match.group(1)
    if re.match(r"^--(?:渠道|channel)(?:\s|=|$)", prompt.strip(), re.I):
        raise ValueError("用法：bnn --渠道 渠道ID 提示词（渠道 ID 可在 WebUI 查看）。")
    return prompt, ""

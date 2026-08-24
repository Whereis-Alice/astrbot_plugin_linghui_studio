"""Stable daily persona appearance and time-of-day state management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot import logger


DEFAULT_OUTFITS = [
    "舒适的浅色针织上衣、百褶短裙和简洁的小配饰",
    "干净的白衬衫、深色高腰裙和低调发饰",
    "柔软连帽外套、休闲短裙和轻便运动鞋",
    "简约连衣裙、薄款开衫和小巧耳饰",
    "宽松卫衣、百褶裙和日常帆布鞋",
    "清爽的短袖上衣、轻盈半身裙和细致发夹",
    "温柔的针织开衫、内搭衬衫和深色短裙",
]

DEFAULT_MOODS = [
    "轻松愉快，表情自然温柔",
    "有一点俏皮，眼神灵动",
    "安静放松，带着浅浅微笑",
    "精神很好，显得活力十足",
    "略微慵懒，但心情平稳",
    "带一点好奇和期待感",
    "从容自在，神态亲近自然",
]

DEFAULT_TIME_PROMPTS = {
    "morning": "清晨状态，光线柔和清透，刚开始一天，精神自然",
    "day": "白天状态，自然日光充足，动作从容，生活感真实",
    "evening": "傍晚状态，暖色余晖或室内灯光，氛围舒缓",
    "night": "夜间状态，环境光柔和，带一点安静放松的夜晚氛围",
}

_CLOTHING_WORDS = (
    "换衣", "换装", "穿上", "穿着", "衣服", "裙", "制服", "校服", "女仆",
    "和服", "旗袍", "汉服", "洛丽塔", "lolita", "婚纱", "西装", "礼服",
    "泳装", "比基尼", "水手服", "cos", "cosplay", "同款", "这件", "那件",
    "这套", "那套", "穿这个", "穿那个", "试穿", "穿搭",
)

_MOOD_WORDS = (
    "开心", "高兴", "难过", "伤心", "生气", "冷淡", "害羞", "脸红", "哭",
    "微笑", "大笑", "严肃", "慵懒", "疲惫", "困", "兴奋", "俏皮", "表情",
)


class PersonaProfileManager:
    """Persist one outfit and mood per calendar day.

    The date-derived selection is deterministic, so concurrent requests and
    plugin reloads cannot randomly change today's appearance. Administrators
    can still edit or refresh the current state from Dashboard.
    """

    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config
        self.state_file = self.data_dir / "persona_daily_state.json"
        self._state: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def _timezone(self):
        value = str(self.config.get("persona_state_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError:
            logger.warning("Linghui persona timezone %s is invalid; using local timezone.", value)
            return datetime.now().astimezone().tzinfo

    def now(self) -> datetime:
        return datetime.now(self._timezone())

    @staticmethod
    def _normalize_list(value: Any, defaults: List[str]) -> List[str]:
        if isinstance(value, str):
            raw = value.splitlines()
        elif isinstance(value, list):
            raw = value
        else:
            raw = []
        result: List[str] = []
        for item in raw:
            text = re.sub(r"\s+", " ", str(item or "")).strip()[:500]
            if text and text not in result:
                result.append(text)
        return result or list(defaults)

    def outfits(self) -> List[str]:
        return self._normalize_list(self.config.get("persona_daily_outfits", []), DEFAULT_OUTFITS)

    def moods(self) -> List[str]:
        return self._normalize_list(self.config.get("persona_daily_moods", []), DEFAULT_MOODS)

    def time_prompts(self) -> Dict[str, str]:
        result = dict(DEFAULT_TIME_PROMPTS)
        raw = self.config.get("persona_time_period_prompts", [])
        if isinstance(raw, str):
            raw = raw.splitlines()
        if isinstance(raw, list):
            aliases = {
                "早晨": "morning", "清晨": "morning", "morning": "morning",
                "白天": "day", "中午": "day", "下午": "day", "day": "day",
                "傍晚": "evening", "黄昏": "evening", "evening": "evening",
                "夜间": "night", "晚上": "night", "深夜": "night", "night": "night",
            }
            for item in raw:
                if ":" not in str(item):
                    continue
                key, prompt = str(item).split(":", 1)
                normalized = aliases.get(key.strip().lower()) or aliases.get(key.strip())
                prompt = re.sub(r"\s+", " ", prompt).strip()[:800]
                if normalized and prompt:
                    result[normalized] = prompt
        return result

    async def initialize(self) -> None:
        if self.state_file.is_file():
            try:
                raw = await asyncio.to_thread(self.state_file.read_text, encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    self._state = loaded
            except Exception as exc:
                logger.warning("Linghui could not load persona daily state: %s", exc)
        await self.get_state()

    def _pick(self, values: List[str], date_key: str, kind: str, refresh_token: int = 0) -> str:
        persona_name = str(self.config.get("persona_name", "") or "")
        salt = str(self.config.get("persona_daily_state_salt", "linghui") or "linghui")
        digest = hashlib.sha256(
            f"{salt}\0{persona_name}\0{date_key}\0{kind}\0{refresh_token}".encode("utf-8", "ignore")
        ).digest()
        index = int.from_bytes(digest[:8], "big") % max(1, len(values))
        return values[index]

    async def _save_locked(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        temp = self.state_file.with_suffix(".json.tmp")

        def write() -> None:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.state_file)

        await asyncio.to_thread(write)

    @staticmethod
    def _period_for(hour: int) -> str:
        if 5 <= hour < 10:
            return "morning"
        if 10 <= hour < 17:
            return "day"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    async def get_state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = now or self.now()
        date_key = current.date().isoformat()
        async with self._lock:
            if str(self._state.get("date", "")) != date_key:
                self._state = {
                    "date": date_key,
                    "outfit": self._pick(self.outfits(), date_key, "outfit"),
                    "mood": self._pick(self.moods(), date_key, "mood"),
                    "refresh_token": 0,
                    "updated_at": current.isoformat(timespec="seconds"),
                }
                await self._save_locked()
            period = self._period_for(current.hour)
            result = dict(self._state)
            result.update({
                "period": period,
                "period_prompt": self.time_prompts().get(period, ""),
                "character_type": self.character_type(),
                "timezone": str(self.config.get("persona_state_timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            })
            return result

    async def update_state(self, *, outfit: str = "", mood: str = "") -> Dict[str, Any]:
        current = self.now()
        async with self._lock:
            if str(self._state.get("date", "")) != current.date().isoformat():
                self._state = {
                    "date": current.date().isoformat(),
                    "refresh_token": 0,
                }
            if outfit.strip():
                self._state["outfit"] = re.sub(r"\s+", " ", outfit).strip()[:500]
            if mood.strip():
                self._state["mood"] = re.sub(r"\s+", " ", mood).strip()[:500]
            self._state.setdefault("outfit", self._pick(self.outfits(), self._state["date"], "outfit"))
            self._state.setdefault("mood", self._pick(self.moods(), self._state["date"], "mood"))
            self._state["updated_at"] = current.isoformat(timespec="seconds")
            await self._save_locked()
        return await self.get_state(current)

    async def refresh_state(self) -> Dict[str, Any]:
        current = self.now()
        async with self._lock:
            date_key = current.date().isoformat()
            refresh_token = int(self._state.get("refresh_token", 0) or 0) + 1
            self._state = {
                "date": date_key,
                "outfit": self._pick(self.outfits(), date_key, "outfit", refresh_token),
                "mood": self._pick(self.moods(), date_key, "mood", refresh_token),
                "refresh_token": refresh_token,
                "updated_at": current.isoformat(timespec="seconds"),
            }
            await self._save_locked()
        return await self.get_state(current)

    def character_type(self) -> str:
        value = str(self.config.get("persona_character_type", "auto") or "auto").strip().lower()
        return value if value in {"auto", "real", "anime"} else "auto"

    def character_type_prompt(self) -> str:
        kind = self.character_type()
        if kind == "real":
            return (
                "The persona is a real human appearance. Keep a natural photographic face, skin texture, "
                "anatomy, lighting, and camera realism while preserving identity."
            )
        if kind == "anime":
            return (
                "The persona is an anime or illustrated character. Preserve the original illustration identity, "
                "line language, facial design, hair shape, and stylized proportions instead of forcing a real-human face."
            )
        return (
            "Infer whether the persona reference is real-human or anime/illustrated, then preserve that same visual "
            "identity type consistently; do not arbitrarily convert between real and anime."
        )

    async def build_prompt_hint(self, extra_request: str = "") -> str:
        if not bool(self.config.get("enable_persona_daily_state", True)):
            return self.character_type_prompt()
        state = await self.get_state()
        request_text = str(extra_request or "").lower()
        parts = [self.character_type_prompt()]
        if not any(word in request_text for word in _CLOTHING_WORDS):
            parts.append(
                f"Today's stable outfit is: {state.get('outfit', '')}. Keep this same outfit throughout today's ordinary photos."
            )
        else:
            parts.append(
                "The user explicitly requested clothing or cosplay, so follow that request and temporarily ignore today's default outfit."
            )
        if not any(word in request_text for word in _MOOD_WORDS):
            parts.append(f"Today's stable mood is: {state.get('mood', '')}.")
        period_prompt = str(state.get("period_prompt", "") or "").strip()
        if period_prompt:
            parts.append(f"Current time-of-day state: {period_prompt}.")
        return " ".join(part for part in parts if part)

"""Plan persona outfits and expressions without changing the daily profile."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid


def outfit_mode(config: Any) -> str:
    value = str(config.get("persona_outfit_mode", "llm") or "llm").lower()
    return value if value in {"daily", "llm"} else "llm"


def expression_mode(config: Any) -> str:
    value = str(config.get("persona_expression_mode", "context") or "context").lower()
    return value if value in {"fixed", "context", "diverse"} else "context"


def has_outfit_request(text: str) -> bool:
    return bool(re.search(
        r"换衣|换装|穿|衣服|裙|制服|校服|女仆|和服|旗袍|汉服|婚纱|西装|礼服|泳装|同款|这件|那件|这套|那套|"
        r"\b(?:cos(?:play)?|wear(?:ing)?|outfit|dress|shirt|jacket|hoodie|suit|costume|uniform|skirt|clothes)\b",
        str(text or ""), re.I,
    ))


def has_expression_request(text: str) -> bool:
    return bool(re.search(
        r"开心|高兴|难过|伤心|悲伤|忧郁|失落|沮丧|生气|冷淡|害羞|脸红|哭|笑|严肃|慵懒|疲惫|困|兴奋|俏皮|表情|"
        r"眨眼|闭眼|惊讶|吃惊|嘟嘴|疑惑|认真|无表情|面无表情|咬唇|挑眉|吐舌|委屈|傲娇|不满|眉眼|眼神|嘴角|神态|目光|"
        r"\b(?:wink(?:ing)?|smil(?:e|ing)|laugh(?:ing)?|cry(?:ing)?|angry|sad|shy|surprised|pout(?:ing)?|"
        r"expression|neutral|serious|sleepy|confused|eyebrows?|lips|gaze|blush(?:ing)?)\b",
        str(text or ""), re.I,
    ))


# Mild alternatives are a fallback when a chat provider is unavailable. Strong
# emotions always come from the request or the LLM's reading of the dialogue.
_NATURAL_EXPRESSIONS = (
    "放松的嘴角，眼神柔和，轻微微笑",
    "自然舒展的眉毛，平静专注的眼神，嘴唇放松闭合",
    "带一点好奇的眼神，眉梢微微抬起，嘴角自然放松",
    "轻轻抿起嘴角，眼睛带着淡淡笑意，表情含蓄",
    "眉眼舒展，嘴角有轻微不对称的笑意，神态自然",
    "目光短暂移向镜头旁，眉毛放松，安静若有所思",
)
_GAZES = ("自然看向镜头", "视线轻轻偏向镜头一侧", "略微侧脸再看向镜头")


def _clean(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def parse_photo_plan(value: Any, count: int) -> list[dict[str, str]]:
    """Reject incomplete plans rather than silently losing photos or fields."""
    text = str(value or "").strip()
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.I | re.S).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    raw = json.loads(text)
    photos = raw.get("photos") if isinstance(raw, dict) else raw
    if not isinstance(photos, list) or len(photos) != count:
        raise ValueError("Photo plan must contain exactly the requested number of photos")
    result = []
    for photo in photos:
        if not isinstance(photo, dict):
            raise ValueError("Invalid photo plan")
        item = {key: _clean(photo.get(key)) for key in ("outfit", "expression", "gaze", "pose")}
        if not item["outfit"] or not item["expression"]:
            raise ValueError("A photo plan needs an outfit and expression")
        result.append(item)
    return result


def photo_plan_prompt(photo: dict[str, str]) -> str:
    details = " ".join(f"{key}: {value}." for key, value in photo.items() if value)
    return (
        "Photo-specific visual direction: " + details + " "
        "The original user's explicit requirements override this suggested direction. "
        "Preserve facial identity and proportions; eyebrows, gaze, eyelids and mouth shape may change naturally. "
        "Do not copy the reference image's frozen expression or an automatic smile."
    )


class PersonaPhotoPlanner:
    """Bounded successful-plan history, with in-flight reservations per session."""

    def __init__(self, data_dir: Path, config: Any):
        self.path = Path(data_dir) / "persona_photo_history.json"
        self.config = config
        self._history: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        self._pending: dict[str, tuple[str, list[dict[str, str]], set[int]]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    def history_window(self) -> int:
        try:
            return max(0, min(10, int(self.config.get("persona_expression_history", 3))))
        except (TypeError, ValueError):
            return 3

    async def _load_locked(self) -> None:
        if self._loaded:
            return
        if self.path.is_file():
            try:
                raw = json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
                if isinstance(raw, dict):
                    for session, photos in list(raw.items())[-100:]:
                        if isinstance(photos, list):
                            self._history[str(session)[:300]] = [
                                {key: _clean(item.get(key)) for key in ("outfit", "expression", "gaze", "pose")}
                                for item in photos[-10:] if isinstance(item, dict)
                            ]
            except (OSError, ValueError):
                self._history.clear()
        self._loaded = True

    async def _save_locked(self) -> None:
        payload = json.dumps(self._history, ensure_ascii=False, indent=2)

        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)

        await asyncio.to_thread(write)

    def _recent_locked(self, session: str) -> list[dict[str, str]]:
        window = self.history_window()
        if not window:
            return []
        recent = list(self._history.get(session, []))[-window:]
        for pending_session, photos, completed in self._pending.values():
            if pending_session == session:
                recent.extend(photo for index, photo in enumerate(photos) if index not in completed)
        return recent[-max(window, 10):]

    async def recent(self, session: str) -> list[dict[str, str]]:
        async with self._lock:
            await self._load_locked()
            return [dict(photo) for photo in self._recent_locked(str(session or "default")[:300])]

    async def plan(
        self, *, session: str, request: str, count: int, daily_state: dict[str, Any], visual_request: str = "",
        photos: list[dict[str, str]] | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        """Use directions already chosen by the calling LLM; no second LLM request."""
        session = str(session or "default")[:300]
        count = max(1, min(50, count))
        if photos is not None and len(photos) != count:
            raise ValueError("Each photo needs its own direction")
        outfit_policy = outfit_mode(self.config)
        expression_policy = expression_mode(self.config)
        explicit_outfit = has_outfit_request(request) or photos is not None
        explicit_expression = has_expression_request(request) or photos is not None
        if visual_request:
            explicit_outfit = explicit_outfit or outfit_policy == "llm"
            explicit_expression = explicit_expression or expression_policy == "context"
        async with self._lock:
            await self._load_locked()
            recent = self._recent_locked(session)
            used = {_clean(photo.get("expression")) for photo in recent}
            result = []
            for index in range(count):
                photo = dict(photos[index]) if photos is not None else {
                    "outfit": (_clean(visual_request) or "遵循本次请求中的穿搭要求") if explicit_outfit else str(daily_state.get("outfit") or "沿用参考图中适合当前场景的穿搭"),
                    "expression": (_clean(visual_request) or "遵循本次请求中的表情要求") if explicit_expression else "",
                    "gaze": "" if visual_request else _GAZES[index % len(_GAZES)],
                    "pose": "" if visual_request else "服从用户指定动作；未指定时自然放松",
                }
                if not explicit_expression and expression_policy == "context":
                    photo["expression"] = "根据本次场景与心情自然表现，眉眼和嘴角不固定微笑"
                    if recent or index:
                        photo["expression"] += "；保留情绪，仅让眉眼、嘴角和视线有适度变化"
                if outfit_policy == "daily" and not explicit_outfit and daily_state.get("outfit"):
                    photo["outfit"] = str(daily_state["outfit"])
                if expression_policy == "fixed" and not explicit_expression:
                    photo["expression"] = str(daily_state.get("mood") or "保持参考图的自然神态")
                elif not explicit_expression and expression_policy == "diverse" and (not photo["expression"] or ((self.history_window() or count > 1) and photo["expression"] in used)):
                    alternatives = [value for value in _NATURAL_EXPRESSIONS if value not in used]
                    photo["expression"] = (alternatives or list(_NATURAL_EXPRESSIONS))[0]
                used.add(photo["expression"])
                result.append(photo)
            token = uuid.uuid4().hex
            self._pending[token] = (session, result, set())
            return token, result

    async def complete_photo(self, token: str, index: int, success: bool) -> None:
        async with self._lock:
            entry = self._pending.get(token)
            if entry is None:
                return
            session, photos, completed = entry
            if index in completed or not 0 <= index < len(photos):
                return
            completed.add(index)
            if success:
                history = self._history.setdefault(session, [])
                history.append(dict(photos[index]))
                self._history[session] = history[-10:]
                self._history.move_to_end(session)
                while len(self._history) > 100:
                    self._history.popitem(last=False)
                try:
                    await self._save_locked()
                except OSError:
                    pass  # Successful delivery must not fail because a history write failed.
            if len(completed) == len(photos):
                self._pending.pop(token, None)

    async def release(self, token: str) -> None:
        async with self._lock:
            self._pending.pop(token, None)

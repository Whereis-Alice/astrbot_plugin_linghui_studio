import json
import asyncio
import io
import os
import random
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from PIL import Image as PILImage
from PIL import ImageOps
from astrbot import logger
from .utils import norm_id


_GENERATION_PREVIEW_MAX_BYTES = 120 * 1024
_GENERATION_PREVIEW_MAX_EDGE = 440
_GENERATION_KIND_TEXT_TO_IMAGE = "text_to_image"
_GENERATION_KIND_IMAGE_TO_IMAGE = "image_to_image"
_GENERATION_KINDS = {
    _GENERATION_KIND_TEXT_TO_IMAGE,
    _GENERATION_KIND_IMAGE_TO_IMAGE,
}


class DataManager:
    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config

        self.user_counts_file = self.data_dir / "user_counts.json"
        self.group_counts_file = self.data_dir / "group_counts.json"
        self.user_checkin_file = self.data_dir / "user_checkin.json"
        self.daily_stats_file = self.data_dir / "daily_stats.json"
        self.preset_images_file = self.data_dir / "preset_images.json"
        self.user_prompts_file = self.data_dir / "user_prompts.json"
        self.preset_ref_images_file = self.data_dir / "preset_ref_images.json"  # 预设参考图索引
        self.generation_history_file = self.data_dir / "generation_history.json"
        self.identity_labels_file = self.data_dir / "identity_labels.json"
        self.preset_images_dir = self.data_dir / "preset_images"
        self.preset_ref_images_dir = self.data_dir / "preset_ref_images"  # 预设参考图目录
        self.generation_cache_dir = self.data_dir / "generation_cache"
        self.generation_preview_cache_dir = self.data_dir / "generation_preview_cache"
        # Input/reference images are kept separately from the generated
        # output.  This makes it possible to show the actual image-to-image
        # source in Dashboard without exposing an arbitrary local path.
        self.generation_source_cache_dir = self.data_dir / "generation_source_cache"
        self.generation_source_preview_cache_dir = self.data_dir / "generation_source_preview_cache"
        self.fonts_dir = self.data_dir / "fonts"

        # [Fix] 确保数据目录存在
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        if not self.preset_images_dir.exists():
            self.preset_images_dir.mkdir(parents=True, exist_ok=True)
        
        if not self.preset_ref_images_dir.exists():
            self.preset_ref_images_dir.mkdir(parents=True, exist_ok=True)

        if not self.generation_cache_dir.exists():
            self.generation_cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.generation_preview_cache_dir.exists():
            self.generation_preview_cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.generation_source_cache_dir.exists():
            self.generation_source_cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.generation_source_preview_cache_dir.exists():
            self.generation_source_preview_cache_dir.mkdir(parents=True, exist_ok=True)

        if not self.fonts_dir.exists():
            self.fonts_dir.mkdir(parents=True, exist_ok=True)

        self.user_counts: Dict[str, int] = {}
        self.group_counts: Dict[str, int] = {}
        self.user_checkin_data: Dict[str, str] = {}
        self.daily_stats: Dict[str, Any] = {}
        self.preset_images: Dict[str, str] = {}
        self.user_prompts: Dict[str, str] = {}
        self.preset_ref_images: Dict[str, List[str]] = {}  # 预设参考图: {预设名: [图片文件名列表]}
        self.generation_history: List[Dict[str, Any]] = []
        # Canonical user/group IDs remain the keys. Names are display-only
        # labels, refreshed whenever AstrBot provides them on an event.
        self.identity_labels: Dict[str, Dict[str, Dict[str, str]]] = {
            "users": {},
            "groups": {},
        }
        self.prompt_map: Dict[str, str] = {}
        # A single lock protects read-modify-write credit/check-in operations.
        # The upstream version could lose credits when concurrent image tasks
        # finished at nearly the same time.
        self._state_lock = asyncio.Lock()
        self._generation_preview_lock = asyncio.Lock()
        self._generation_history_summary_cache: Optional[Tuple[str, Dict[str, int]]] = None
        self._generation_history_favorites_cache: Optional[List[Dict[str, Any]]] = None

    async def initialize(self):
        await self._load_json(self.user_counts_file, "user_counts")
        await self._load_json(self.group_counts_file, "group_counts")
        await self._load_json(self.user_checkin_file, "user_checkin_data")
        await self._load_json(self.user_prompts_file, "user_prompts")
        await self._load_json(self.preset_ref_images_file, "preset_ref_images")  # 加载预设参考图索引

        if not self.daily_stats_file.exists():
            self.daily_stats = {"date": "", "users": {}, "groups": {}}
        else:
            await self._load_json(self.daily_stats_file, "daily_stats")

        await self._load_json(self.preset_images_file, "preset_images")
        await self._load_json(self.generation_history_file, "generation_history")
        await self._load_json(self.identity_labels_file, "identity_labels")
        self.generation_history = self._normalize_generation_history(self.generation_history)
        self.identity_labels = self._normalize_identity_labels(self.identity_labels)
        self._invalidate_generation_history_summary_locked()
        self.reload_prompts()

    async def _load_json(self, file_path: Path, attr_name: str):
        if not file_path.exists(): return
        try:
            content = await asyncio.to_thread(file_path.read_text, "utf-8")
            setattr(self, attr_name, json.loads(content))
        except Exception as e:
            logger.error(f"Failed to load {file_path}: {e}")

    async def _save_json(self, file_path: Path, data: Any):
        try:
            content = json.dumps(data, indent=4, ensure_ascii=False)
            temp_path = file_path.with_suffix(f"{file_path.suffix}.tmp")

            def _atomic_write():
                temp_path.write_text(content, "utf-8")
                os.replace(temp_path, file_path)

            await asyncio.to_thread(_atomic_write)
        except Exception as e:
            logger.error(f"Failed to save {file_path}: {e}")

    @staticmethod
    def _normalize_display_name(value: Any, limit: int = 160) -> str:
        """Normalize an untrusted adapter-provided display name for storage."""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text[:limit]

    def _normalize_identity_labels(self, raw_labels: Any) -> Dict[str, Dict[str, Dict[str, str]]]:
        """Load display labels without ever turning names into identity keys."""
        normalized: Dict[str, Dict[str, Dict[str, str]]] = {"users": {}, "groups": {}}
        if not isinstance(raw_labels, dict):
            return normalized

        for kind in ("users", "groups"):
            raw_items = raw_labels.get(kind, {})
            if not isinstance(raw_items, dict):
                continue
            for raw_id, raw_entry in raw_items.items():
                identity_id = norm_id(raw_id)
                if not identity_id:
                    continue
                if isinstance(raw_entry, dict):
                    raw_name = raw_entry.get("name", "")
                    updated_at = str(raw_entry.get("updated_at", "") or "")[:80]
                else:
                    # Accept a short-lived development format that used a
                    # plain ID-to-name map, then rewrite it safely on update.
                    raw_name = raw_entry
                    updated_at = ""
                name = self._normalize_display_name(raw_name)
                if name:
                    normalized[kind][identity_id] = {"name": name, "updated_at": updated_at}
        return normalized

    def _identity_display_name(self, kind: str, identity_id: Any) -> str:
        entry = self.identity_labels.get(kind, {}).get(norm_id(identity_id), {})
        return self._normalize_display_name(entry.get("name", "") if isinstance(entry, dict) else entry)

    def get_user_display_name(self, user_id: Any) -> str:
        return self._identity_display_name("users", user_id)

    def get_group_display_name(self, group_id: Any) -> str:
        return self._identity_display_name("groups", group_id)

    def get_identity_label_map(self) -> Dict[str, Dict[str, str]]:
        """Return names for Dashboard presentation while keeping IDs canonical."""
        labels: Dict[str, Dict[str, str]] = {"users": {}, "groups": {}}
        for kind in ("users", "groups"):
            for identity_id, entry in self.identity_labels.get(kind, {}).items():
                name = self._normalize_display_name(
                    entry.get("name", "") if isinstance(entry, dict) else entry
                )
                if name:
                    labels[kind][identity_id] = name
        return labels

    def _update_identity_labels_locked(
            self,
            user_id: Any,
            user_name: Any = "",
            group_id: Any = "",
            group_name: Any = "",
    ) -> bool:
        """Update latest display labels while the shared state lock is held."""
        updates = (
            ("users", norm_id(user_id), self._normalize_display_name(user_name)),
            ("groups", norm_id(group_id), self._normalize_display_name(group_name)),
        )
        changed = False
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        for kind, identity_id, name in updates:
            if not identity_id or not name:
                continue
            bucket = self.identity_labels.setdefault(kind, {})
            previous = bucket.get(identity_id, {})
            previous_name = self._normalize_display_name(
                previous.get("name", "") if isinstance(previous, dict) else previous
            )
            if previous_name == name:
                continue
            bucket[identity_id] = {"name": name, "updated_at": updated_at}
            changed = True
        return changed

    async def update_identity_labels(
            self,
            user_id: Any,
            user_name: Any = "",
            group_id: Any = "",
            group_name: Any = "",
    ) -> bool:
        """Persist latest non-empty names without changing user or group IDs."""
        async with self._state_lock:
            changed = self._update_identity_labels_locked(user_id, user_name, group_id, group_name)
            if changed:
                await self._save_json(self.identity_labels_file, self.identity_labels)
            return changed

    def reload_prompts(self):
        self.prompt_map.clear()
        # 内置预设
        base_cmd_map = {
            "手办化": "figurine_1", "手办化2": "figurine_2", "手办化3": "figurine_3",
            "手办化4": "figurine_4", "手办化5": "figurine_5", "手办化6": "figurine_6",
            "Q版化": "q_version",
            "痛屋化": "pain_room_1", "痛屋化2": "pain_room_2",
            "痛车化": "pain_car",
            "cos化": "cos", "cos自拍": "cos_selfie",
            "孤独的我": "clown",
            "第三视角": "view_3", "鬼图": "ghost", "第一视角": "view_1"
        }
        for k in base_cmd_map.keys(): self.prompt_map[k] = "[内置预设]"

        # 配置中的 prompts (兼容旧版)
        prompts_cfg = self.config.get("prompts", {})
        if isinstance(prompts_cfg, dict):
            for k, v in prompts_cfg.items():
                if isinstance(v, dict) and "default" in v:
                    self.prompt_map[k] = v["default"]
                elif isinstance(v, str):
                    self.prompt_map[k] = v

        # Prompt List (Config)
        prompt_list = self.config.get("prompt_list", [])
        if isinstance(prompt_list, list):
            for item in prompt_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.prompt_map[k.strip()] = v.strip()
        
        # User Prompts (Persistence) - 优先级最高，覆盖前面的
        for k, v in self.user_prompts.items():
            self.prompt_map[k] = v

    def get_prompt(self, key: str) -> Optional[str]:
        return self.prompt_map.get(key)
        
    async def add_user_prompt(self, key: str, prompt: str):
        """添加或更新用户预设，并持久化保存"""
        self.user_prompts[key] = prompt
        await self._save_json(self.user_prompts_file, self.user_prompts)
        self.reload_prompts()

    async def remove_user_prompt(self, key: str) -> bool:
        """Remove a user-defined prompt preset and persist the change."""
        if key not in self.user_prompts:
            return False
        del self.user_prompts[key]
        await self._save_json(self.user_prompts_file, self.user_prompts)
        self.reload_prompts()
        return True

    async def replace_user_prompts(self, prompts: Dict[str, str]) -> None:
        """Replace Dashboard-managed prompt presets atomically.

        Chat commands persist presets in ``user_prompts.json`` while the
        Dashboard keeps the same list in the plugin config. Keeping both
        stores synchronized prevents a stale chat-side value from overriding
        a Dashboard edit after reload.
        """
        normalized = {
            str(name).strip(): str(prompt).strip()
            for name, prompt in prompts.items()
            if str(name).strip() and str(prompt).strip()
        }
        self.user_prompts = normalized
        await self._save_json(self.user_prompts_file, self.user_prompts)
        self.reload_prompts()

    # --- 积分相关 ---
    def get_user_count(self, uid: str) -> int:
        return self.user_counts.get(norm_id(uid), 0)

    async def decrease_user_count(self, uid: str, amount: int = 1):
        uid = norm_id(uid)
        async with self._state_lock:
            count = self.get_user_count(uid)
            if amount <= 0 or count <= 0:
                return
            self.user_counts[uid] = count - min(amount, count)
            await self._save_json(self.user_counts_file, self.user_counts)

    async def add_user_count(self, uid: str, amount: int):
        uid = norm_id(uid)
        async with self._state_lock:
            self.user_counts[uid] = max(0, self.get_user_count(uid) + int(amount))
            await self._save_json(self.user_counts_file, self.user_counts)

    async def set_user_count(self, uid: str, amount: int) -> None:
        uid = norm_id(uid)
        async with self._state_lock:
            self.user_counts[uid] = max(0, int(amount))
            await self._save_json(self.user_counts_file, self.user_counts)

    def get_group_count(self, gid: str) -> int:
        return self.group_counts.get(norm_id(gid), 0)

    async def decrease_group_count(self, gid: str, amount: int = 1):
        gid = norm_id(gid)
        async with self._state_lock:
            count = self.get_group_count(gid)
            if amount <= 0 or count <= 0:
                return
            self.group_counts[gid] = count - min(amount, count)
            await self._save_json(self.group_counts_file, self.group_counts)

    async def add_group_count(self, gid: str, amount: int):
        gid = norm_id(gid)
        async with self._state_lock:
            self.group_counts[gid] = max(0, self.get_group_count(gid) + int(amount))
            await self._save_json(self.group_counts_file, self.group_counts)

    async def set_group_count(self, gid: str, amount: int) -> None:
        gid = norm_id(gid)
        async with self._state_lock:
            self.group_counts[gid] = max(0, int(amount))
            await self._save_json(self.group_counts_file, self.group_counts)

    async def process_checkin(self, uid: str) -> str:
        uid = norm_id(uid)
        async with self._state_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.user_checkin_data.get(uid) == today:
                return f"已签到。剩余: {self.get_user_count(uid)}"

            reward = int(self.config.get("checkin_fixed_reward", 3))
            if self.config.get("enable_random_checkin", False):
                max_r = int(self.config.get("checkin_random_reward_max", 5))
                reward = random.randint(1, max(1, max_r))

            self.user_counts[uid] = self.get_user_count(uid) + reward
            self.user_checkin_data[uid] = today
            await self._save_json(self.user_counts_file, self.user_counts)
            await self._save_json(self.user_checkin_file, self.user_checkin_data)
            return f"🎉 签到成功 +{reward}次。"

    async def clear_checkin(self, uid: str) -> None:
        uid = norm_id(uid)
        async with self._state_lock:
            self.user_checkin_data.pop(uid, None)
            await self._save_json(self.user_checkin_file, self.user_checkin_data)

    async def record_usage(
            self,
            uid: str,
            gid: Optional[str],
            *,
            user_name: str = "",
            group_name: str = "",
    ):
        async with self._state_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.daily_stats.get("date") != today:
                self.daily_stats = {"date": today, "users": {}, "groups": {}}

            uid = norm_id(uid)
            gid = norm_id(gid)
            labels_changed = self._update_identity_labels_locked(uid, user_name, gid, group_name)
            if uid:
                self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1
            if gid:
                self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1
            await self._save_json(self.daily_stats_file, self.daily_stats)
            if labels_changed:
                await self._save_json(self.identity_labels_file, self.identity_labels)

    # --- 成功生成记录与缓存 ---
    @staticmethod
    def _history_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    @staticmethod
    def _history_int(value: Any, minimum: int = 0) -> int:
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError):
            return minimum

    @staticmethod
    def _history_float(value: Any, minimum: float = 0.0) -> float:
        try:
            return max(minimum, float(value))
        except (TypeError, ValueError):
            return minimum

    @staticmethod
    def _inspect_generation_image(image_bytes: bytes) -> Tuple[str, str, int, int]:
        """Validate an output image and derive a stable filename suffix."""
        try:
            with PILImage.open(io.BytesIO(image_bytes)) as image:
                image.load()
                image_format = (image.format or "UNKNOWN").upper()
                width, height = image.size
        except Exception as exc:
            raise ValueError("生成结果不是可识别的图片文件。") from exc

        suffixes = {
            "JPEG": "jpg",
            "PNG": "png",
            "WEBP": "webp",
            "GIF": "gif",
            "BMP": "bmp",
            "TIFF": "tiff",
        }
        return suffixes.get(image_format, "img"), image_format, int(width), int(height)

    @staticmethod
    def _infer_generation_kind(task_type: Any, has_sources: bool = False) -> str:
        """Classify a record without relying on its display-facing task label."""
        if has_sources:
            return _GENERATION_KIND_IMAGE_TO_IMAGE
        label = str(task_type or "").strip().lower()
        # Old histories did not store the request inputs.  These labels are
        # the only safe clue we have for separating them in the new UI.
        image_tokens = ("图生图", "人设", "编辑", "手办", "image-to-image", "image_to_image")
        if any(token in label for token in image_tokens):
            return _GENERATION_KIND_IMAGE_TO_IMAGE
        return _GENERATION_KIND_TEXT_TO_IMAGE

    def _normalize_generation_sources(self, raw_sources: Any) -> List[Dict[str, Any]]:
        """Normalize cached input-image metadata; filenames must stay local."""
        if not isinstance(raw_sources, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for raw_source in raw_sources:
            if not isinstance(raw_source, dict):
                continue
            filename_raw = str(raw_source.get("filename", "") or "").strip()
            filename = Path(filename_raw).name
            if not filename or filename != filename_raw or len(filename) > 180:
                continue
            normalized.append({
                "filename": filename,
                "image_format": str(raw_source.get("image_format", "") or "").strip()[:20],
                "width": self._history_int(raw_source.get("width")),
                "height": self._history_int(raw_source.get("height")),
                "size_bytes": self._history_int(raw_source.get("size_bytes")),
            })
        return normalized

    def _normalize_attempt_chain(self, raw_attempts: Any) -> List[Dict[str, Any]]:
        """Keep bounded, secret-free routing diagnostics in successful records."""
        if not isinstance(raw_attempts, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for raw in raw_attempts[:64]:
            if not isinstance(raw, dict):
                continue
            try:
                duration = max(0.0, float(raw.get("duration", 0.0) or 0.0))
            except (TypeError, ValueError):
                duration = 0.0
            normalized.append({
                "channel_id": self._normalize_display_name(raw.get("channel_id", ""), limit=80),
                "channel_name": self._normalize_display_name(raw.get("channel_name", ""), limit=160),
                "model": str(raw.get("model", "") or "").strip()[:200],
                "duration": round(duration, 4),
                "success": self._history_bool(raw.get("success", False)),
                "error_category": self._normalize_display_name(raw.get("error_category", ""), limit=80),
                "error_label": self._normalize_display_name(raw.get("error_label", ""), limit=120),
                "error": self._normalize_display_name(raw.get("error", ""), limit=240),
                "status_code": self._history_int(raw.get("status_code")),
                "key_attempt": self._history_int(raw.get("key_attempt"), minimum=1),
            })
        return normalized

    def _normalize_generation_history(self, raw_history: Any) -> List[Dict[str, Any]]:
        """Keep only safe, forward-compatible history entries loaded from disk."""
        if not isinstance(raw_history, list):
            return []

        normalized: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in raw_history:
            if not isinstance(raw, dict):
                continue
            record_id = str(raw.get("id", "") or "").strip()
            filename_raw = str(raw.get("filename", "") or "").strip()
            filename = Path(filename_raw).name
            if (
                not record_id
                or len(record_id) > 80
                or record_id in seen_ids
                or not filename
                or filename != filename_raw
            ):
                continue
            task_type = str(raw.get("task_type", "") or "").strip()[:80]
            source_images = self._normalize_generation_sources(raw.get("source_images", []))
            raw_kind = str(raw.get("generation_kind", "") or "").strip().lower()
            generation_kind = (
                raw_kind if raw_kind in _GENERATION_KINDS
                else self._infer_generation_kind(task_type, bool(source_images))
            )
            source_status = str(raw.get("source_status", "") or "").strip().lower()
            if source_images:
                source_status = "cached"
            elif source_status not in {"not_applicable", "legacy_unavailable", "missing"}:
                source_status = (
                    "legacy_unavailable"
                    if generation_kind == _GENERATION_KIND_IMAGE_TO_IMAGE
                    else "not_applicable"
                )
            seen_ids.add(record_id)
            normalized.append({
                "id": record_id,
                "filename": filename,
                "created_at": str(raw.get("created_at", "") or ""),
                "user_id": norm_id(raw.get("user_id")),
                "group_id": norm_id(raw.get("group_id")),
                "user_name": self._normalize_display_name(raw.get("user_name", "")),
                "group_name": self._normalize_display_name(raw.get("group_name", "")),
                "prompt": str(raw.get("prompt", "") or "").strip()[:12_000],
                "model": str(raw.get("model", "") or "").strip()[:200],
                "channel_id": self._normalize_display_name(raw.get("channel_id", ""), limit=80),
                "channel_name": self._normalize_display_name(raw.get("channel_name", ""), limit=160),
                "fallback_count": self._history_int(raw.get("fallback_count")),
                "route_duration": self._history_float(raw.get("route_duration")),
                "attempt_chain": self._normalize_attempt_chain(raw.get("attempt_chain", [])),
                "task_id": self._normalize_display_name(raw.get("task_id", ""), limit=80),
                "delivery_status": self._normalize_display_name(raw.get("delivery_status", "sent"), limit=40) or "sent",
                "preset": str(raw.get("preset", "") or "").strip()[:160],
                "task_type": task_type,
                "image_format": str(raw.get("image_format", "") or "").strip()[:20],
                "width": self._history_int(raw.get("width")),
                "height": self._history_int(raw.get("height")),
                "size_bytes": self._history_int(raw.get("size_bytes")),
                "generation_kind": generation_kind,
                "source_images": source_images,
                "source_count": len(source_images),
                "source_size_bytes": sum(self._history_int(item.get("size_bytes")) for item in source_images),
                "source_status": source_status,
                "favorite": self._history_bool(raw.get("favorite", False)),
                "locked": self._history_bool(raw.get("locked", False)),
            })

        return sorted(normalized, key=lambda item: item["created_at"], reverse=True)

    def _generation_cache_path(self, filename: Any) -> Optional[Path]:
        """Resolve a cache file only when it stays immediately under its root."""
        try:
            root = self.generation_cache_dir.resolve()
            raw_name = str(filename or "")
            if not raw_name or Path(raw_name).name != raw_name:
                return None
            candidate = (root / raw_name).resolve()
        except (OSError, TypeError, ValueError):
            return None
        return candidate if candidate.parent == root else None

    def _generation_preview_cache_path(self, record_id: Any) -> Optional[Path]:
        """Resolve an opaque record ID to its dedicated JPEG thumbnail path."""
        normalized_id = str(record_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", normalized_id):
            return None
        try:
            root = self.generation_preview_cache_dir.resolve()
            candidate = (root / f"{normalized_id}.jpg").resolve()
        except (OSError, TypeError, ValueError):
            return None
        return candidate if candidate.parent == root else None

    def _generation_source_cache_path(self, filename: Any) -> Optional[Path]:
        """Resolve a cached input image only when it stays in its own root."""
        try:
            root = self.generation_source_cache_dir.resolve()
            raw_name = str(filename or "")
            if not raw_name or Path(raw_name).name != raw_name:
                return None
            candidate = (root / raw_name).resolve()
        except (OSError, TypeError, ValueError):
            return None
        return candidate if candidate.parent == root else None

    def _generation_source_preview_cache_path(self, record_id: Any, source_index: Any) -> Optional[Path]:
        """Resolve one input image's dedicated bounded thumbnail path."""
        normalized_id = str(record_id or "").strip()
        try:
            index = int(source_index)
        except (TypeError, ValueError):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", normalized_id) or index < 1 or index > 999:
            return None
        try:
            root = self.generation_source_preview_cache_dir.resolve()
            candidate = (root / f"{normalized_id}_{index:03d}.jpg").resolve()
        except (OSError, TypeError, ValueError):
            return None
        return candidate if candidate.parent == root else None

    @staticmethod
    def _generation_source_entries(record: Any) -> List[Dict[str, Any]]:
        if not isinstance(record, dict):
            return []
        sources = record.get("source_images", [])
        return [item for item in sources if isinstance(item, dict)] if isinstance(sources, list) else []

    def get_generation_image_path(self, record: Dict[str, Any]) -> Optional[Path]:
        if not isinstance(record, dict):
            return None
        path = self._generation_cache_path(record.get("filename"))
        return path if path is not None and path.is_file() else None

    def get_generation_preview_path(self, record: Dict[str, Any]) -> Optional[Path]:
        if not isinstance(record, dict):
            return None
        path = self._generation_preview_cache_path(record.get("id"))
        return path if path is not None and path.is_file() else None

    def get_generation_source_path(self, record: Dict[str, Any], source_index: int) -> Optional[Path]:
        """Return one cached request input by its 1-based Dashboard position."""
        try:
            source_index = int(source_index)
        except (TypeError, ValueError):
            return None
        sources = self._generation_source_entries(record)
        if source_index < 1 or source_index > len(sources):
            return None
        record_id = str(record.get("id", "") or "").strip()
        filename = str(sources[source_index - 1].get("filename", "") or "")
        # Input cache filenames are record-scoped.  Do not let a damaged JSON
        # history entry expose an input file owned by a different record.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record_id) or not filename.startswith(
                f"{record_id}_source_{source_index:03d}."):
            return None
        path = self._generation_source_cache_path(filename)
        return path if path is not None and path.is_file() else None

    def get_generation_source_preview_path(self, record: Dict[str, Any], source_index: int) -> Optional[Path]:
        if not isinstance(record, dict):
            return None
        path = self._generation_source_preview_cache_path(record.get("id"), source_index)
        return path if path is not None and path.is_file() else None

    @staticmethod
    def _write_generation_preview(source_path: Path, target_path: Path) -> None:
        """Create a bounded JPEG thumbnail without modifying the original cache file."""
        try:
            with PILImage.open(source_path) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode != "RGB":
                    if "A" in image.getbands():
                        background = PILImage.new("RGB", image.size, "#111827")
                        background.paste(image, mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")

                encoded = b""
                for edge, quality in (
                    (_GENERATION_PREVIEW_MAX_EDGE, 82),
                    (352, 76),
                    (264, 68),
                ):
                    preview = image.copy()
                    preview.thumbnail((edge, edge), PILImage.Resampling.LANCZOS)
                    buffer = io.BytesIO()
                    preview.save(
                        buffer,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=True,
                    )
                    encoded = buffer.getvalue()
                    if len(encoded) <= _GENERATION_PREVIEW_MAX_BYTES:
                        break
        except Exception as exc:
            raise ValueError(f"无法生成成功记录缩略图: {exc}") from exc

        if not encoded:
            raise ValueError("成功记录缩略图为空")

        temp_path = target_path.with_suffix(".jpg.tmp")
        try:
            temp_path.write_bytes(encoded)
            os.replace(temp_path, target_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def get_or_create_generation_preview(self, record: Dict[str, Any]) -> Optional[Path]:
        """Return a cached thumbnail, creating it only when an administrator needs it."""
        source_path = self.get_generation_image_path(record)
        target_path = self._generation_preview_cache_path(record.get("id") if isinstance(record, dict) else "")
        if source_path is None or target_path is None:
            return None
        if target_path.is_file():
            return target_path

        # Serialising thumbnail writes avoids duplicate decoding when multiple
        # browser tiles enter the viewport at nearly the same time.
        async with self._generation_preview_lock:
            if target_path.is_file():
                return target_path
            try:
                await asyncio.to_thread(self._write_generation_preview, source_path, target_path)
            except Exception as exc:
                logger.warning("Linghui could not create cached generation preview: %s", exc)
                return None
        return target_path if target_path.is_file() else None

    async def get_or_create_generation_source_preview(
            self, record: Dict[str, Any], source_index: int
    ) -> Optional[Path]:
        """Return a small source-image preview only when the UI asks for it."""
        source_path = self.get_generation_source_path(record, source_index)
        target_path = self._generation_source_preview_cache_path(
            record.get("id") if isinstance(record, dict) else "", source_index
        )
        if source_path is None or target_path is None:
            return None
        if target_path.is_file():
            return target_path

        async with self._generation_preview_lock:
            if target_path.is_file():
                return target_path
            try:
                await asyncio.to_thread(self._write_generation_preview, source_path, target_path)
            except Exception as exc:
                logger.warning("Linghui could not create cached input-image preview: %s", exc)
                return None
        return target_path if target_path.is_file() else None

    async def _remove_generation_source_artifacts(self, record: Dict[str, Any]) -> Tuple[int, int]:
        """Delete cached request inputs and their previews for one record."""
        removed_sources = 0
        removed_previews = 0
        for source_index in range(1, len(self._generation_source_entries(record)) + 1):
            source_path = self.get_generation_source_path(record, source_index)
            if source_path is not None:
                try:
                    await asyncio.to_thread(source_path.unlink)
                    removed_sources += 1
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.warning("Linghui could not delete cached input image %s: %s", source_path.name, exc)
            preview_path = self.get_generation_source_preview_path(record, source_index)
            if preview_path is not None:
                try:
                    await asyncio.to_thread(preview_path.unlink)
                    removed_previews += 1
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    logger.warning("Linghui could not delete cached input preview %s: %s", preview_path.name, exc)
        return removed_sources, removed_previews

    def _invalidate_generation_history_summary_locked(self) -> None:
        self._generation_history_summary_cache = None
        self._generation_history_favorites_cache = None

    def _favorite_generation_history_locked(self) -> List[Dict[str, Any]]:
        """Reuse the favorite-only view until the history changes."""
        cached = self._generation_history_favorites_cache
        if cached is None:
            cached = [item for item in self.generation_history if self._history_bool(item.get("favorite", False))]
            self._generation_history_favorites_cache = cached
        return cached

    def _generation_history_summary_locked(self) -> Dict[str, int]:
        """Compute history statistics once per mutation instead of once per page request."""
        today_key = datetime.now().date().isoformat()
        cached = self._generation_history_summary_cache
        if cached is not None and cached[0] == today_key:
            return dict(cached[1])

        unique_users = set()
        unique_groups = set()
        summary = {
            "total": 0,
            "favorite": 0,
            "locked": 0,
            "protected": 0,
            "size_bytes": 0,
            "output_size_bytes": 0,
            "source_size_bytes": 0,
            "source_images": 0,
            "text_to_image": 0,
            "image_to_image": 0,
            "legacy_source_unavailable": 0,
            "today": 0,
            "users": 0,
            "groups": 0,
            "private": 0,
            "fallback_successes": 0,
            "route_duration_total": 0.0,
            "route_p50": 0.0,
            "route_p95": 0.0,
        }
        route_durations: List[float] = []
        for item in self.generation_history:
            summary["total"] += 1
            user_id = norm_id(item.get("user_id"))
            group_id = norm_id(item.get("group_id"))
            if user_id:
                unique_users.add(user_id)
            if group_id:
                unique_groups.add(group_id)
            else:
                summary["private"] += 1
            if self._history_bool(item.get("favorite", False)):
                summary["favorite"] += 1
            if self._history_bool(item.get("locked", False)):
                summary["locked"] += 1
            if self._history_bool(item.get("favorite", False)) or self._history_bool(item.get("locked", False)):
                summary["protected"] += 1
            if self._history_int(item.get("fallback_count")) > 0:
                summary["fallback_successes"] += 1
            route_duration = self._history_float(item.get("route_duration"))
            if route_duration > 0:
                route_durations.append(route_duration)
                summary["route_duration_total"] += route_duration
            output_size = self._history_int(item.get("size_bytes"))
            source_size = self._history_int(item.get("source_size_bytes"))
            summary["output_size_bytes"] += output_size
            summary["source_size_bytes"] += source_size
            summary["size_bytes"] += output_size + source_size
            summary["source_images"] += self._history_int(item.get("source_count"))
            kind = str(item.get("generation_kind", "") or "")
            if kind == _GENERATION_KIND_IMAGE_TO_IMAGE:
                summary["image_to_image"] += 1
                if str(item.get("source_status", "") or "") == "legacy_unavailable":
                    summary["legacy_source_unavailable"] += 1
            else:
                summary["text_to_image"] += 1
            created_at = self._history_created_at(item, None)
            if created_at is not None and created_at.date().isoformat() == today_key:
                summary["today"] += 1

        summary["users"] = len(unique_users)
        summary["groups"] = len(unique_groups)
        if route_durations:
            ordered = sorted(route_durations)
            summary["route_p50"] = ordered[round((len(ordered) - 1) * 0.50)]
            summary["route_p95"] = ordered[round((len(ordered) - 1) * 0.95)]
        self._generation_history_summary_cache = (today_key, dict(summary))
        return summary

    async def save_generation_record(
            self,
            image_bytes: bytes,
            *,
            prompt: str,
            user_id: str,
            group_id: str = "",
            user_name: str = "",
            group_name: str = "",
            model: str = "",
            channel_id: str = "",
            channel_name: str = "",
            preset: str = "",
            task_type: str = "",
            reference_images: Optional[List[bytes]] = None,
            generation_kind: str = "",
            fallback_count: int = 0,
            route_duration: float = 0.0,
            attempt_chain: Optional[List[Dict[str, Any]]] = None,
            task_id: str = "",
            delivery_status: str = "sent",
    ) -> Optional[Dict[str, Any]]:
        """Persist a successful output and the exact image inputs used for it."""
        if not isinstance(image_bytes, bytes) or not image_bytes:
            return None
        try:
            suffix, image_format, width, height = await asyncio.to_thread(
                self._inspect_generation_image, image_bytes
            )
        except ValueError as exc:
            logger.warning("Linghui skipped an unrecognizable generation result cache: %s", exc)
            return None

        record_id = uuid.uuid4().hex
        timestamp = datetime.now()
        filename = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{record_id}.{suffix}"
        source_payloads: List[Tuple[Dict[str, Any], bytes]] = []
        for source_image in reference_images or []:
            if not isinstance(source_image, bytes) or not source_image:
                continue
            try:
                source_suffix, source_format, source_width, source_height = await asyncio.to_thread(
                    self._inspect_generation_image, source_image
                )
            except ValueError:
                # A request can technically contain a malformed image accepted
                # by an upstream provider.  Do not make output history fail for
                # that case, but never write unvalidated data to the cache.
                logger.warning("Linghui skipped an unrecognizable input image in generation history")
                continue
            source_index = len(source_payloads) + 1
            source_filename = f"{record_id}_source_{source_index:03d}.{source_suffix}"
            if self._generation_source_cache_path(source_filename) is None:
                continue
            source_payloads.append(({
                "filename": source_filename,
                "image_format": source_format,
                "width": source_width,
                "height": source_height,
                "size_bytes": len(source_image),
            }, source_image))

        requested_kind = str(generation_kind or "").strip().lower()
        record_kind = (
            requested_kind if requested_kind in _GENERATION_KINDS
            else self._infer_generation_kind(task_type, bool(source_payloads))
        )
        source_images = [item[0] for item in source_payloads]
        record = {
            "id": record_id,
            "filename": filename,
            "created_at": timestamp.astimezone().isoformat(timespec="seconds"),
            "user_id": norm_id(user_id),
            "group_id": norm_id(group_id),
            "user_name": "",
            "group_name": "",
            "prompt": str(prompt or "").strip()[:12_000],
            "model": str(model or "").strip()[:200],
            "channel_id": self._normalize_display_name(channel_id, limit=80),
            "channel_name": self._normalize_display_name(channel_name, limit=160),
            "fallback_count": self._history_int(fallback_count),
            "route_duration": self._history_float(route_duration),
            "attempt_chain": self._normalize_attempt_chain(attempt_chain or []),
            "task_id": self._normalize_display_name(task_id, limit=80),
            "delivery_status": self._normalize_display_name(delivery_status, limit=40) or "sent",
            "preset": str(preset or "").strip()[:160],
            "task_type": str(task_type or "").strip()[:80],
            "image_format": image_format,
            "width": width,
            "height": height,
            "size_bytes": len(image_bytes),
            "generation_kind": record_kind,
            "source_images": source_images,
            "source_count": len(source_images),
            "source_size_bytes": sum(item["size_bytes"] for item in source_images),
            "source_status": "cached" if source_images else (
                "missing" if record_kind == _GENERATION_KIND_IMAGE_TO_IMAGE else "not_applicable"
            ),
            "favorite": False,
            "locked": False,
        }
        path = self._generation_cache_path(filename)
        if path is None:
            return None

        async with self._state_lock:
            labels_changed = self._update_identity_labels_locked(
                record["user_id"], user_name, record["group_id"], group_name
            )
            # A history record keeps the name seen at success time. Older
            # entries without a snapshot can still use the latest label in
            # the Dashboard, but new entries remain understandable after a
            # later nickname change.
            record["user_name"] = (
                self._normalize_display_name(user_name)
                or self.get_user_display_name(record["user_id"])
            )
            record["group_name"] = (
                self._normalize_display_name(group_name)
                or self.get_group_display_name(record["group_id"])
            )
            written_paths: List[Path] = []
            try:
                await asyncio.to_thread(path.write_bytes, image_bytes)
                written_paths.append(path)
                for source_info, source_image in source_payloads:
                    source_path = self._generation_source_cache_path(source_info["filename"])
                    if source_path is None:
                        raise OSError("Invalid generation input cache path")
                    await asyncio.to_thread(source_path.write_bytes, source_image)
                    written_paths.append(source_path)
            except Exception as exc:
                for written_path in written_paths:
                    try:
                        await asyncio.to_thread(written_path.unlink)
                    except OSError:
                        pass
                logger.error("Linghui could not cache a generated image or its input: %s", exc)
                return None
            self.generation_history.insert(0, record)
            self._invalidate_generation_history_summary_locked()
            await self._save_json(self.generation_history_file, self.generation_history)
            if labels_changed:
                await self._save_json(self.identity_labels_file, self.identity_labels)
        # Prewarm the result thumbnail and first input thumbnail in the
        # background. Dashboard still loads them lazily, but the expensive
        # decode usually finishes before an administrator opens the page.
        try:
            asyncio.create_task(self.get_or_create_generation_preview(record))
            if source_images:
                asyncio.create_task(self.get_or_create_generation_source_preview(record, 1))
        except RuntimeError:
            pass
        return dict(record)

    async def get_generation_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Look up one cache record by its opaque Dashboard record ID."""
        record_id = str(record_id or "").strip()
        if not record_id:
            return None
        async with self._state_lock:
            for record in self.generation_history:
                if record.get("id") == record_id:
                    return dict(record)
        return None

    async def get_generation_history_page(
            self,
            limit: int = 24,
            offset: int = 0,
            *,
            favorite_only: bool = False,
            generation_kind: str = "all",
            group_filter: str = "",
            user_filter: str = "",
    ) -> Tuple[List[Dict[str, Any]], int, Dict[str, int], Dict[str, Any]]:
        """Return one metadata-only history page plus filter facets and statistics."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 24
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 0
        limit = min(max(1, limit), 100)
        offset = max(0, offset)
        generation_kind = str(generation_kind or "all").strip().lower()
        if generation_kind not in {"all", *_GENERATION_KINDS}:
            generation_kind = "all"
        raw_group_filter = str(group_filter or "").strip()
        private_only = raw_group_filter == "__private__"
        normalized_group_filter = "" if private_only else norm_id(raw_group_filter)
        normalized_user_filter = norm_id(user_filter)

        async with self._state_lock:
            source_records = self._favorite_generation_history_locked() if favorite_only else self.generation_history
            if generation_kind != "all":
                source_records = [
                    item for item in source_records
                    if item.get("generation_kind") == generation_kind
                ]

            def matches_group(item: Dict[str, Any]) -> bool:
                record_group_id = norm_id(item.get("group_id"))
                if private_only:
                    return not record_group_id
                if normalized_group_filter:
                    return record_group_id == normalized_group_filter
                return True

            def matches_user(item: Dict[str, Any]) -> bool:
                return not normalized_user_filter or norm_id(item.get("user_id")) == normalized_user_filter

            user_counts: Dict[str, int] = {}
            for item in source_records:
                if not matches_group(item):
                    continue
                user_id = norm_id(item.get("user_id"))
                if user_id:
                    user_counts[user_id] = user_counts.get(user_id, 0) + 1

            group_counts: Dict[str, int] = {}
            private_count = 0
            for item in source_records:
                if not matches_user(item):
                    continue
                group_id = norm_id(item.get("group_id"))
                if group_id:
                    group_counts[group_id] = group_counts.get(group_id, 0) + 1
                else:
                    private_count += 1

            filtered_records = [
                item for item in source_records
                if matches_group(item) and matches_user(item)
            ]
            total = len(filtered_records)
            page = [dict(item) for item in filtered_records[offset:offset + limit]]
            summary = self._generation_history_summary_locked()
            today_key = datetime.now().date().isoformat()
            view_summary = {
                "total": total,
                "today": 0,
                "protected": 0,
                "size_bytes": 0,
            }
            for item in filtered_records:
                if self._history_bool(item.get("favorite", False)) or self._history_bool(item.get("locked", False)):
                    view_summary["protected"] += 1
                view_summary["size_bytes"] += (
                    self._history_int(item.get("size_bytes"))
                    + self._history_int(item.get("source_size_bytes"))
                )
                created_at = self._history_created_at(item, None)
                if created_at is not None and created_at.date().isoformat() == today_key:
                    view_summary["today"] += 1

            view = {
                "summary": view_summary,
                "filter_options": {
                    "users": user_counts,
                    "groups": group_counts,
                    "private_count": private_count,
                },
            }
        return page, total, summary, view

    async def update_generation_record_flags(
            self,
            record_id: str,
            *,
            favorite: Optional[bool] = None,
            locked: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        record_id = str(record_id or "").strip()
        if not record_id:
            return None
        async with self._state_lock:
            for record in self.generation_history:
                if record.get("id") != record_id:
                    continue
                if favorite is not None:
                    record["favorite"] = bool(favorite)
                if locked is not None:
                    record["locked"] = bool(locked)
                self._invalidate_generation_history_summary_locked()
                await self._save_json(self.generation_history_file, self.generation_history)
                return dict(record)
        return None

    async def update_generation_record_delivery(self, record_id: str, delivery_status: str) -> Optional[Dict[str, Any]]:
        """Persist final platform delivery state after the image is already cached."""
        record_id = str(record_id or "").strip()
        delivery_status = self._normalize_display_name(delivery_status, limit=40)
        if not record_id or not delivery_status:
            return None
        async with self._state_lock:
            for record in self.generation_history:
                if record.get("id") != record_id:
                    continue
                record["delivery_status"] = delivery_status
                await self._save_json(self.generation_history_file, self.generation_history)
                return dict(record)
        return None

    async def delete_generation_record(self, record_id: str) -> bool:
        """Explicit Dashboard deletion may remove a protected record as well."""
        record_id = str(record_id or "").strip()
        if not record_id:
            return False
        async with self._state_lock:
            for index, record in enumerate(self.generation_history):
                if record.get("id") != record_id:
                    continue
                path = self.get_generation_image_path(record)
                if path is not None:
                    try:
                        await asyncio.to_thread(path.unlink)
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        logger.warning("Linghui could not delete cached image %s: %s", path.name, exc)
                        return False
                preview_path = self.get_generation_preview_path(record)
                if preview_path is not None:
                    try:
                        await asyncio.to_thread(preview_path.unlink)
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        logger.warning("Linghui could not delete cached generation preview %s: %s", preview_path.name, exc)
                await self._remove_generation_source_artifacts(record)
                self.generation_history.pop(index)
                self._invalidate_generation_history_summary_locked()
                await self._save_json(self.generation_history_file, self.generation_history)
                return True
        return False

    @staticmethod
    def _history_created_at(record: Dict[str, Any], path: Optional[Path]) -> Optional[datetime]:
        raw = str(record.get("created_at", "") or "").strip()
        if raw:
            try:
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if created.tzinfo is not None:
                    created = created.astimezone().replace(tzinfo=None)
                return created
            except ValueError:
                pass
        if path is not None:
            try:
                return datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                pass
        return None

    def _generation_record_artifact_size(self, record: Dict[str, Any]) -> int:
        """Return the actual on-disk footprint of one output and its inputs."""
        total = 0
        paths: List[Optional[Path]] = [
            self.get_generation_image_path(record),
            self.get_generation_preview_path(record),
        ]
        for source_index in range(1, len(self._generation_source_entries(record)) + 1):
            paths.append(self.get_generation_source_path(record, source_index))
            paths.append(self.get_generation_source_preview_path(record, source_index))
        for path in paths:
            if path is None:
                continue
            try:
                total += max(0, int(path.stat().st_size))
            except OSError:
                continue
        return total

    async def cleanup_generation_cache(
            self,
            retention_days: int = 7,
            max_bytes: int = 0,
            trim_ratio: float = 0.15,
    ) -> Dict[str, int]:
        """Remove expired and over-capacity cache entries while protecting favorites/locks."""
        try:
            retention_days = min(max(1, int(retention_days)), 365)
        except (TypeError, ValueError):
            retention_days = 7
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed_records = 0
        removed_images = 0
        removed_previews = 0
        removed_source_images = 0
        removed_source_previews = 0
        removed_orphans = 0
        capacity_removed_records = 0
        capacity_before_bytes = 0
        capacity_after_bytes = 0
        try:
            max_bytes = max(0, int(max_bytes or 0))
        except (TypeError, ValueError):
            max_bytes = 0
        try:
            trim_ratio = min(max(float(trim_ratio), 0.01), 0.90)
        except (TypeError, ValueError):
            trim_ratio = 0.15

        async with self._state_lock:
            retained: List[Dict[str, Any]] = []
            for record in self.generation_history:
                path = self.get_generation_image_path(record)
                protected = bool(record.get("favorite")) or bool(record.get("locked"))
                created_at = self._history_created_at(record, path)
                expired = created_at is None or created_at < cutoff

                if protected:
                    retained.append(record)
                    continue
                if not expired and path is not None:
                    retained.append(record)
                    continue

                if path is not None:
                    try:
                        await asyncio.to_thread(path.unlink)
                        removed_images += 1
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        logger.warning("Linghui could not clean cached image %s: %s", path.name, exc)
                        retained.append(record)
                        continue
                preview_path = self.get_generation_preview_path(record)
                if preview_path is not None:
                    try:
                        await asyncio.to_thread(preview_path.unlink)
                        removed_previews += 1
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        logger.warning("Linghui could not clean cached generation preview %s: %s", preview_path.name, exc)
                source_removed, source_preview_removed = await self._remove_generation_source_artifacts(record)
                removed_source_images += source_removed
                removed_source_previews += source_preview_removed
                removed_records += 1

            capacity_before_bytes = sum(self._generation_record_artifact_size(record) for record in retained)
            capacity_after_bytes = capacity_before_bytes
            if max_bytes > 0 and capacity_before_bytes > max_bytes:
                target_bytes = max(0, int(max_bytes * (1.0 - trim_ratio)))
                removable = sorted(
                    (
                        record for record in retained
                        if not self._history_bool(record.get("favorite", False))
                        and not self._history_bool(record.get("locked", False))
                    ),
                    key=lambda record: self._history_created_at(
                        record, self.get_generation_image_path(record)
                    ) or datetime.min,
                )
                removed_ids: set[str] = set()
                for record in removable:
                    if capacity_after_bytes <= target_bytes:
                        break
                    artifact_size = self._generation_record_artifact_size(record)
                    path = self.get_generation_image_path(record)
                    if path is not None:
                        try:
                            await asyncio.to_thread(path.unlink)
                            removed_images += 1
                        except FileNotFoundError:
                            pass
                        except Exception as exc:
                            logger.warning("Linghui could not trim cached image %s: %s", path.name, exc)
                            continue
                    preview_path = self.get_generation_preview_path(record)
                    if preview_path is not None:
                        try:
                            await asyncio.to_thread(preview_path.unlink)
                            removed_previews += 1
                        except FileNotFoundError:
                            pass
                        except Exception as exc:
                            logger.warning("Linghui could not trim generation preview %s: %s", preview_path.name, exc)
                    source_removed, source_preview_removed = await self._remove_generation_source_artifacts(record)
                    removed_source_images += source_removed
                    removed_source_previews += source_preview_removed
                    removed_ids.add(str(record.get("id", "")))
                    capacity_after_bytes = max(0, capacity_after_bytes - artifact_size)
                    capacity_removed_records += 1
                    removed_records += 1
                if removed_ids:
                    retained = [record for record in retained if str(record.get("id", "")) not in removed_ids]

            history_changed = len(retained) != len(self.generation_history)
            self.generation_history = retained
            known_files = {str(record.get("filename", "")) for record in retained}
            known_preview_files = {
                f"{record_id}.jpg"
                for record in retained
                if (record_id := str(record.get("id", "") or "").strip())
                and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record_id)
            }
            known_source_files = {
                str(source.get("filename", ""))
                for record in retained
                for source in self._generation_source_entries(record)
                if str(source.get("filename", ""))
            }
            known_source_preview_files = {
                f"{record_id}_{source_index:03d}.jpg"
                for record in retained
                if (record_id := str(record.get("id", "") or "").strip())
                and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", record_id)
                for source_index in range(1, len(self._generation_source_entries(record)) + 1)
            }

            try:
                cache_files = list(self.generation_cache_dir.iterdir())
            except OSError:
                cache_files = []
            for path in cache_files:
                if not path.is_file() or path.name in known_files:
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                        await asyncio.to_thread(path.unlink)
                        removed_orphans += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning("Linghui could not clean orphaned cache file %s: %s", path.name, exc)

            try:
                preview_files = list(self.generation_preview_cache_dir.iterdir())
            except OSError:
                preview_files = []
            for path in preview_files:
                if not path.is_file() or path.name in known_preview_files:
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                        await asyncio.to_thread(path.unlink)
                        removed_previews += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning("Linghui could not clean orphaned generation preview %s: %s", path.name, exc)

            try:
                source_files = list(self.generation_source_cache_dir.iterdir())
            except OSError:
                source_files = []
            for path in source_files:
                if not path.is_file() or path.name in known_source_files:
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                        await asyncio.to_thread(path.unlink)
                        removed_orphans += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning("Linghui could not clean orphaned input cache %s: %s", path.name, exc)

            try:
                source_preview_files = list(self.generation_source_preview_cache_dir.iterdir())
            except OSError:
                source_preview_files = []
            for path in source_preview_files:
                if not path.is_file() or path.name in known_source_preview_files:
                    continue
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                        await asyncio.to_thread(path.unlink)
                        removed_source_previews += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning("Linghui could not clean orphaned input preview %s: %s", path.name, exc)

            if history_changed:
                self._invalidate_generation_history_summary_locked()
                await self._save_json(self.generation_history_file, self.generation_history)

        return {
            "removed_records": removed_records,
            "removed_images": removed_images,
            "removed_previews": removed_previews,
            "removed_source_images": removed_source_images,
            "removed_source_previews": removed_source_previews,
            "removed_orphans": removed_orphans,
            "capacity_removed_records": capacity_removed_records,
            "capacity_before_bytes": capacity_before_bytes,
            "capacity_after_bytes": capacity_after_bytes,
            "capacity_limit_bytes": max_bytes,
        }

    # --- 预设图片管理 ---
    async def save_preset_image(self, preset_key: str, image_bytes: bytes):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{preset_key}_{timestamp}.png"
            filepath = self.preset_images_dir / filename
            await asyncio.to_thread(filepath.write_bytes, image_bytes)

            if preset_key in self.preset_images:
                old_f = self.preset_images_dir / self.preset_images[preset_key]
                if old_f.exists(): await asyncio.to_thread(old_f.unlink)

            self.preset_images[preset_key] = filename
            await self._save_json(self.preset_images_file, self.preset_images)
        except Exception as e:
            logger.error(f"Save preset img error: {e}")

    def get_preset_image_path(self, preset_key: str) -> Optional[str]:
        if preset_key not in self.preset_images: return None
        f_path = self.preset_images_dir / self.preset_images[preset_key]
        return str(f_path) if f_path.exists() else None

    # [新增] 统计与清理功能
    async def cleanup_old_presets(self, days: int) -> int:
        count = 0
        now = datetime.now()
        for k, v in list(self.preset_images.items()):
            p = self.preset_images_dir / v
            if p.exists():
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                if (now - mtime).days > days:
                    await asyncio.to_thread(p.unlink)
                    del self.preset_images[k]
                    count += 1
            else:
                del self.preset_images[k]  # Clean broken link
        if count > 0:
            await self._save_json(self.preset_images_file, self.preset_images)
        return count

    def get_preset_stats(self) -> Tuple[int, float]:
        """返回 (数量, MB大小)"""
        total_size = 0
        count = 0
        for v in self.preset_images.values():
            p = self.preset_images_dir / v
            if p.exists():
                total_size += p.stat().st_size
                count += 1
        return count, total_size / (1024 * 1024)

    # ================= 预设参考图管理 =================

    @staticmethod
    def _reference_image_suffix(image_bytes: bytes) -> str:
        """Verify a reference image and return an extension matching its bytes."""
        try:
            with PILImage.open(io.BytesIO(image_bytes)) as image:
                image.verify()
                image_format = (image.format or "").upper()
        except Exception as exc:
            raise ValueError("参考图不是可识别的图片文件。") from exc

        suffixes = {
            "JPEG": "jpg",
            "PNG": "png",
            "WEBP": "webp",
            "GIF": "gif",
            "BMP": "bmp",
            "TIFF": "tiff",
        }
        if image_format not in suffixes:
            raise ValueError(f"不支持的参考图格式：{image_format or 'unknown'}")
        return suffixes[image_format]

    async def save_preset_ref_image(self, preset_key: str, image_bytes: bytes) -> str:
        """
        保存预设参考图
        
        Args:
            preset_key: 预设名称
            image_bytes: 图片二进制数据
            
        Returns:
            保存的文件名
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            suffix = await asyncio.to_thread(self._reference_image_suffix, image_bytes)
            # 清理预设名中的特殊字符
            safe_key = "".join(c if c.isalnum() or c in "_-" else "_" for c in preset_key)
            filename = f"{safe_key}_{timestamp}.{suffix}"
            filepath = self.preset_ref_images_dir / filename
            
            await asyncio.to_thread(filepath.write_bytes, image_bytes)
            
            # 更新索引
            if preset_key not in self.preset_ref_images:
                self.preset_ref_images[preset_key] = []
            self.preset_ref_images[preset_key].append(filename)
            
            await self._save_json(self.preset_ref_images_file, self.preset_ref_images)
            logger.info(f"预设参考图已保存: {preset_key} -> {filename}")
            return filename
            
        except Exception as e:
            logger.error(f"保存预设参考图失败: {e}")
            return ""

    async def add_preset_ref_images(self, preset_key: str, image_bytes_list: List[bytes]) -> int:
        """
        批量添加预设参考图
        
        Args:
            preset_key: 预设名称
            image_bytes_list: 图片二进制数据列表
            
        Returns:
            成功保存的图片数量
        """
        count = 0
        for img_bytes in image_bytes_list:
            if await self.save_preset_ref_image(preset_key, img_bytes):
                count += 1
        return count

    def get_preset_ref_image_paths(self, preset_key: str) -> List[str]:
        """
        获取预设的所有参考图路径
        
        Args:
            preset_key: 预设名称
            
        Returns:
            图片文件路径列表
        """
        if preset_key not in self.preset_ref_images:
            return []
        
        paths = []
        for filename in self.preset_ref_images[preset_key]:
            filepath = self.preset_ref_images_dir / filename
            if filepath.exists():
                paths.append(str(filepath))
        return paths

    def has_preset_ref_images(self, preset_key: str) -> bool:
        """检查预设是否有参考图"""
        return preset_key in self.preset_ref_images and len(self.preset_ref_images[preset_key]) > 0

    async def clear_preset_ref_images(self, preset_key: str) -> int:
        """
        清除预设的所有参考图
        
        Args:
            preset_key: 预设名称
            
        Returns:
            删除的图片数量
        """
        if preset_key not in self.preset_ref_images:
            return 0
        
        count = 0
        for filename in self.preset_ref_images[preset_key]:
            filepath = self.preset_ref_images_dir / filename
            if filepath.exists():
                try:
                    await asyncio.to_thread(filepath.unlink)
                    count += 1
                except Exception as e:
                    logger.error(f"删除预设参考图失败: {filepath} - {e}")
        
        del self.preset_ref_images[preset_key]
        await self._save_json(self.preset_ref_images_file, self.preset_ref_images)
        return count

    async def remove_preset_ref_image(self, preset_key: str, index: int) -> bool:
        """
        删除预设的指定参考图
        
        Args:
            preset_key: 预设名称
            index: 图片索引（从0开始）
            
        Returns:
            是否删除成功
        """
        if preset_key not in self.preset_ref_images:
            return False
        
        if index < 0 or index >= len(self.preset_ref_images[preset_key]):
            return False
        
        filename = self.preset_ref_images[preset_key][index]
        filepath = self.preset_ref_images_dir / filename
        
        try:
            if filepath.exists():
                await asyncio.to_thread(filepath.unlink)
            self.preset_ref_images[preset_key].pop(index)
            
            # 如果没有参考图了，删除整个条目
            if not self.preset_ref_images[preset_key]:
                del self.preset_ref_images[preset_key]
            
            await self._save_json(self.preset_ref_images_file, self.preset_ref_images)
            return True
        except Exception as e:
            logger.error(f"删除预设参考图失败: {e}")
            return False

    def get_preset_ref_stats(self) -> Dict[str, Any]:
        """
        获取预设参考图统计信息
        
        Returns:
            {
                "total_presets": 有参考图的预设数量,
                "total_images": 总图片数量,
                "total_size_mb": 总大小(MB),
                "details": {预设名: 图片数量}
            }
        """
        total_images = 0
        total_size = 0
        details = {}
        
        for preset_key, filenames in self.preset_ref_images.items():
            valid_count = 0
            for filename in filenames:
                filepath = self.preset_ref_images_dir / filename
                if filepath.exists():
                    total_size += filepath.stat().st_size
                    valid_count += 1
            total_images += valid_count
            if valid_count > 0:
                details[preset_key] = valid_count
        
        return {
            "total_presets": len(details),
            "total_images": total_images,
            "total_size_mb": total_size / (1024 * 1024),
            "details": details
        }

    @staticmethod
    def _normalize_reference_image_for_generation(image_bytes: bytes) -> bytes:
        """Convert nonstandard reference-image formats to a stable PNG input.

        Images arriving in chat are normalized by ``ImageManager`` before they
        reach a drawing channel. Dashboard-uploaded reference images are read
        directly from disk, so formats such as WebP would otherwise take a
        different path. Keep the original file for previews, but normalize the
        in-memory request payload for the drawing API.
        """
        if not image_bytes:
            return image_bytes
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") or image_bytes.startswith(b"\xff\xd8"):
            return image_bytes

        try:
            with PILImage.open(io.BytesIO(image_bytes)) as image:
                if getattr(image, "is_animated", False):
                    image.seek(0)
                normalized = image.convert("RGBA")
                max_side = 1568
                width, height = normalized.size
                if width > max_side or height > max_side:
                    scale = min(max_side / width, max_side / height)
                    normalized = normalized.resize(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        PILImage.Resampling.LANCZOS,
                    )
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue()
        except Exception as exc:
            logger.warning("参考图格式标准化失败，将使用原始文件: %s", exc)
            return image_bytes

    async def load_preset_ref_images_bytes(self, preset_key: str,
                                           normalize_for_generation: bool = False) -> List[bytes]:
        """
        加载预设的所有参考图为字节数据
        
        Args:
            preset_key: 预设名称
            
        Returns:
            图片字节数据列表
        """
        paths = self.get_preset_ref_image_paths(preset_key)
        images = []
        normalized_count = 0
        
        for path in paths:
            try:
                img_bytes = await asyncio.to_thread(Path(path).read_bytes)
                if normalize_for_generation:
                    prepared = await asyncio.to_thread(
                        self._normalize_reference_image_for_generation,
                        img_bytes,
                    )
                    if prepared != img_bytes:
                        normalized_count += 1
                    img_bytes = prepared
                images.append(img_bytes)
            except Exception as e:
                logger.error(f"加载预设参考图失败: {path} - {e}")
        
        if normalized_count:
            logger.info(
                "已将 %s 张已保存参考图标准化为 PNG 后提交绘图渠道，原文件保持不变。",
                normalized_count,
            )
        return images

import json
import asyncio
import io
import os
import random
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List
from PIL import Image as PILImage
from astrbot import logger
from .utils import norm_id


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
        self.preset_images_dir = self.data_dir / "preset_images"
        self.preset_ref_images_dir = self.data_dir / "preset_ref_images"  # 预设参考图目录
        self.generation_cache_dir = self.data_dir / "generation_cache"
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
        self.prompt_map: Dict[str, str] = {}
        # A single lock protects read-modify-write credit/check-in operations.
        # The upstream version could lose credits when concurrent image tasks
        # finished at nearly the same time.
        self._state_lock = asyncio.Lock()

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
        self.generation_history = self._normalize_generation_history(self.generation_history)
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

    async def record_usage(self, uid: str, gid: Optional[str]):
        async with self._state_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.daily_stats.get("date") != today:
                self.daily_stats = {"date": today, "users": {}, "groups": {}}

            uid = norm_id(uid)
            self.daily_stats["users"][uid] = self.daily_stats["users"].get(uid, 0) + 1
            if gid:
                gid = norm_id(gid)
                self.daily_stats["groups"][gid] = self.daily_stats["groups"].get(gid, 0) + 1
            await self._save_json(self.daily_stats_file, self.daily_stats)

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
            seen_ids.add(record_id)
            normalized.append({
                "id": record_id,
                "filename": filename,
                "created_at": str(raw.get("created_at", "") or ""),
                "user_id": norm_id(raw.get("user_id")),
                "group_id": norm_id(raw.get("group_id")),
                "prompt": str(raw.get("prompt", "") or "").strip()[:12_000],
                "model": str(raw.get("model", "") or "").strip()[:200],
                "preset": str(raw.get("preset", "") or "").strip()[:160],
                "task_type": str(raw.get("task_type", "") or "").strip()[:80],
                "image_format": str(raw.get("image_format", "") or "").strip()[:20],
                "width": self._history_int(raw.get("width")),
                "height": self._history_int(raw.get("height")),
                "size_bytes": self._history_int(raw.get("size_bytes")),
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

    def get_generation_image_path(self, record: Dict[str, Any]) -> Optional[Path]:
        if not isinstance(record, dict):
            return None
        path = self._generation_cache_path(record.get("filename"))
        return path if path is not None and path.is_file() else None

    async def save_generation_record(
            self,
            image_bytes: bytes,
            *,
            prompt: str,
            user_id: str,
            group_id: str = "",
            model: str = "",
            preset: str = "",
            task_type: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Persist a successful output image and its Dashboard-visible metadata."""
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
        record = {
            "id": record_id,
            "filename": filename,
            "created_at": timestamp.astimezone().isoformat(timespec="seconds"),
            "user_id": norm_id(user_id),
            "group_id": norm_id(group_id),
            "prompt": str(prompt or "").strip()[:12_000],
            "model": str(model or "").strip()[:200],
            "preset": str(preset or "").strip()[:160],
            "task_type": str(task_type or "").strip()[:80],
            "image_format": image_format,
            "width": width,
            "height": height,
            "size_bytes": len(image_bytes),
            "favorite": False,
            "locked": False,
        }
        path = self._generation_cache_path(filename)
        if path is None:
            return None

        async with self._state_lock:
            try:
                await asyncio.to_thread(path.write_bytes, image_bytes)
            except Exception as exc:
                logger.error("Linghui could not cache a generated image: %s", exc)
                return None
            self.generation_history.insert(0, record)
            await self._save_json(self.generation_history_file, self.generation_history)
        return dict(record)

    async def get_generation_history_page(self, limit: int = 24, offset: int = 0) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
        """Return a bounded history page plus aggregate cache statistics."""
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
        async with self._state_lock:
            records = [dict(item) for item in self.generation_history]

        today = datetime.now().date()
        unique_users = set()
        unique_groups = set()
        today_count = 0
        private_count = 0
        protected_count = 0
        for item in records:
            user_id = norm_id(item.get("user_id"))
            group_id = norm_id(item.get("group_id"))
            if user_id:
                unique_users.add(user_id)
            if group_id:
                unique_groups.add(group_id)
            else:
                private_count += 1
            if item.get("favorite") or item.get("locked"):
                protected_count += 1
            created_at = self._history_created_at(item, None)
            if created_at is not None and created_at.date() == today:
                today_count += 1

        summary = {
            "total": len(records),
            "favorite": sum(1 for item in records if item.get("favorite")),
            "locked": sum(1 for item in records if item.get("locked")),
            "protected": protected_count,
            "size_bytes": sum(self._history_int(item.get("size_bytes")) for item in records),
            "today": today_count,
            "users": len(unique_users),
            "groups": len(unique_groups),
            "private": private_count,
        }
        return records[offset:offset + limit], len(records), summary

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
                self.generation_history.pop(index)
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

    async def cleanup_generation_cache(self, retention_days: int = 7) -> Dict[str, int]:
        """Remove expired unprotected cache entries and stale orphaned files."""
        try:
            retention_days = min(max(1, int(retention_days)), 365)
        except (TypeError, ValueError):
            retention_days = 7
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed_records = 0
        removed_images = 0
        removed_orphans = 0

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
                removed_records += 1

            history_changed = len(retained) != len(self.generation_history)
            self.generation_history = retained
            known_files = {str(record.get("filename", "")) for record in retained}

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

            if history_changed:
                await self._save_json(self.generation_history_file, self.generation_history)

        return {
            "removed_records": removed_records,
            "removed_images": removed_images,
            "removed_orphans": removed_orphans,
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

    async def load_preset_ref_images_bytes(self, preset_key: str) -> List[bytes]:
        """
        加载预设的所有参考图为字节数据
        
        Args:
            preset_key: 预设名称
            
        Returns:
            图片字节数据列表
        """
        paths = self.get_preset_ref_image_paths(preset_key)
        images = []
        
        for path in paths:
            try:
                img_bytes = await asyncio.to_thread(Path(path).read_bytes)
                images.append(img_bytes)
            except Exception as e:
                logger.error(f"加载预设参考图失败: {path} - {e}")
        
        return images

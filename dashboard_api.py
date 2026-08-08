"""AstrBot Dashboard API for Linghui Studio's management page."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from astrbot import logger
from quart import jsonify, request, send_file
from PIL import Image as PILImage
from PIL import ImageOps

from .utils import norm_id


PLUGIN_NAME = "astrbot_plugin_linghui_studio"
DRAWING_CHANNEL_TEMPLATE_KEY = "drawing_channel"
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PREVIEW_MAX_BYTES = 300 * 1024
_HISTORY_PREVIEW_MAX_BYTES = 120 * 1024
_INTERFACE_MODES = {"openai_image", "openai_chat", "gemini_official", "custom_endpoint"}
_DASHBOARD_THEMES = {"dark", "light", "alice"}


class LinghuiDashboardApi:
    """Small, validated API surface consumed by ``pages/linghui-studio``."""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._lock = asyncio.Lock()

    def register(self) -> None:
        routes = (
            ("get_config", self.get_config, ["GET"], "Get Linghui Studio configuration"),
            ("dashboard_theme", self.save_dashboard_theme, ["POST"], "Save Linghui Studio Dashboard theme"),
            ("save_config", self.save_config, ["POST"], "Save Linghui Studio configuration"),
            ("get_usage", self.get_usage, ["GET"], "Get Linghui Studio usage and credits"),
            ("adjust_credit", self.adjust_credit, ["POST"], "Adjust Linghui Studio credits"),
            ("reset_credit", self.reset_credit, ["POST"], "Reset Linghui Studio credits"),
            ("reference", self.reference, ["POST"], "Manage Linghui Studio reference images"),
            ("asset", self.asset, ["GET"], "Preview Linghui Studio reference image"),
            ("generation_history", self.generation_history, ["GET"], "Get Linghui Studio successful generation history"),
            ("generation_download", self.generation_download, ["GET"], "Download one Linghui Studio successful image"),
            ("generation_record", self.generation_record, ["POST"], "Manage Linghui Studio successful generation history"),
        )
        for endpoint, handler, methods, description in routes:
            self.plugin.context.register_web_api(
                f"/{PLUGIN_NAME}/{endpoint}", handler, methods, description
            )

    @staticmethod
    async def _json_body() -> Dict[str, Any]:
        payload = await request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    @staticmethod
    def _as_int(value: Any, minimum: int = 0, maximum: int = 10_000) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = minimum
        return min(max(parsed, minimum), maximum)

    @staticmethod
    def _dashboard_theme(value: Any) -> str:
        theme = str(value or "").strip().lower()
        return theme if theme in _DASHBOARD_THEMES else "dark"

    @staticmethod
    def _id_list(value: Any) -> List[str]:
        if isinstance(value, str):
            values = re.split(r"[\s,;，；]+", value)
        elif isinstance(value, list):
            values = value
        else:
            values = []
        result: List[str] = []
        for item in values:
            normalized = norm_id(item)
            if normalized and normalized not in result:
                result.append(normalized)
        return result[:2_000]

    @staticmethod
    def _identity_label_map(manager: Any) -> Dict[str, Dict[str, str]]:
        """Return display labels without allowing them to replace canonical IDs."""
        raw_labels: Any = None
        getter = getattr(manager, "get_identity_label_map", None)
        if callable(getter):
            try:
                raw_labels = getter()
            except Exception:
                raw_labels = None
        if not isinstance(raw_labels, dict):
            raw_labels = getattr(manager, "identity_labels", {})

        normalized: Dict[str, Dict[str, str]] = {"users": {}, "groups": {}}
        if not isinstance(raw_labels, dict):
            return normalized
        for kind in ("users", "groups"):
            values = raw_labels.get(kind, {})
            if not isinstance(values, dict):
                continue
            for identity_id, entry in values.items():
                safe_id = norm_id(identity_id)
                raw_name = entry.get("name", "") if isinstance(entry, dict) else entry
                name = re.sub(r"\s+", " ", str(raw_name or "")).strip()[:160]
                if safe_id and name:
                    normalized[kind][safe_id] = name
        return normalized

    @staticmethod
    def _mask_keys(value: Any) -> str:
        keys = [item.strip() for item in re.split(r"[\r\n,]+", str(value or "")) if item.strip()]
        if not keys:
            return ""
        return "\n".join(f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "已配置" for key in keys)

    def _public_channel(self, channel: Dict[str, Any], index: int) -> Dict[str, Any]:
        result = {
            "id": str(channel.get("id", f"channel_{index + 1}") or f"channel_{index + 1}"),
            "name": str(channel.get("name", "") or ""),
            "enabled": self._as_bool(channel.get("enabled", True)),
            "fallback_enabled": self._as_bool(channel.get("fallback_enabled", True)),
            "interface_mode": str(channel.get("interface_mode", "openai_chat") or "openai_chat"),
            "image_edit_transport": str(channel.get("image_edit_transport", "auto") or "auto"),
            "base_url": str(channel.get("base_url", "") or ""),
            "model": str(channel.get("model", "") or ""),
            "text_to_image_model": str(channel.get("text_to_image_model", "") or ""),
            "timeout": self._as_int(channel.get("timeout", 120), 5, 900),
            "api_keys_masked": self._mask_keys(channel.get("api_keys", "")),
            "has_api_keys": bool(self._mask_keys(channel.get("api_keys", ""))),
        }
        return result

    def _preset_rows(self) -> List[Dict[str, str]]:
        rows: Dict[str, str] = {}
        for entry in self.plugin.conf.get("prompt_list", []) or []:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            name, prompt = entry.split(":", 1)
            name = name.strip()
            if name:
                rows[name] = prompt.strip()

        # Chat-created presets live in user_prompts.json and have higher
        # priority at runtime. Show that effective value in the Dashboard so
        # edits made on either surface cannot invisibly override each other.
        user_prompts = getattr(self.plugin.data_mgr, "user_prompts", {})
        if isinstance(user_prompts, dict):
            for name, prompt in user_prompts.items():
                name = str(name).strip()
                prompt = str(prompt).strip()
                if name and prompt:
                    rows[name] = prompt
        return [{"name": name, "prompt": prompt} for name, prompt in sorted(rows.items())]

    def _safe_reference_path(self, raw_path: str | Path) -> Path | None:
        """Return a stored reference image only when it stays under the data root."""
        try:
            root = self.plugin.data_mgr.preset_ref_images_dir.resolve()
            path = Path(raw_path).resolve()
        except (OSError, TypeError, ValueError):
            return None
        if root not in path.parents or not path.is_file():
            return None
        return path

    @staticmethod
    def _image_preview_data_url(
            path: Path,
            *,
            max_bytes: int = _PREVIEW_MAX_BYTES,
            max_edge: int = 560,
    ) -> str:
        """Make a small inline JPEG preview for a Bridge-authenticated page response."""
        max_bytes = max(16 * 1024, min(int(max_bytes), _PREVIEW_MAX_BYTES))
        max_edge = max(160, min(int(max_edge), 1_024))
        try:
            with PILImage.open(path) as source:
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
                    (max_edge, 82),
                    (max(int(max_edge * 0.8), 224), 76),
                    (max(int(max_edge * 0.6), 160), 68),
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
                    if len(encoded) <= max_bytes:
                        break
        except Exception as exc:
            logger.warning("Linghui dashboard could not create an image preview: %s", exc)
            return ""

        return f"data:image/jpeg;base64,{base64.b64encode(encoded).decode('ascii')}"

    @staticmethod
    def _reference_preview_data_url(path: Path) -> str:
        """Backward-compatible preview helper used by reference image tests."""
        return LinghuiDashboardApi._image_preview_data_url(path)

    async def _reference_previews(self, paths: List[str]) -> List[str]:
        previews: List[str] = []
        # Decode sequentially to avoid a large batch of source images occupying
        # memory while an administrator opens a reference-heavy configuration.
        for raw_path in paths:
            path = self._safe_reference_path(raw_path)
            if path is None:
                previews.append("")
                continue
            previews.append(await asyncio.to_thread(self._reference_preview_data_url, path))
        return previews

    async def _reference_summary(self) -> Dict[str, Any]:
        manager = self.plugin.data_mgr
        items: List[Dict[str, Any]] = []
        for preset in sorted(manager.preset_ref_images):
            if preset == "_persona_":
                continue
            paths = manager.get_preset_ref_image_paths(preset)
            if not paths:
                continue
            items.append({
                "preset": preset,
                "count": len(paths),
                "images": await self._reference_previews(paths),
            })
        return {"items": items, "stats": manager.get_preset_ref_stats()}

    async def get_config(self):
        channels = self.plugin.conf.get("drawing_channels", []) or []
        if not isinstance(channels, list):
            channels = []
        persona_scenes = self.plugin.conf.get("persona_scene_prompts", []) or []
        if not isinstance(persona_scenes, list):
            persona_scenes = []
        persona_keywords = self.plugin.conf.get("persona_trigger_keywords", []) or []
        if not isinstance(persona_keywords, list):
            persona_keywords = []
        persona_paths = self.plugin.data_mgr.get_preset_ref_image_paths("_persona_")
        persona_previews, reference_summary = await asyncio.gather(
            self._reference_previews(persona_paths),
            self._reference_summary(),
        )
        return jsonify({
            "success": True,
            "dashboard_theme": self._dashboard_theme(self.plugin.conf.get("dashboard_theme", "dark")),
            "settings": {
                "model": str(self.plugin.conf.get("model", "") or ""),
                "text_to_image_model": str(self.plugin.conf.get("text_to_image_model", "") or ""),
                "image_resolution": str(self.plugin.conf.get("image_resolution", "1K") or "1K"),
                "image_aspect_ratio": str(self.plugin.conf.get("image_aspect_ratio", "1:1") or "1:1"),
                "timeout": self._as_int(self.plugin.conf.get("timeout", 120), 5, 900),
                "generation_cache_retention_days": self._as_int(
                    self.plugin.conf.get("generation_cache_retention_days", 7), 1, 365
                ),
                "show_model_info": self._as_bool(self.plugin.conf.get("show_model_info", False)),
                "enable_preset_ref_images": self._as_bool(self.plugin.conf.get("enable_preset_ref_images", True)),
                "enable_persona_mode": self._as_bool(self.plugin.conf.get("enable_persona_mode", False)),
            },
            "channels": [self._public_channel(channel, index) for index, channel in enumerate(channels) if isinstance(channel, dict)],
            "active_drawing_channel": str(self.plugin.conf.get("active_drawing_channel", "") or ""),
            "commands": {
                "namespace": str(self.plugin.conf.get("command_namespace", "") or ""),
                "enable_direct_commands": self._as_bool(self.plugin.conf.get("enable_direct_commands", True)),
            },
            "permissions": {
                "group_access_mode": str(self.plugin.conf.get("group_access_mode", "whitelist") or "whitelist"),
                "allow_private_messages": self._as_bool(self.plugin.conf.get("allow_private_messages", False)),
                "admins_unlimited": self._as_bool(self.plugin.conf.get("admins_unlimited", True)),
                "allowed_users": self._id_list(self.plugin.conf.get("allowed_users", [])),
                "blocked_users": self._id_list(self.plugin.conf.get("blocked_users", [])),
                "group_whitelist": self._id_list(self.plugin.conf.get("group_whitelist", [])),
                "group_blacklist": self._id_list(self.plugin.conf.get("group_blacklist", [])),
                "unlimited_users": self._id_list(self.plugin.conf.get("unlimited_users", [])),
                "unlimited_groups": self._id_list(self.plugin.conf.get("unlimited_groups", [])),
            },
            "usage": {
                "enable_user_limit": self._as_bool(self.plugin.conf.get("enable_user_limit", True)),
                "enable_group_limit": self._as_bool(self.plugin.conf.get("enable_group_limit", False)),
                "enable_checkin": self._as_bool(self.plugin.conf.get("enable_checkin", False)),
                "checkin_fixed_reward": self._as_int(self.plugin.conf.get("checkin_fixed_reward", 3), 0, 999),
                "enable_random_checkin": self._as_bool(self.plugin.conf.get("enable_random_checkin", False)),
                "checkin_random_reward_max": self._as_int(self.plugin.conf.get("checkin_random_reward_max", 5), 1, 999),
            },
            "prompt_tools": {
                "enable_prompt_optimization": self._as_bool(self.plugin.conf.get("enable_prompt_optimization", False)),
                "enable_prompt_translation": self._as_bool(self.plugin.conf.get("enable_prompt_translation", False)),
                "prompt_processor_base_url": str(self.plugin.conf.get("prompt_processor_base_url", "") or ""),
                "prompt_processor_api_key_masked": self._mask_keys(self.plugin.conf.get("prompt_processor_api_key", "")),
                "has_prompt_processor_api_key": bool(self._mask_keys(self.plugin.conf.get("prompt_processor_api_key", ""))),
                "prompt_optimization_model": str(self.plugin.conf.get("prompt_optimization_model", "") or ""),
                "prompt_translation_model": str(self.plugin.conf.get("prompt_translation_model", "") or ""),
                "prompt_processor_timeout": self._as_int(self.plugin.conf.get("prompt_processor_timeout", 30), 5, 300),
                "prompt_optimization_system_prompt": str(self.plugin.conf.get("prompt_optimization_system_prompt", "") or ""),
                "prompt_translation_system_prompt": str(self.plugin.conf.get("prompt_translation_system_prompt", "") or ""),
                "custom_drawing_negative_prompt": str(self.plugin.conf.get("custom_drawing_negative_prompt", "") or ""),
            },
            "persona": {
                "name": str(self.plugin.conf.get("persona_name", "") or ""),
                "description": str(self.plugin.conf.get("persona_description", "") or ""),
                "photo_style": str(self.plugin.conf.get("persona_photo_style", "") or ""),
                "trigger_keywords": persona_keywords,
                "default_prompt": str(self.plugin.conf.get("persona_default_prompt", "") or ""),
                "scene_prompts": persona_scenes,
                "reference_images": persona_previews,
            },
            "presets": self._preset_rows(),
            "references": reference_summary,
        })

    async def save_dashboard_theme(self):
        """Persist the Dashboard skin without applying unrelated form edits."""
        payload = await self._json_body()
        theme = str(payload.get("theme", "") or "").strip().lower()
        if theme not in _DASHBOARD_THEMES:
            return jsonify({"success": False, "message": "无效的 Dashboard 主题。"}), 400

        try:
            async with self._lock:
                self.plugin.conf["dashboard_theme"] = theme
                self.plugin._save_config(["dashboard_theme"])
        except Exception as exc:
            logger.exception("Linghui dashboard theme save failed")
            return jsonify({"success": False, "message": f"主题保存失败：{exc}"}), 500
        return jsonify({"success": True, "theme": theme, "message": "Dashboard 主题已保存。"})

    def _existing_channels(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for item in self.plugin.conf.get("drawing_channels", []) or []:
            if isinstance(item, dict) and item.get("id"):
                result[str(item["id"])] = item
        return result

    def _normalize_channels(self, incoming: Any) -> List[Dict[str, Any]]:
        if not isinstance(incoming, list):
            raise ValueError("绘图渠道必须是列表。")
        existing = self._existing_channels()
        result: List[Dict[str, Any]] = []
        ids: set[str] = set()
        for index, raw in enumerate(incoming[:8], start=1):
            if not isinstance(raw, dict):
                continue
            channel_id = str(raw.get("id", "") or "").strip()
            if not _CHANNEL_ID.fullmatch(channel_id):
                raise ValueError(f"渠道 {index} 的 ID 只能使用字母、数字、下划线和连字符。")
            if channel_id in ids:
                raise ValueError(f"渠道 ID 重复：{channel_id}")
            ids.add(channel_id)
            # The Dashboard keeps the original ID while a channel is being
            # edited. This preserves a masked key when an administrator
            # renames the channel without re-entering that key.
            original_id = str(raw.get("original_id", "") or "").strip()
            old = existing.get(original_id) or existing.get(channel_id, {})
            interface_mode = str(raw.get("interface_mode", "openai_chat") or "openai_chat").strip()
            if interface_mode not in _INTERFACE_MODES:
                raise ValueError(f"渠道 {channel_id} 的接口模式无效。")
            image_edit_transport = str(raw.get("image_edit_transport", "auto") or "auto").strip().lower()
            if image_edit_transport not in {"auto", "multipart", "json"}:
                raise ValueError(f"渠道 {channel_id} 的图生图上传格式无效。")
            api_keys = str(raw.get("api_keys", "") or "").strip()
            if not api_keys and not self._as_bool(raw.get("clear_api_keys", False)):
                api_keys = str(old.get("api_keys", "") or "")
            result.append({
                # AstrBot validates template_list entries by this internal key.
                "__template_key": DRAWING_CHANNEL_TEMPLATE_KEY,
                "id": channel_id,
                "name": str(raw.get("name", "") or "").strip()[:80],
                "enabled": self._as_bool(raw.get("enabled", True)),
                "fallback_enabled": self._as_bool(raw.get("fallback_enabled", True)),
                "interface_mode": interface_mode,
                "image_edit_transport": image_edit_transport,
                "base_url": str(raw.get("base_url", "") or "").strip()[:500],
                "api_keys": api_keys,
                "model": str(raw.get("model", "") or "").strip()[:160],
                "text_to_image_model": str(raw.get("text_to_image_model", "") or "").strip()[:160],
                "timeout": self._as_int(raw.get("timeout", 120), 5, 900),
            })
        return result

    @staticmethod
    def _normalize_presets(incoming: Any) -> List[str]:
        if not isinstance(incoming, list):
            return []
        result: List[str] = []
        names: set[str] = set()
        for raw in incoming[:200]:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "") or "").strip()
            prompt = str(raw.get("prompt", "") or "").strip()
            if not name or not prompt:
                continue
            if ":" in name or len(name) > 48:
                raise ValueError("预设名称不能为空、不能包含冒号且最多 48 个字符。")
            if name in names:
                raise ValueError(f"预设名称重复：{name}")
            names.add(name)
            result.append(f"{name}:{prompt[:12_000]}")
        return result

    @staticmethod
    def _normalize_persona_scenes(incoming: Any) -> List[str]:
        if not isinstance(incoming, list):
            return []
        result: List[str] = []
        names: set[str] = set()
        for raw in incoming[:64]:
            text = str(raw or "").strip()
            if not text:
                continue
            if ":" not in text:
                raise ValueError("人设场景需按“场景名:提示词”逐行填写。")
            name, prompt = (part.strip() for part in text.split(":", 1))
            if not name or not prompt or name in names:
                raise ValueError("人设场景名称不能为空且不能重复。")
            names.add(name)
            result.append(f"{name[:80]}:{prompt[:12_000]}")
        return result

    async def save_config(self):
        payload = await self._json_body()
        async with self._lock:
            try:
                settings = payload.get("settings", {})
                if isinstance(settings, dict):
                    for key in ("model", "text_to_image_model", "image_resolution", "image_aspect_ratio"):
                        if key in settings:
                            self.plugin.conf[key] = str(settings[key] or "").strip()
                    for key, minimum, maximum in (
                        ("timeout", 5, 900),
                        ("generation_cache_retention_days", 1, 365),
                    ):
                        if key in settings:
                            self.plugin.conf[key] = self._as_int(settings[key], minimum, maximum)
                    for key in ("show_model_info", "enable_preset_ref_images", "enable_persona_mode"):
                        if key in settings:
                            self.plugin.conf[key] = self._as_bool(settings[key])

                if "channels" in payload:
                    self.plugin.conf["drawing_channels"] = self._normalize_channels(payload["channels"])
                if "active_drawing_channel" in payload:
                    active_channel = str(payload["active_drawing_channel"] or "").strip()
                    channel_ids = {
                        str(item.get("id", ""))
                        for item in self.plugin.conf.get("drawing_channels", []) or []
                        if isinstance(item, dict)
                    }
                    if active_channel and active_channel not in channel_ids:
                        raise ValueError("当前主渠道必须是已配置的渠道 ID。")
                    self.plugin.conf["active_drawing_channel"] = active_channel

                commands = payload.get("commands", {})
                if isinstance(commands, dict):
                    if "namespace" in commands:
                        namespace = str(commands["namespace"] or "").strip()
                        if len(namespace) > 40 or any(char in namespace for char in "#/！!\r\n"):
                            raise ValueError("命名空间最长 40 个字符，且不能包含命令前缀或换行。")
                        self.plugin.conf["command_namespace"] = namespace
                    if "enable_direct_commands" in commands:
                        self.plugin.conf["enable_direct_commands"] = self._as_bool(commands["enable_direct_commands"])

                permissions = payload.get("permissions", {})
                if isinstance(permissions, dict):
                    if "group_access_mode" in permissions:
                        mode = str(permissions["group_access_mode"] or "whitelist").lower()
                        self.plugin.conf["group_access_mode"] = mode if mode in {"whitelist", "all"} else "whitelist"
                    for key in ("allow_private_messages", "admins_unlimited"):
                        if key in permissions:
                            self.plugin.conf[key] = self._as_bool(permissions[key])
                    for key in (
                        "allowed_users", "blocked_users", "group_whitelist", "group_blacklist",
                        "unlimited_users", "unlimited_groups",
                    ):
                        if key in permissions:
                            self.plugin.conf[key] = self._id_list(permissions[key])

                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    for key in ("enable_user_limit", "enable_group_limit", "enable_checkin", "enable_random_checkin"):
                        if key in usage:
                            self.plugin.conf[key] = self._as_bool(usage[key])
                    for key, minimum, maximum in (
                        ("checkin_fixed_reward", 0, 999),
                        ("checkin_random_reward_max", 1, 999),
                    ):
                        if key in usage:
                            self.plugin.conf[key] = self._as_int(usage[key], minimum, maximum)

                prompt_tools = payload.get("prompt_tools", {})
                if isinstance(prompt_tools, dict):
                    for key in ("enable_prompt_optimization", "enable_prompt_translation"):
                        if key in prompt_tools:
                            self.plugin.conf[key] = self._as_bool(prompt_tools[key])
                    for key in (
                        "prompt_processor_base_url", "prompt_optimization_model", "prompt_translation_model",
                        "prompt_optimization_system_prompt", "prompt_translation_system_prompt",
                    ):
                        if key in prompt_tools:
                            self.plugin.conf[key] = str(prompt_tools[key] or "").strip()
                    if "custom_drawing_negative_prompt" in prompt_tools:
                        self.plugin.conf["custom_drawing_negative_prompt"] = str(
                            prompt_tools["custom_drawing_negative_prompt"] or ""
                        ).strip()[:12_000]
                    if str(prompt_tools.get("prompt_processor_api_key", "") or "").strip():
                        self.plugin.conf["prompt_processor_api_key"] = str(prompt_tools["prompt_processor_api_key"]).strip()
                    if self._as_bool(prompt_tools.get("clear_prompt_processor_api_key", False)):
                        self.plugin.conf["prompt_processor_api_key"] = ""
                    if "prompt_processor_timeout" in prompt_tools:
                        self.plugin.conf["prompt_processor_timeout"] = self._as_int(prompt_tools["prompt_processor_timeout"], 5, 300)

                persona = payload.get("persona", {})
                if isinstance(persona, dict):
                    field_map = {
                        "name": "persona_name",
                        "description": "persona_description",
                        "photo_style": "persona_photo_style",
                    }
                    for source, target in field_map.items():
                        if source in persona:
                            self.plugin.conf[target] = str(persona[source] or "").strip()
                    if "trigger_keywords" in persona:
                        self.plugin.conf["persona_trigger_keywords"] = [
                            str(item).strip()[:80] for item in persona["trigger_keywords"]
                            if str(item).strip()
                        ][:64]
                    if "default_prompt" in persona:
                        self.plugin.conf["persona_default_prompt"] = str(persona["default_prompt"] or "").strip()[:12_000]
                    if "scene_prompts" in persona:
                        self.plugin.conf["persona_scene_prompts"] = self._normalize_persona_scenes(persona["scene_prompts"])

                if "presets" in payload:
                    previous_names = {row["name"] for row in self._preset_rows()}
                    normalized_presets = self._normalize_presets(payload["presets"])
                    self.plugin.conf["prompt_list"] = normalized_presets
                    preset_map = {
                        entry.split(":", 1)[0]: entry.split(":", 1)[1]
                        for entry in normalized_presets
                    }
                    await self.plugin.data_mgr.replace_user_prompts(preset_map)
                    for removed_name in previous_names - set(preset_map):
                        await self.plugin.data_mgr.clear_preset_ref_images(removed_name)

                self.plugin.data_mgr.reload_prompts()
                self.plugin._load_persona_scenes()
                self.plugin._persona_mode = self._as_bool(self.plugin.conf.get("enable_persona_mode", False))
                self.plugin._save_config()
                await self.plugin.api_mgr.refresh()
            except ValueError as exc:
                return jsonify({"success": False, "message": str(exc)}), 400
            except Exception as exc:
                logger.exception("Linghui dashboard configuration save failed")
                return jsonify({"success": False, "message": f"保存失败：{exc}"}), 500
        return jsonify({"success": True, "message": "配置已保存。"})

    async def get_usage(self):
        manager = self.plugin.data_mgr
        identity_labels = self._identity_label_map(manager)
        users = [
            {
                "id": user_id,
                "name": identity_labels["users"].get(norm_id(user_id), ""),
                "credits": credits,
                "checked_in": manager.user_checkin_data.get(user_id, ""),
            }
            for user_id, credits in sorted(manager.user_counts.items())
        ]
        groups = [
            {
                "id": group_id,
                "name": identity_labels["groups"].get(norm_id(group_id), ""),
                "credits": credits,
            }
            for group_id, credits in sorted(manager.group_counts.items())
        ]
        raw_stats = manager.daily_stats if isinstance(manager.daily_stats, dict) else {}
        today = datetime.now().strftime("%Y-%m-%d")
        # The data file is reset on the next successful request. Until then,
        # do not label a prior day's records as today's Dashboard usage.
        is_current_day = raw_stats.get("date") == today
        daily_users = raw_stats.get("users", {})
        daily_groups = raw_stats.get("groups", {})
        stats = {
            "date": today,
            "users": daily_users if is_current_day and isinstance(daily_users, dict) else {},
            "groups": daily_groups if is_current_day and isinstance(daily_groups, dict) else {},
        }
        return jsonify({
            "success": True,
            "users": users,
            "groups": groups,
            "daily_stats": stats,
            "identity_labels": identity_labels,
            "references": manager.get_preset_ref_stats(),
        })

    async def adjust_credit(self):
        payload = await self._json_body()
        kind = str(payload.get("kind", "") or "")
        target_id = norm_id(payload.get("id"))
        amount = self._as_int(payload.get("amount"), 1, 100_000)
        if kind not in {"user", "group"} or not target_id:
            return jsonify({"success": False, "message": "请提供有效的用户/群 ID 和额度。"}), 400
        if kind == "user":
            await self.plugin.data_mgr.add_user_count(target_id, amount)
        else:
            await self.plugin.data_mgr.add_group_count(target_id, amount)
        return jsonify({"success": True, "message": "额度已增加。"})

    async def reset_credit(self):
        payload = await self._json_body()
        kind = str(payload.get("kind", "") or "")
        target_id = norm_id(payload.get("id"))
        if kind not in {"user", "group"} or not target_id:
            return jsonify({"success": False, "message": "请提供有效的用户/群 ID。"}), 400
        if kind == "user":
            await self.plugin.data_mgr.set_user_count(target_id, 0)
            await self.plugin.data_mgr.clear_checkin(target_id)
        else:
            await self.plugin.data_mgr.set_group_count(target_id, 0)
        return jsonify({"success": True, "message": "额度已重置。"})

    @staticmethod
    def _decode_data_url(value: Any) -> bytes:
        text = str(value or "")
        if not text.startswith("data:") or "," not in text:
            raise ValueError("请选择图片文件。")
        try:
            data = base64.b64decode(text.split(",", 1)[1], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("图片数据无法解析。") from exc
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError("图片大小必须在 1 B 到 10 MB 之间。")
        return data

    async def reference(self):
        payload = await self._json_body()
        action = str(payload.get("action", "upload") or "upload")
        preset = str(payload.get("preset", "") or "").strip()
        if not preset or len(preset) > 80:
            return jsonify({"success": False, "message": "请提供有效的预设名称。"}), 400
        if action == "upload":
            try:
                data = self._decode_data_url(payload.get("data_url"))
            except ValueError as exc:
                return jsonify({"success": False, "message": str(exc)}), 400
            filename = await self.plugin.data_mgr.save_preset_ref_image(preset, data)
            return jsonify({"success": bool(filename), "message": "参考图已保存。" if filename else "参考图保存失败。"})
        if action == "delete":
            index = self._as_int(payload.get("index"), 0, 10_000)
            deleted = await self.plugin.data_mgr.remove_preset_ref_image(preset, index)
            return jsonify({"success": deleted, "message": "参考图已删除。" if deleted else "未找到参考图。"})
        if action == "clear":
            deleted = await self.plugin.data_mgr.clear_preset_ref_images(preset)
            return jsonify({"success": True, "message": f"已删除 {deleted} 张参考图。"})
        return jsonify({"success": False, "message": "未知参考图操作。"}), 400

    async def asset(self):
        preset = str(request.args.get("preset", "") or "").strip()
        try:
            index = int(request.args.get("index", "-1"))
        except ValueError:
            index = -1
        paths = self.plugin.data_mgr.get_preset_ref_image_paths(preset)
        if not preset or index < 0 or index >= len(paths):
            return jsonify({"success": False, "message": "未找到参考图。"}), 404
        path = self._safe_reference_path(paths[index])
        if path is None:
            return jsonify({"success": False, "message": "参考图路径无效。"}), 404
        return await send_file(path, mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    async def generation_history(self):
        """Return one paged, authenticated view of successful output cache records."""
        limit = self._as_int(request.args.get("limit", 24), 1, 100)
        offset = self._as_int(request.args.get("offset", 0), 0, 1_000_000)
        favorite_only = self._as_bool(request.args.get("favorite_only", False))
        manager = self.plugin.data_mgr
        records, total, summary = await manager.get_generation_history_page(
            limit=limit,
            offset=offset,
            favorite_only=favorite_only,
        )
        identity_labels = self._identity_label_map(manager)

        public_records: List[Dict[str, Any]] = []
        for record in records:
            image_path = manager.get_generation_image_path(record)
            preview = ""
            if image_path is not None:
                preview = await asyncio.to_thread(
                    self._image_preview_data_url,
                    image_path,
                    max_bytes=_HISTORY_PREVIEW_MAX_BYTES,
                    max_edge=440,
                )
            public_records.append({
                "id": str(record.get("id", "") or ""),
                "created_at": str(record.get("created_at", "") or ""),
                "user_id": norm_id(record.get("user_id")),
                "group_id": norm_id(record.get("group_id")),
                "user_name": str(record.get("user_name", "") or "").strip()[:160]
                or identity_labels["users"].get(norm_id(record.get("user_id")), ""),
                "group_name": str(record.get("group_name", "") or "").strip()[:160]
                or identity_labels["groups"].get(norm_id(record.get("group_id")), ""),
                "prompt": str(record.get("prompt", "") or ""),
                "model": str(record.get("model", "") or ""),
                "preset": str(record.get("preset", "") or ""),
                "task_type": str(record.get("task_type", "") or ""),
                "image_format": str(record.get("image_format", "") or ""),
                "width": self._as_int(record.get("width"), 0, 100_000),
                "height": self._as_int(record.get("height"), 0, 100_000),
                "size_bytes": self._as_int(record.get("size_bytes"), 0, 2_147_483_647),
                "favorite": self._as_bool(record.get("favorite", False)),
                "locked": self._as_bool(record.get("locked", False)),
                "image_available": image_path is not None,
                "preview": preview,
            })

        return jsonify({
            "success": True,
            "records": public_records,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(public_records) < total,
            },
            "summary": summary,
            "favorite_only": favorite_only,
            "retention_days": self._as_int(
                self.plugin.conf.get("generation_cache_retention_days", 7), 1, 365
            ),
        })

    async def generation_download(self):
        """Send one cached original image as an authenticated attachment."""
        record_id = str(request.args.get("id", "") or "").strip()
        if not record_id or len(record_id) > 80:
            return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400

        manager = self.plugin.data_mgr
        record = await manager.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404
        image_path = manager.get_generation_image_path(record)
        if image_path is None:
            return jsonify({"success": False, "message": "原图缓存已不可用。"}), 404

        suffix = image_path.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".img"
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", record_id)[:20] or "image"
        download_name = f"linghui_{safe_id}{suffix}"
        return await send_file(
            image_path,
            mimetype=mimetypes.guess_type(image_path.name)[0] or "application/octet-stream",
            as_attachment=True,
            attachment_filename=download_name,
            cache_timeout=0,
        )

    async def generation_record(self):
        """Update protection flags, explicitly delete, or clean expired cache records."""
        payload = await self._json_body()
        action = str(payload.get("action", "") or "").strip().lower()
        manager = self.plugin.data_mgr

        async with self._lock:
            if action in {"favorite", "lock"}:
                record_id = str(payload.get("id", "") or "").strip()
                if not record_id or len(record_id) > 80:
                    return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400
                desired_value = self._as_bool(payload.get("value", False))
                record = await manager.update_generation_record_flags(
                    record_id,
                    favorite=desired_value if action == "favorite" else None,
                    locked=desired_value if action == "lock" else None,
                )
                if record is None:
                    return jsonify({"success": False, "message": "未找到成功记录。"}), 404
                label = "收藏" if action == "favorite" else "锁定"
                state = "已开启" if desired_value else "已取消"
                return jsonify({
                    "success": True,
                    "message": f"{label}{state}。",
                    "record": {
                        "id": record.get("id", ""),
                        "favorite": self._as_bool(record.get("favorite", False)),
                        "locked": self._as_bool(record.get("locked", False)),
                    },
                })

            if action == "delete":
                record_id = str(payload.get("id", "") or "").strip()
                if not record_id or len(record_id) > 80:
                    return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400
                deleted = await manager.delete_generation_record(record_id)
                if not deleted:
                    return jsonify({"success": False, "message": "未找到记录或缓存图片无法删除。"}), 404
                return jsonify({"success": True, "message": "成功记录已删除。"})

            if action == "cleanup":
                cleanup = getattr(self.plugin, "_cleanup_generation_cache", None)
                if callable(cleanup):
                    result = await cleanup()
                else:
                    result = await manager.cleanup_generation_cache(
                        self._as_int(
                            self.plugin.conf.get("generation_cache_retention_days", 7), 1, 365
                        )
                    )
                removed_records = self._as_int(result.get("removed_records"), 0, 1_000_000)
                removed_images = self._as_int(result.get("removed_images"), 0, 1_000_000)
                removed_orphans = self._as_int(result.get("removed_orphans"), 0, 1_000_000)
                return jsonify({
                    "success": True,
                    "message": (
                        f"已清理 {removed_records} 条过期记录、{removed_images} 张缓存图片"
                        f"和 {removed_orphans} 个遗留文件。"
                    ),
                    "result": {
                        "removed_records": removed_records,
                        "removed_images": removed_images,
                        "removed_orphans": removed_orphans,
                    },
                })

        return jsonify({"success": False, "message": "未知成功记录操作。"}), 400

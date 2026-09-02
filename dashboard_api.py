"""AstrBot Dashboard API for Linghui Studio's management page."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import io
import json
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
from astrbot import logger
from quart import jsonify, request, send_file
from PIL import Image as PILImage
from PIL import ImageOps

from .batch_policy import POLICY_LABELS, normalize_policy
from .error_classify import safe_error_summary
from .protocol_adapters import PROTOCOL_CHOICES, PROTOCOL_LABELS, normalize_protocol
from .utils import is_ambiguous_message_delivery_timeout, norm_id, normalize_api_root


PLUGIN_NAME = "astrbot_plugin_linghui_studio"
PLUGIN_VERSION = "3.8.3"
DRAWING_CHANNEL_TEMPLATE_KEY = "drawing_channel"
_CHANNEL_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_PREVIEW_MAX_BYTES = 300 * 1024
_INTERFACE_MODES = {"openai_image", "openai_chat", "gemini_official", "custom_endpoint"}
_DASHBOARD_THEMES = {"dark", "light", "alice", "terminal", "nebula"}
_DASHBOARD_DENSITIES = {"comfortable", "compact"}


class LinghuiDashboardApi:
    """Small, validated API surface consumed by ``pages/linghui-studio``."""

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._lock = asyncio.Lock()
        self._background_jobs: set[asyncio.Task] = set()

    def _spawn_background_job(self, coroutine) -> asyncio.Task:
        """Keep Dashboard-started generation work alive and log failures."""
        task = asyncio.create_task(coroutine)
        self._background_jobs.add(task)

        def done(completed: asyncio.Task) -> None:
            self._background_jobs.discard(completed)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except Exception:
                error = None
            if error is not None:
                logger.error("Linghui Dashboard background job failed: %s", error)

        task.add_done_callback(done)
        return task

    def register(self) -> None:
        routes = (
            ("get_config", self.get_config, ["GET"], "Get Linghui Studio configuration"),
            ("dashboard_theme", self.save_dashboard_theme, ["POST"], "Save Linghui Studio Dashboard theme"),
            ("dashboard_density", self.save_dashboard_density, ["POST"], "Save Linghui Studio Dashboard layout density"),
            ("save_config", self.save_config, ["POST"], "Save Linghui Studio configuration"),
            ("get_usage", self.get_usage, ["GET"], "Get Linghui Studio usage and credits"),
            ("adjust_credit", self.adjust_credit, ["POST"], "Adjust Linghui Studio credits"),
            ("reset_credit", self.reset_credit, ["POST"], "Reset Linghui Studio credits"),
            ("reference", self.reference, ["POST"], "Manage Linghui Studio reference images"),
            ("asset", self.asset, ["GET"], "Preview Linghui Studio reference image"),
            ("generation_history", self.generation_history, ["GET"], "Get Linghui Studio successful generation history"),
            ("generation_preview", self.generation_preview, ["GET"], "Preview one Linghui Studio successful image"),
            ("generation_prompt", self.generation_prompt, ["GET"], "Get one Linghui Studio successful generation prompt"),
            ("generation_download", self.generation_download, ["GET"], "Download one Linghui Studio successful image"),
            ("generation_sources", self.generation_sources, ["GET"], "Get Linghui Studio image-to-image source metadata"),
            ("generation_source_preview", self.generation_source_preview, ["GET"], "Preview one Linghui Studio image-to-image source"),
            ("generation_source_download", self.generation_source_download, ["GET"], "Download one Linghui Studio image-to-image source"),
            ("generation_record", self.generation_record, ["POST"], "Manage Linghui Studio successful generation history"),
            ("route_health", self.route_health, ["GET"], "Get Linghui Studio route health metrics"),
            ("generation_tasks", self.generation_tasks, ["GET"], "Get Linghui Studio generation tasks"),
            ("generation_task", self.generation_task, ["POST"], "Manage one Linghui Studio generation task"),
            ("persona_state", self.persona_state, ["GET", "POST"], "Manage Linghui Studio daily persona state"),
            ("session_overrides", self.session_overrides, ["GET", "POST"], "Manage Linghui Studio session-scoped model and channel overrides"),
            ("studio", self.studio, ["GET", "POST"], "Manage Linghui Studio server-side image workbench"),
            ("studio_asset", self.studio_asset, ["GET"], "Preview one Linghui Studio workbench asset"),
            ("studio_generate", self.studio_generate, ["POST"], "Run a real Linghui Studio Dashboard drawing request"),
            ("channel_models", self.channel_models, ["GET"], "Refresh model list for one Linghui Studio channel"),
            ("config_export", self.config_export, ["GET"], "Export redacted Linghui Studio configuration"),
            ("config_import", self.config_import, ["POST"], "Preview or import Linghui Studio configuration"),
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
    def _as_float(value: Any, minimum: float = 0.0, maximum: float = 10_000.0) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = minimum
        return min(max(parsed, minimum), maximum)

    @staticmethod
    def _dashboard_theme(value: Any) -> str:
        theme = str(value or "").strip().lower()
        return theme if theme in _DASHBOARD_THEMES else "dark"

    @staticmethod
    def _dashboard_density(value: Any) -> str:
        density = str(value or "").strip().lower()
        return density if density in _DASHBOARD_DENSITIES else "comfortable"

    def _command_prefix_display(self) -> str:
        """Report the deployment's real command prefix so page hints match chat."""
        resolver = getattr(self.plugin, "_wake_prefix_display", None)
        if callable(resolver):
            try:
                return str(resolver() or "")
            except Exception:
                return ""
        return ""

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
            "reference_image_enabled": self._as_bool(channel.get("reference_image_enabled", True)),
            "interface_mode": str(channel.get("interface_mode", "openai_chat") or "openai_chat"),
            "protocol": normalize_protocol(channel.get("protocol", "auto")),
            "image_edit_transport": str(channel.get("image_edit_transport", "auto") or "auto"),
            "base_url": str(channel.get("base_url", "") or ""),
            "model": str(channel.get("model", "") or ""),
            "image_edit_model": str(channel.get("image_edit_model", "") or ""),
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
        persona_profile = getattr(self.plugin, "persona_profile", None)
        persona_state_job = (
            persona_profile.get_state()
            if persona_profile is not None and callable(getattr(persona_profile, "get_state", None))
            else asyncio.sleep(0, result={})
        )
        persona_previews, reference_summary, persona_state = await asyncio.gather(
            self._reference_previews(persona_paths),
            self._reference_summary(),
            persona_state_job,
        )
        api_manager = getattr(self.plugin, "api_mgr", None)
        route_health = (
            api_manager.get_health_snapshot()
            if api_manager is not None and callable(getattr(api_manager, "get_health_snapshot", None))
            else {"channels": []}
        )
        studio_manager = getattr(self.plugin, "studio_mgr", None)
        studio_summary = (
            studio_manager.public_summary()
            if studio_manager is not None and callable(getattr(studio_manager, "public_summary", None))
            else {"slots": [], "order": []}
        )
        return jsonify({
            "success": True,
            "config_version": 1,
            "dashboard_theme": self._dashboard_theme(self.plugin.conf.get("dashboard_theme", "dark")),
            "dashboard_density": self._dashboard_density(self.plugin.conf.get("dashboard_density", "comfortable")),
            "plugin_version": PLUGIN_VERSION,
            "command_prefix": self._command_prefix_display(),
            "settings": {
                "model": str(self.plugin.conf.get("model", "") or ""),
                "text_to_image_model": str(self.plugin.conf.get("text_to_image_model", "") or ""),
                "image_resolution": str(self.plugin.conf.get("image_resolution", "1K") or "1K"),
                "image_aspect_ratio": str(self.plugin.conf.get("image_aspect_ratio", "1:1") or "1:1"),
                "timeout": self._as_int(self.plugin.conf.get("timeout", 120), 5, 900),
                "generation_cache_retention_days": self._as_int(
                    self.plugin.conf.get("generation_cache_retention_days", 7), 1, 365
                ),
                "generation_cache_max_mb": self._as_int(
                    self.plugin.conf.get("generation_cache_max_mb", 2048), 0, 102_400
                ),
                "generation_cache_trim_ratio": self._as_float(
                    self.plugin.conf.get("generation_cache_trim_ratio", 0.15), 0.01, 0.90
                ),
                "result_image_download_timeout": self._as_int(
                    self.plugin.conf.get("result_image_download_timeout", 45), 5, 300
                ),
                "result_image_download_retries": self._as_int(
                    self.plugin.conf.get("result_image_download_retries", 2), 0, 10
                ),
                "download_retries": self._as_int(self.plugin.conf.get("download_retries", 3), 0, 10),
                "preset_table_quality": str(self.plugin.conf.get("preset_table_quality", "高清") or "高清"),
                "preset_table_columns": self._as_int(self.plugin.conf.get("preset_table_columns", 5), 2, 10),
                "generating_msg_template": str(
                    self.plugin.conf.get("generating_msg_template", "🎨 收到请求，正在生成 [{preset}]...") or ""
                ),
                "show_model_info": self._as_bool(self.plugin.conf.get("show_model_info", False)),
                "enable_preset_ref_images": self._as_bool(self.plugin.conf.get("enable_preset_ref_images", True)),
                "enable_persona_mode": self._as_bool(self.plugin.conf.get("enable_persona_mode", False)),
                "enable_binary_image_response": self._as_bool(
                    self.plugin.conf.get("enable_binary_image_response", True)
                ),
                "enable_bare_base64_response": self._as_bool(
                    self.plugin.conf.get("enable_bare_base64_response", True)
                ),
                "stream_heartbeat_tolerant": self._as_bool(
                    self.plugin.conf.get("stream_heartbeat_tolerant", True)
                ),
            },
            "channels": [self._public_channel(channel, index) for index, channel in enumerate(channels) if isinstance(channel, dict)],
            "active_drawing_channel": str(self.plugin.conf.get("active_drawing_channel", "") or ""),
            "reference_image_drawing_channel": str(
                self.plugin.conf.get("reference_image_drawing_channel", "") or ""
            ),
            "route_policy": {
                "failure_threshold": self._as_int(
                    self.plugin.conf.get("channel_failure_threshold", 3), 1, 20
                ),
                "cooldown_seconds": self._as_int(
                    self.plugin.conf.get("channel_cooldown_seconds", 90), 5, 3600
                ),
                "key_retry_count": self._as_int(
                    self.plugin.conf.get("channel_key_retry_count", 1), 0, 20
                ),
                "fallback_on_safety_error": self._as_bool(
                    self.plugin.conf.get("fallback_on_safety_error", False)
                ),
                "enable_session_model_override": self._as_bool(
                    self.plugin.conf.get("enable_session_model_override", True)
                ),
                "enable_session_channel_override": self._as_bool(
                    self.plugin.conf.get("enable_session_channel_override", True)
                ),
                "session_override_ttl_minutes": self._as_int(
                    self.plugin.conf.get("session_override_ttl_minutes", 720), 0, 43_200
                ),
            },
            "protocol_labels": dict(PROTOCOL_LABELS),
            "batch_policy_labels": dict(POLICY_LABELS),
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
                "character_type": str(self.plugin.conf.get("persona_character_type", "auto") or "auto"),
                "enable_daily_state": self._as_bool(
                    self.plugin.conf.get("enable_persona_daily_state", True)
                ),
                "daily_outfits": list(self.plugin.conf.get("persona_daily_outfits", []) or []),
                "daily_moods": list(self.plugin.conf.get("persona_daily_moods", []) or []),
                "time_period_prompts": list(self.plugin.conf.get("persona_time_period_prompts", []) or []),
                "state_timezone": str(
                    self.plugin.conf.get("persona_state_timezone", "Asia/Shanghai") or "Asia/Shanghai"
                ),
                "daily_state": persona_state,
            },
            "tasks": {
                "dedup_seconds": self._as_int(self.plugin.conf.get("task_dedup_seconds", 180), 0, 86_400),
                "history_limit": self._as_int(self.plugin.conf.get("task_history_limit", 500), 50, 5_000),
                "request_retention_days": self._as_int(
                    self.plugin.conf.get("task_request_retention_days", 7), 1, 90
                ),
                "batch_failure_policy": normalize_policy(
                    self.plugin.conf.get("batch_failure_policy", "skip")
                ),
                "batch_max_skips": self._as_int(self.plugin.conf.get("batch_max_skips", 3), 0, 200),
            },
            "route_health": route_health,
            "studio": studio_summary,
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

    async def save_dashboard_density(self):
        """Persist the Dashboard layout density without applying unrelated form edits."""
        payload = await self._json_body()
        density = str(payload.get("density", "") or "").strip().lower()
        if density not in _DASHBOARD_DENSITIES:
            return jsonify({"success": False, "message": "无效的 Dashboard 界面密度。"}), 400

        try:
            async with self._lock:
                self.plugin.conf["dashboard_density"] = density
                self.plugin._save_config(["dashboard_density"])
        except Exception as exc:
            logger.exception("Linghui dashboard density save failed")
            return jsonify({"success": False, "message": f"界面密度保存失败：{exc}"}), 500
        return jsonify({"success": True, "density": density, "message": "Dashboard 界面密度已保存。"})

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
            protocol = str(raw.get("protocol", "auto") or "auto").strip().lower()
            if protocol not in set(PROTOCOL_CHOICES):
                raise ValueError(f"渠道 {channel_id} 的原生协议无效。")
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
                "reference_image_enabled": self._as_bool(raw.get("reference_image_enabled", True)),
                "interface_mode": interface_mode,
                "protocol": protocol,
                "image_edit_transport": image_edit_transport,
                "base_url": str(raw.get("base_url", "") or "").strip()[:500],
                "api_keys": api_keys,
                "model": str(raw.get("model", "") or "").strip()[:160],
                "image_edit_model": str(raw.get("image_edit_model", "") or "").strip()[:160],
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
                # Keep the native-save fallback scoped to fields that this
                # request actually changes. This is especially important for
                # channel selectors whose empty value has a real meaning.
                changed_dynamic_keys: set[str] = set()
                settings = payload.get("settings", {})
                if isinstance(settings, dict):
                    for key in ("model", "text_to_image_model", "image_resolution", "image_aspect_ratio"):
                        if key in settings:
                            self.plugin.conf[key] = str(settings[key] or "").strip()
                            if key in {"model", "text_to_image_model"}:
                                changed_dynamic_keys.add(key)
                    for key, minimum, maximum in (
                        ("timeout", 5, 900),
                        ("generation_cache_retention_days", 1, 365),
                        ("generation_cache_max_mb", 0, 102_400),
                        ("result_image_download_timeout", 5, 300),
                        ("result_image_download_retries", 0, 10),
                        ("download_retries", 0, 10),
                        ("preset_table_columns", 2, 10),
                    ):
                        if key in settings:
                            self.plugin.conf[key] = self._as_int(settings[key], minimum, maximum)
                            if key in {"generation_cache_retention_days", "generation_cache_max_mb"}:
                                changed_dynamic_keys.add(key)
                    if "generation_cache_trim_ratio" in settings:
                        self.plugin.conf["generation_cache_trim_ratio"] = self._as_float(
                            settings["generation_cache_trim_ratio"], 0.01, 0.90
                        )
                        changed_dynamic_keys.add("generation_cache_trim_ratio")
                    if "preset_table_quality" in settings:
                        quality = str(settings["preset_table_quality"] or "高清")
                        self.plugin.conf["preset_table_quality"] = quality if quality in {"标准", "高清", "超清"} else "高清"
                    if "generating_msg_template" in settings:
                        template = str(settings["generating_msg_template"] or "").strip()[:500]
                        self.plugin.conf["generating_msg_template"] = template or "🎨 收到请求，正在生成 [{preset}]..."
                    for key in ("show_model_info", "enable_preset_ref_images", "enable_persona_mode"):
                        if key in settings:
                            self.plugin.conf[key] = self._as_bool(settings[key])
                    for key in (
                        "enable_binary_image_response",
                        "enable_bare_base64_response",
                        "stream_heartbeat_tolerant",
                    ):
                        if key in settings:
                            self.plugin.conf[key] = self._as_bool(settings[key])
                            changed_dynamic_keys.add(key)

                if "channels" in payload:
                    self.plugin.conf["drawing_channels"] = self._normalize_channels(payload["channels"])
                    changed_dynamic_keys.add("drawing_channels")
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
                    changed_dynamic_keys.add("active_drawing_channel")
                if "reference_image_drawing_channel" in payload:
                    reference_channel = str(payload["reference_image_drawing_channel"] or "").strip()
                    channel_ids = {
                        str(item.get("id", ""))
                        for item in self.plugin.conf.get("drawing_channels", []) or []
                        if isinstance(item, dict)
                    }
                    if reference_channel and reference_channel not in channel_ids:
                        raise ValueError("带参考图优先渠道必须是已配置的渠道 ID。")
                    self.plugin.conf["reference_image_drawing_channel"] = reference_channel
                    changed_dynamic_keys.add("reference_image_drawing_channel")

                route_policy = payload.get("route_policy", {})
                if isinstance(route_policy, dict):
                    route_fields = (
                        ("failure_threshold", "channel_failure_threshold", 1, 20),
                        ("cooldown_seconds", "channel_cooldown_seconds", 5, 3600),
                        ("key_retry_count", "channel_key_retry_count", 0, 20),
                    )
                    for source, target, minimum, maximum in route_fields:
                        if source in route_policy:
                            self.plugin.conf[target] = self._as_int(route_policy[source], minimum, maximum)
                            changed_dynamic_keys.add(target)
                    if "fallback_on_safety_error" in route_policy:
                        self.plugin.conf["fallback_on_safety_error"] = self._as_bool(
                            route_policy["fallback_on_safety_error"]
                        )
                        changed_dynamic_keys.add("fallback_on_safety_error")
                    for key in ("enable_session_model_override", "enable_session_channel_override"):
                        if key in route_policy:
                            self.plugin.conf[key] = self._as_bool(route_policy[key])
                            changed_dynamic_keys.add(key)
                    if "session_override_ttl_minutes" in route_policy:
                        ttl_minutes = self._as_int(
                            route_policy["session_override_ttl_minutes"], 0, 43_200
                        )
                        self.plugin.conf["session_override_ttl_minutes"] = ttl_minutes
                        changed_dynamic_keys.add("session_override_ttl_minutes")
                        store = getattr(self.plugin, "session_overrides", None)
                        if store is not None and callable(getattr(store, "set_ttl_minutes", None)):
                            store.set_ttl_minutes(ttl_minutes)

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
                        changed_dynamic_keys.add("custom_drawing_negative_prompt")
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
                    if "character_type" in persona:
                        character_type = str(persona["character_type"] or "auto").strip().lower()
                        if character_type not in {"auto", "real", "anime"}:
                            raise ValueError("人设形象类型必须是 auto、real 或 anime。")
                        self.plugin.conf["persona_character_type"] = character_type
                        changed_dynamic_keys.add("persona_character_type")
                    if "enable_daily_state" in persona:
                        self.plugin.conf["enable_persona_daily_state"] = self._as_bool(persona["enable_daily_state"])
                        changed_dynamic_keys.add("enable_persona_daily_state")
                    for source, target in (
                        ("daily_outfits", "persona_daily_outfits"),
                        ("daily_moods", "persona_daily_moods"),
                        ("time_period_prompts", "persona_time_period_prompts"),
                    ):
                        if source in persona:
                            raw_items = persona[source] if isinstance(persona[source], list) else []
                            self.plugin.conf[target] = [
                                str(item).strip()[:800] for item in raw_items if str(item).strip()
                            ][:128]
                            changed_dynamic_keys.add(target)
                    if "state_timezone" in persona:
                        self.plugin.conf["persona_state_timezone"] = str(
                            persona["state_timezone"] or "Asia/Shanghai"
                        ).strip()[:120]
                        changed_dynamic_keys.add("persona_state_timezone")

                tasks = payload.get("tasks", {})
                if isinstance(tasks, dict):
                    for source, target, minimum, maximum in (
                        ("dedup_seconds", "task_dedup_seconds", 0, 86_400),
                        ("history_limit", "task_history_limit", 50, 5_000),
                        ("request_retention_days", "task_request_retention_days", 1, 90),
                    ):
                        if source in tasks:
                            self.plugin.conf[target] = self._as_int(tasks[source], minimum, maximum)
                            changed_dynamic_keys.add(target)
                    if "batch_failure_policy" in tasks:
                        self.plugin.conf["batch_failure_policy"] = normalize_policy(
                            tasks["batch_failure_policy"]
                        )
                        changed_dynamic_keys.add("batch_failure_policy")
                    if "batch_max_skips" in tasks:
                        self.plugin.conf["batch_max_skips"] = self._as_int(tasks["batch_max_skips"], 0, 200)
                        changed_dynamic_keys.add("batch_max_skips")

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
                    changed_dynamic_keys.add("prompt_list")

                self.plugin.data_mgr.reload_prompts()
                self.plugin._load_persona_scenes()
                self.plugin._persona_mode = self._as_bool(self.plugin.conf.get("enable_persona_mode", False))
                image_manager = getattr(self.plugin, "img_mgr", None)
                if image_manager is not None:
                    image_manager.max_retries = self._as_int(self.plugin.conf.get("download_retries", 3), 0, 10)
                    image_manager.table_quality = str(self.plugin.conf.get("preset_table_quality", "高清") or "高清")
                    image_manager.table_columns = self._as_int(self.plugin.conf.get("preset_table_columns", 5), 2, 10)
                self.plugin._save_config(sorted(changed_dynamic_keys))
                api_manager = getattr(self.plugin, "api_mgr", None)
                if api_manager is not None and callable(getattr(api_manager, "refresh", None)):
                    await api_manager.refresh()
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

    @staticmethod
    def _generation_source_status(manager: Any, record: Dict[str, Any]) -> tuple[str, int, int]:
        """Return an availability state without reading image bytes into memory."""
        raw_sources = record.get("source_images", []) if isinstance(record, dict) else []
        sources = [item for item in raw_sources if isinstance(item, dict)] if isinstance(raw_sources, list) else []
        available_count = sum(
            1 for source_index in range(1, len(sources) + 1)
            if manager.get_generation_source_path(record, source_index) is not None
        )
        if sources:
            if available_count == len(sources):
                return "cached", len(sources), available_count
            if available_count:
                return "partial", len(sources), available_count
            return "missing", len(sources), 0
        status = str(record.get("source_status", "") or "").strip().lower()
        if status not in {"not_applicable", "legacy_unavailable", "missing"}:
            status = "not_applicable"
        return status, 0, 0

    async def _legacy_generation_source_candidates(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Offer current preset/persona images as explicitly non-historical hints.

        Pre-v3.4.0 histories did not store user uploads, quoted images, or
        avatars.  We must not imply that those source images can be restored.
        For a preset/persona path we can, however, show the administrator the
        *current* configured references as a troubleshooting aid.
        """
        task_type = str(record.get("task_type", "") or "")
        preset = str(record.get("preset", "") or "").strip()
        candidates: List[tuple[str, str]] = []
        if "人设" in task_type:
            candidates.append(("_persona_", "当前人设参考图（非本次历史原图）"))
        if preset and preset not in {"自定义", "编辑", "人设"}:
            candidates.append((preset, "当前预设参考图（非本次历史原图）"))

        result: List[Dict[str, Any]] = []
        seen_presets: set[str] = set()
        for preset_name, label in candidates:
            if preset_name in seen_presets:
                continue
            seen_presets.add(preset_name)
            paths = self.plugin.data_mgr.get_preset_ref_image_paths(preset_name)
            for source_index, raw_path in enumerate(paths[:16], start=1):
                path = self._safe_reference_path(raw_path)
                if path is None:
                    continue
                preview = await asyncio.to_thread(self._image_preview_data_url, path)
                result.append({
                    "source_index": source_index,
                    "label": label,
                    "notice": "该图来自当前配置，只能作为候选参考，不能证明它是当次生成使用的原图。",
                    "preview": preview,
                })
        return result

    async def generation_sources(self):
        """Return one record's image-to-image inputs without eager image payloads."""
        record_id = str(request.args.get("id", "") or "").strip()
        if not record_id or len(record_id) > 80:
            return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400

        manager = self.plugin.data_mgr
        record = await manager.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404

        status, source_count, available_count = self._generation_source_status(manager, record)
        raw_sources = record.get("source_images", [])
        sources = raw_sources if isinstance(raw_sources, list) else []
        public_sources: List[Dict[str, Any]] = []
        for source_index, source in enumerate(sources, start=1):
            if not isinstance(source, dict):
                continue
            public_sources.append({
                "source_index": source_index,
                "image_format": str(source.get("image_format", "") or ""),
                "width": self._as_int(source.get("width"), 0, 100_000),
                "height": self._as_int(source.get("height"), 0, 100_000),
                "size_bytes": self._as_int(source.get("size_bytes"), 0, 2_147_483_647),
                "available": manager.get_generation_source_path(record, source_index) is not None,
            })

        legacy_candidates: List[Dict[str, Any]] = []
        if status == "legacy_unavailable":
            legacy_candidates = await self._legacy_generation_source_candidates(record)
        return jsonify({
            "success": True,
            "id": record_id,
            "generation_kind": str(record.get("generation_kind", "text_to_image") or "text_to_image"),
            "source_status": status,
            "source_count": source_count,
            "available_count": available_count,
            "sources": public_sources,
            "legacy_candidates": legacy_candidates,
            "message": (
                "旧版记录没有缓存当次输入原图，无法从日志、引用消息或 QQ 头像中可靠恢复。"
                if status == "legacy_unavailable" else ""
            ),
        })

    async def generation_source_preview(self):
        """Return one actual cached input image preview on demand."""
        record_id = str(request.args.get("id", "") or "").strip()
        source_index = self._as_int(request.args.get("index"), 0, 999)
        if not record_id or len(record_id) > 80 or source_index < 1:
            return jsonify({"success": False, "message": "请提供有效的记录 ID 和输入图序号。"}), 400
        manager = self.plugin.data_mgr
        record = await manager.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404
        preview_path = await manager.get_or_create_generation_source_preview(record, source_index)
        if preview_path is None:
            return jsonify({"success": False, "message": "该输入原图缓存不可用。"}), 404
        try:
            preview_bytes = await asyncio.to_thread(preview_path.read_bytes)
        except OSError as exc:
            logger.warning("Linghui dashboard could not read cached input preview: %s", exc)
            return jsonify({"success": False, "message": "该输入原图预览不可用。"}), 404
        if not preview_bytes:
            return jsonify({"success": False, "message": "该输入原图预览不可用。"}), 404
        return jsonify({
            "success": True,
            "id": record_id,
            "index": source_index,
            "preview": f"data:image/jpeg;base64,{base64.b64encode(preview_bytes).decode('ascii')}",
        })

    async def generation_source_download(self):
        """Send one cached request input as an authenticated attachment."""
        record_id = str(request.args.get("id", "") or "").strip()
        source_index = self._as_int(request.args.get("index"), 0, 999)
        if not record_id or len(record_id) > 80 or source_index < 1:
            return jsonify({"success": False, "message": "请提供有效的记录 ID 和输入图序号。"}), 400
        manager = self.plugin.data_mgr
        record = await manager.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404
        source_path = manager.get_generation_source_path(record, source_index)
        if source_path is None:
            return jsonify({"success": False, "message": "该输入原图缓存不可用。"}), 404
        suffix = source_path.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            suffix = ".img"
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", record_id)[:20] or "image"
        return await send_file(
            source_path,
            mimetype=mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
            as_attachment=True,
            attachment_filename=f"linghui_{safe_id}_source_{source_index}{suffix}",
            cache_timeout=0,
        )

    async def generation_history(self):
        """Return one paged, authenticated view of successful output cache records."""
        limit = self._as_int(request.args.get("limit", 24), 1, 100)
        offset = self._as_int(request.args.get("offset", 0), 0, 1_000_000)
        favorite_only = self._as_bool(request.args.get("favorite_only", False))
        generation_kind = str(request.args.get("mode", "all") or "all").strip().lower()
        if generation_kind not in {"all", "text_to_image", "image_to_image"}:
            generation_kind = "all"
        raw_group_filter = str(request.args.get("group_id", "") or "").strip()[:160]
        group_filter = "__private__" if raw_group_filter == "__private__" else norm_id(raw_group_filter)
        user_filter = norm_id(request.args.get("user_id"))
        manager = self.plugin.data_mgr
        records, total, summary, view = await manager.get_generation_history_page(
            limit=limit,
            offset=offset,
            favorite_only=favorite_only,
            generation_kind=generation_kind,
            group_filter=group_filter,
            user_filter=user_filter,
        )
        identity_labels = self._identity_label_map(manager)

        raw_filter_options = view.get("filter_options", {}) if isinstance(view, dict) else {}
        user_counts = raw_filter_options.get("users", {}) if isinstance(raw_filter_options, dict) else {}
        group_counts = raw_filter_options.get("groups", {}) if isinstance(raw_filter_options, dict) else {}
        filter_options = {
            "users": [
                {
                    "id": user_id,
                    "name": identity_labels["users"].get(user_id, ""),
                    "count": self._as_int(count, 0, 2_147_483_647),
                }
                for user_id, count in sorted(
                    user_counts.items(),
                    key=lambda item: (-self._as_int(item[1], 0, 2_147_483_647), item[0]),
                )
                if user_id
            ],
            "groups": [
                {
                    "id": group_id,
                    "name": identity_labels["groups"].get(group_id, ""),
                    "count": self._as_int(count, 0, 2_147_483_647),
                }
                for group_id, count in sorted(
                    group_counts.items(),
                    key=lambda item: (-self._as_int(item[1], 0, 2_147_483_647), item[0]),
                )
                if group_id
            ],
            "private_count": self._as_int(raw_filter_options.get("private_count", 0), 0, 2_147_483_647),
        }

        public_records: List[Dict[str, Any]] = []
        for record in records:
            image_path = manager.get_generation_image_path(record)
            record_id = str(record.get("id", "") or "")
            source_status, source_count, source_available_count = self._generation_source_status(manager, record)
            public_records.append({
                "id": record_id,
                "created_at": str(record.get("created_at", "") or ""),
                "user_id": norm_id(record.get("user_id")),
                "group_id": norm_id(record.get("group_id")),
                "user_name": str(record.get("user_name", "") or "").strip()[:160]
                or identity_labels["users"].get(norm_id(record.get("user_id")), ""),
                "group_name": str(record.get("group_name", "") or "").strip()[:160]
                or identity_labels["groups"].get(norm_id(record.get("group_id")), ""),
                # Prompts can be up to 12,000 characters. Fetch the full text
                # only when an administrator expands a record in the Dashboard.
                "has_prompt": bool(str(record.get("prompt", "") or "").strip()),
                "model": str(record.get("model", "") or ""),
                "channel_id": str(record.get("channel_id", "") or "").strip()[:80],
                "channel_name": str(record.get("channel_name", "") or "").strip()[:160],
                "preset": str(record.get("preset", "") or ""),
                "task_type": str(record.get("task_type", "") or ""),
                "image_format": str(record.get("image_format", "") or ""),
                "width": self._as_int(record.get("width"), 0, 100_000),
                "height": self._as_int(record.get("height"), 0, 100_000),
                "size_bytes": self._as_int(record.get("size_bytes"), 0, 2_147_483_647),
                "generation_kind": str(record.get("generation_kind", "text_to_image") or "text_to_image"),
                "source_status": source_status,
                "source_count": source_count,
                "source_available_count": source_available_count,
                "source_size_bytes": self._as_int(record.get("source_size_bytes"), 0, 2_147_483_647),
                "favorite": self._as_bool(record.get("favorite", False)),
                "locked": self._as_bool(record.get("locked", False)),
                "image_available": image_path is not None,
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
            "view_summary": view.get("summary", {}) if isinstance(view, dict) else {},
            "favorite_only": favorite_only,
            "mode": generation_kind,
            "filters": {
                "group_id": group_filter,
                "user_id": user_filter,
                "options": filter_options,
            },
            "retention_days": self._as_int(
                self.plugin.conf.get("generation_cache_retention_days", 7), 1, 365
            ),
        })

    async def generation_preview(self):
        """Return one on-demand JPEG data URL through the Plugin Page bridge."""
        record_id = str(request.args.get("id", "") or "").strip()
        if not record_id or len(record_id) > 80:
            return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400

        manager = self.plugin.data_mgr
        record = await manager.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404
        preview_path = await manager.get_or_create_generation_preview(record)
        if preview_path is None:
            return jsonify({"success": False, "message": "成功图片预览不可用。"}), 404
        try:
            preview_bytes = await asyncio.to_thread(preview_path.read_bytes)
        except OSError as exc:
            logger.warning("Linghui dashboard could not read cached generation preview: %s", exc)
            return jsonify({"success": False, "message": "成功图片预览不可用。"}), 404
        if not preview_bytes:
            return jsonify({"success": False, "message": "成功图片预览不可用。"}), 404
        return jsonify({
            "success": True,
            "id": record_id,
            "preview": f"data:image/jpeg;base64,{base64.b64encode(preview_bytes).decode('ascii')}",
        })

    async def generation_prompt(self):
        """Return a single prompt on demand so list pages stay lightweight."""
        record_id = str(request.args.get("id", "") or "").strip()
        if not record_id or len(record_id) > 80:
            return jsonify({"success": False, "message": "请提供有效的成功记录 ID。"}), 400

        record = await self.plugin.data_mgr.get_generation_record(record_id)
        if record is None:
            return jsonify({"success": False, "message": "未找到成功记录。"}), 404
        prompt = str(record.get("prompt", "") or "")
        return jsonify({
            "success": True,
            "id": record_id,
            "has_prompt": bool(prompt.strip()),
            "prompt": prompt,
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

    @staticmethod
    def _friendly_stamp(value: Any) -> str:
        """Shorten an ISO timestamp to 月-日 时:分 for toast messages."""
        text = str(value or "").strip()
        if len(text) < 16:
            return ""
        return text[5:16].replace("T", " ")

    def _cleanup_idle_message(self, result: Dict[str, Any]) -> str:
        """Explain an empty sweep so "0 removed" does not look like a broken button."""
        retention_days = self._as_int(
            result.get("retention_days")
            or self.plugin.conf.get("generation_cache_retention_days", 7),
            1,
            365,
        )
        remaining = self._as_int(result.get("remaining_records"), 0, 1_000_000)
        protected = self._as_int(result.get("protected_records"), 0, 1_000_000)
        if remaining <= 0:
            return f"没有需要清理的内容：成功记录缓存当前是空的（保留期 {retention_days} 天）。"
        parts = [f"没有需要清理的内容：保留期 {retention_days} 天，现有 {remaining} 条记录都还在保留期内"]
        if protected:
            parts.append(f"其中 {protected} 条已收藏或锁定，永久保留")
        oldest = self._friendly_stamp(result.get("oldest_created_at"))
        expiry = self._friendly_stamp(result.get("next_expiry_at"))
        if oldest and expiry:
            parts.append(f"最早一条生成于 {oldest}，要到 {expiry} 才会过期")
        return "；".join(parts) + "。"

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
                removed_previews = self._as_int(result.get("removed_previews"), 0, 1_000_000)
                removed_source_images = self._as_int(result.get("removed_source_images"), 0, 1_000_000)
                removed_source_previews = self._as_int(result.get("removed_source_previews"), 0, 1_000_000)
                removed_orphans = self._as_int(result.get("removed_orphans"), 0, 1_000_000)
                removed_total = (
                    removed_records + removed_images + removed_previews
                    + removed_source_images + removed_source_previews + removed_orphans
                )
                if removed_total:
                    message = (
                        f"已清理 {removed_records} 条过期记录、{removed_images} 张结果图、"
                        f"{removed_source_images} 张输入原图、{removed_previews + removed_source_previews} 张缩略图"
                        f"和 {removed_orphans} 个遗留文件。"
                    )
                else:
                    message = self._cleanup_idle_message(result)
                return jsonify({
                    "success": True,
                    "message": message,
                    "result": {
                        "removed_records": removed_records,
                        "removed_images": removed_images,
                        "removed_previews": removed_previews,
                        "removed_source_images": removed_source_images,
                        "removed_source_previews": removed_source_previews,
                        "removed_orphans": removed_orphans,
                        "removed_total": removed_total,
                        "retention_days": self._as_int(result.get("retention_days"), 1, 365),
                        "remaining_records": self._as_int(result.get("remaining_records"), 0, 1_000_000),
                        "remaining_bytes": self._as_int(result.get("remaining_bytes"), 0, 1_000_000_000_000),
                        "protected_records": self._as_int(result.get("protected_records"), 0, 1_000_000),
                        "oldest_created_at": str(result.get("oldest_created_at", "") or ""),
                        "next_expiry_at": str(result.get("next_expiry_at", "") or ""),
                    },
                })

        return jsonify({"success": False, "message": "未知成功记录操作。"}), 400

    def _public_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Return task fields that are useful to an authenticated administrator."""
        attempts = task.get("attempt_chain", [])
        if not isinstance(attempts, list):
            attempts = []
        request_meta = task.get("request", {})
        if not isinstance(request_meta, dict):
            request_meta = {}
        return {
            "id": str(task.get("id", "") or ""),
            "status": str(task.get("status", "failed") or "failed"),
            "task_type": str(task.get("task_type", "") or ""),
            "session_id": str(task.get("session_id", "") or ""),
            "user_id": norm_id(task.get("user_id")),
            "group_id": norm_id(task.get("group_id")),
            "user_name": str(task.get("user_name", "") or "")[:160],
            "group_name": str(task.get("group_name", "") or "")[:160],
            "prompt": str(task.get("prompt", "") or "")[:12_000],
            "preset": str(task.get("preset", "") or "")[:160],
            "requested_model": str(task.get("requested_model", "") or "")[:200],
            "actual_model": str(task.get("actual_model", "") or "")[:200],
            "channel_id": str(task.get("channel_id", "") or "")[:80],
            "channel_name": str(task.get("channel_name", "") or "")[:160],
            "created_at": str(task.get("created_at", "") or ""),
            "updated_at": str(task.get("updated_at", "") or ""),
            "started_at": str(task.get("started_at", "") or ""),
            "finished_at": str(task.get("finished_at", "") or ""),
            "duration": float(task.get("duration", 0.0) or 0.0),
            "error": safe_error_summary(task.get("error", ""), 600),
            "error_category": str(task.get("error_category", "") or "")[:80],
            "attempt_chain": attempts[:64],
            "result_record_id": str(task.get("result_record_id", "") or "")[:80],
            "delivery_status": str(task.get("delivery_status", "") or "")[:40],
            "input_count": self._as_int(task.get("input_count"), 0, 10_000),
            "rerun_of": str(task.get("rerun_of", "") or "")[:80],
            "progress": task.get("progress", {}) if isinstance(task.get("progress"), dict) else {},
            "request": {
                "use_text_to_image_api": bool(request_meta.get("use_text_to_image_api", False)),
                "aspect_ratio": str(request_meta.get("aspect_ratio", "") or "")[:40],
                "resolution": str(request_meta.get("resolution", "") or "")[:40],
                "preferred_channel_id": str(request_meta.get("preferred_channel_id", "") or "")[:80],
                "allow_fallback": bool(request_meta.get("allow_fallback", True)),
            },
        }

    async def route_health(self):
        return jsonify({
            "success": True,
            "health": self.plugin.api_mgr.get_health_snapshot(),
            "last_route": self.plugin.api_mgr.get_last_metrics(),
        })

    async def generation_tasks(self):
        limit = self._as_int(request.args.get("limit", 40), 1, 100)
        offset = self._as_int(request.args.get("offset", 0), 0, 1_000_000)
        status = str(request.args.get("status", "all") or "all").strip().lower()
        tasks, total, summary = await self.plugin.task_mgr.list_tasks(
            limit=limit,
            offset=offset,
            status=status,
        )
        return jsonify({
            "success": True,
            "tasks": [self._public_task(item) for item in tasks],
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(tasks) < total,
            },
            "summary": summary,
            "status": status,
        })

    async def _run_dashboard_generation(
        self,
        *,
        task_id: str,
        session_id: str,
        user_id: str,
        group_id: str,
        user_name: str,
        group_name: str,
        images: List[bytes],
        prompt: str,
        model: str,
        preset: str,
        task_type: str,
        use_text_to_image_api: bool,
        aspect_ratio: str,
        resolution: str,
        negative_prompt: str = "",
        preferred_channel_id: str = "",
        allow_fallback: bool = True,
    ) -> None:
        try:
            result = await self.plugin._call_generation_api_task(
                task_id,
                images,
                prompt,
                model,
                use_text_to_image_api=use_text_to_image_api,
                aspect_ratio=aspect_ratio or None,
                resolution=resolution or None,
                negative_prompt=negative_prompt or None,
                preferred_channel_id=preferred_channel_id or None,
                allow_fallback=allow_fallback,
            )
        except asyncio.CancelledError:
            # cancel_task() already persisted the final state.
            return
        except Exception as exc:
            await self.plugin.task_mgr.finish_failure(
                task_id,
                exc,
                metrics=self.plugin._snapshot_generation_route_metrics(),
            )
            return

        metrics = self.plugin._snapshot_generation_route_metrics()
        if not isinstance(result, bytes):
            await self.plugin.task_mgr.finish_failure(task_id, result, metrics=metrics)
            return

        try:
            result = await self.plugin._prepare_send_image_bytes(result)
            record = await self.plugin._record_generation_result(
                session_id,
                result,
                user_id=user_id,
                group_id=group_id,
                user_name=user_name,
                group_name=group_name,
                prompt=prompt,
                model=model,
                preset_name=preset,
                task_type=task_type,
                reference_images=images,
                route_metrics=metrics,
                task_id=task_id,
                delivery_status="dashboard_only",
            )
            record_id = str(record.get("id", "") or "") if isinstance(record, dict) else ""
            await self.plugin.task_mgr.mark_generated(
                task_id,
                metrics=metrics,
                result_record_id=record_id,
            )
            if record_id:
                await self.plugin.data_mgr.update_generation_record_delivery(
                    record_id,
                    "dashboard_only",
                )
            await self.plugin.task_mgr.finish_success(
                task_id,
                metrics=metrics,
                result_record_id=record_id,
                delivery_status="dashboard_only",
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self.plugin.task_mgr.finish_failure(
                task_id,
                exc,
                metrics=metrics,
            )

    def _channel_ids(self) -> set[str]:
        return {
            str(item.get("id", "") or "").strip()
            for item in self.plugin.conf.get("drawing_channels", []) or []
            if isinstance(item, dict) and str(item.get("id", "") or "").strip()
        }

    async def generation_task(self):
        payload = await self._json_body()
        action = str(payload.get("action", "") or "").strip().lower()
        task_id = str(payload.get("id", "") or "").strip()

        if action == "cleanup":
            result = await self.plugin.task_mgr.cleanup()
            removed = self._as_int(result.get("removed"), 0, 1_000_000)
            remaining = self._as_int(result.get("after"), 0, 1_000_000)
            retention_days = self._as_int(
                self.plugin.conf.get("task_request_retention_days", 7), 1, 365
            )
            if removed:
                message = f"已整理 {removed} 条超期任务及其请求素材，剩余 {remaining} 条。"
            else:
                message = (
                    f"没有需要整理的任务：保留期 {retention_days} 天，"
                    f"现有 {remaining} 条任务都还在保留期内。"
                )
            return jsonify({"success": True, "message": message, "result": result})

        if not task_id or len(task_id) > 80:
            return jsonify({"success": False, "message": "请提供有效的任务 ID。"}), 400

        if action == "cancel":
            success, message = await self.plugin.task_mgr.cancel_task(task_id)
            return jsonify({"success": success, "message": message}), (200 if success else 409)

        task = await self.plugin.task_mgr.get_task(task_id)
        if task is None:
            return jsonify({"success": False, "message": "未找到任务。"}), 404

        if action == "rerun":
            images = await self.plugin.task_mgr.get_request_images(task_id)
            if int(task.get("input_count", 0) or 0) > 0 and not images:
                return jsonify({
                    "success": False,
                    "message": "该任务的输入原图缓存已过期，无法强制重跑。",
                }), 409
            preferred_channel_id = str(payload.get("channel_id", "") or "").strip()
            if preferred_channel_id and preferred_channel_id not in self._channel_ids():
                return jsonify({"success": False, "message": "指定绘图渠道不存在。"}), 400
            model = str(payload.get("model", "") or task.get("requested_model", "") or "").strip()[:200]
            request_meta = task.get("request", {}) if isinstance(task.get("request"), dict) else {}
            allow_fallback = self._as_bool(payload.get("allow_fallback", True))
            new_task, _ = await self.plugin.task_mgr.begin_task(
                request_id=f"dashboard-rerun:{uuid.uuid4().hex}",
                session_id=str(task.get("session_id", "") or "dashboard:rerun"),
                user_id=norm_id(task.get("user_id")),
                group_id=norm_id(task.get("group_id")),
                user_name=str(task.get("user_name", "") or ""),
                group_name=str(task.get("group_name", "") or ""),
                task_type=str(task.get("task_type", "") or "Dashboard 强制重跑"),
                prompt=str(task.get("prompt", "") or ""),
                preset=str(task.get("preset", "") or ""),
                requested_model=model,
                images=images,
                force=True,
                nonce=uuid.uuid4().hex,
                rerun_of=task_id,
                request={
                    "use_text_to_image_api": bool(request_meta.get("use_text_to_image_api", not images)),
                    "aspect_ratio": str(request_meta.get("aspect_ratio", "") or ""),
                    "resolution": str(request_meta.get("resolution", "") or ""),
                    "preferred_channel_id": preferred_channel_id,
                    "allow_fallback": allow_fallback,
                },
            )
            self._spawn_background_job(self._run_dashboard_generation(
                task_id=str(new_task.get("id", "")),
                session_id=str(new_task.get("session_id", "") or "dashboard:rerun"),
                user_id=norm_id(new_task.get("user_id")),
                group_id=norm_id(new_task.get("group_id")),
                user_name=str(new_task.get("user_name", "") or ""),
                group_name=str(new_task.get("group_name", "") or ""),
                images=images,
                prompt=str(new_task.get("prompt", "") or ""),
                model=model,
                preset=str(new_task.get("preset", "") or ""),
                task_type=str(new_task.get("task_type", "") or "Dashboard 强制重跑"),
                use_text_to_image_api=bool(request_meta.get("use_text_to_image_api", not images)),
                aspect_ratio=str(request_meta.get("aspect_ratio", "") or ""),
                resolution=str(request_meta.get("resolution", "") or ""),
                preferred_channel_id=preferred_channel_id,
                allow_fallback=allow_fallback,
            ))
            return jsonify({
                "success": True,
                "message": "已创建强制重跑任务，可在任务中心查看进度。",
                "task": self._public_task(new_task),
            }), 202

        if action == "resend":
            # Keep AstrBot message classes lazy so the Dashboard storage/API
            # module remains independently testable without booting the core.
            from astrbot.api.event import MessageChain
            from astrbot.core.message.components import Image as MessageImage, Plain

            record_id = str(task.get("result_record_id", "") or "").strip()
            session_id = str(task.get("session_id", "") or "").strip()
            if not record_id or not session_id or session_id.startswith("dashboard:"):
                return jsonify({"success": False, "message": "该任务没有可重发的会话或成功图片。"}), 409
            record = await self.plugin.data_mgr.get_generation_record(record_id)
            image_path = self.plugin.data_mgr.get_generation_image_path(record or {}) if record else None
            if image_path is None:
                return jsonify({"success": False, "message": "成功图片缓存已不可用。"}), 404
            image_bytes = await asyncio.to_thread(image_path.read_bytes)
            delivery_status = "sent"
            try:
                await self.plugin.context.send_message(
                    session_id,
                    MessageChain([
                        MessageImage.fromBytes(image_bytes),
                        Plain("\nDashboard 已重发这张成功图片。"),
                    ]),
                )
            except Exception as exc:
                if not is_ambiguous_message_delivery_timeout(exc):
                    return jsonify({
                        "success": False,
                        "message": f"重发失败：{safe_error_summary(exc, 240)}",
                    }), 502
                delivery_status = "possibly_sent"
            await self.plugin.data_mgr.update_generation_record_delivery(record_id, delivery_status)
            updated = await self.plugin.task_mgr.update_task(
                task_id,
                status="possibly_sent" if delivery_status == "possibly_sent" else "succeeded",
                delivery_status=delivery_status,
                error="",
                error_category="",
            )
            return jsonify({
                "success": True,
                "message": "平台回执较慢，图片可能已经送达；不会自动重复发送。"
                if delivery_status == "possibly_sent" else "图片已重发。",
                "task": self._public_task(updated or task),
            })

        return jsonify({"success": False, "message": "未知任务操作。"}), 400

    async def persona_state(self):
        if request.method == "GET":
            return jsonify({"success": True, "state": await self.plugin.persona_profile.get_state()})

        payload = await self._json_body()
        action = str(payload.get("action", "update") or "update").strip().lower()
        if action == "refresh":
            state = await self.plugin.persona_profile.refresh_state()
        elif action == "update":
            outfit = str(payload.get("outfit", "") or "").strip()[:500]
            mood = str(payload.get("mood", "") or "").strip()[:500]
            if not outfit and not mood:
                return jsonify({"success": False, "message": "请填写今日穿搭或心情。"}), 400
            state = await self.plugin.persona_profile.update_state(outfit=outfit, mood=mood)
        else:
            return jsonify({"success": False, "message": "未知人设状态操作。"}), 400
        return jsonify({"success": True, "message": "今日人设状态已更新。", "state": state})

    @staticmethod
    def _iso_epoch(value: Any) -> str:
        try:
            seconds = float(value or 0.0)
        except (TypeError, ValueError):
            return ""
        if seconds <= 0:
            return ""
        try:
            return datetime.fromtimestamp(seconds).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""

    def _channel_name_map(self) -> Dict[str, str]:
        names: Dict[str, str] = {}
        for index, channel in enumerate(self.plugin.conf.get("drawing_channels", []) or []):
            if not isinstance(channel, dict):
                continue
            channel_id = str(channel.get("id", "") or "").strip()
            if not channel_id:
                continue
            names[channel_id] = str(channel.get("name", "") or "").strip() or f"渠道 {index + 1}"
        return names

    async def session_overrides(self):
        """List or clear the per-conversation model/channel overrides."""
        store = getattr(self.plugin, "session_overrides", None)
        if store is None:
            return jsonify({"success": False, "message": "会话级覆盖尚未初始化。"}), 503

        if request.method == "POST":
            payload = await self._json_body()
            action = str(payload.get("action", "") or "").strip().lower()
            if action == "clear_all":
                store.clear_all()
            elif action == "clear":
                session_id = str(payload.get("session_id", "") or "").strip()
                if not session_id:
                    return jsonify({"success": False, "message": "缺少会话 ID。"}), 400
                store.clear(session_id)
            else:
                return jsonify({"success": False, "message": "未知的会话覆盖操作。"}), 400

        channel_names = self._channel_name_map()
        overrides: List[Dict[str, Any]] = []
        for item in store.list_all():
            channel_id = str(item.get("channel_id", "") or "")
            overrides.append({
                "session_id": str(item.get("session_id", "") or ""),
                "label": str(item.get("label", "") or ""),
                "scope": str(item.get("scope", "") or ""),
                "channel_id": channel_id,
                "channel_name": channel_names.get(channel_id, channel_id),
                "model": str(item.get("model", "") or ""),
                "updated_at": self._iso_epoch(item.get("updated_at", 0.0)),
                "expires_at": self._iso_epoch(item.get("expires_at", 0.0)),
            })
        return jsonify({
            "success": True,
            "overrides": overrides,
            "enabled": {
                "model": self._as_bool(self.plugin.conf.get("enable_session_model_override", True)),
                "channel": self._as_bool(self.plugin.conf.get("enable_session_channel_override", True)),
            },
            "ttl_minutes": self._as_int(self.plugin.conf.get("session_override_ttl_minutes", 720), 0, 43_200),
        })

    async def studio(self):
        manager = self.plugin.studio_mgr
        if request.method == "GET":
            return jsonify({"success": True, "studio": manager.public_summary()})

        payload = await self._json_body()
        action = str(payload.get("action", "upload") or "upload").strip().lower()
        slot = str(payload.get("slot", "") or "").strip().lower()
        try:
            slot = manager.validate_slot(slot)
            if action == "upload":
                data = self._decode_data_url(payload.get("data_url"))
                item = await manager.add_image(slot, data, label=str(payload.get("label", "") or ""))
                return jsonify({"success": True, "message": "工作台参考图已上传。", "item": item})
            if action == "import_record":
                record_id = str(payload.get("record_id", "") or "").strip()
                record = await self.plugin.data_mgr.get_generation_record(record_id)
                path = self.plugin.data_mgr.get_generation_image_path(record or {}) if record else None
                if path is None:
                    return jsonify({"success": False, "message": "成功记录原图缓存不可用。"}), 404
                data = await asyncio.to_thread(path.read_bytes)
                item = await manager.add_image(
                    slot,
                    data,
                    label=str(payload.get("label", "") or record.get("preset", "") or "成功记录"),
                    source_record_id=record_id,
                )
                return jsonify({"success": True, "message": "成功图片已加入工作台。", "item": item})
            if action == "delete":
                deleted = await manager.remove_image(slot, payload.get("id", ""))
                return jsonify({"success": deleted, "message": "工作台图片已删除。" if deleted else "未找到工作台图片。"}), (200 if deleted else 404)
            if action == "clear":
                count = await manager.clear_slot(slot)
                return jsonify({"success": True, "message": f"已清空 {count} 张工作台图片。"})
            if action == "reorder":
                ordered_ids = payload.get("ordered_ids", [])
                if not isinstance(ordered_ids, list):
                    raise ValueError("排序参数必须是图片 ID 列表。")
                items = await manager.reorder(slot, ordered_ids)
                return jsonify({"success": True, "message": "工作台顺序已保存。", "items": items})
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except Exception as exc:
            logger.exception("Linghui Dashboard studio operation failed")
            return jsonify({"success": False, "message": f"工作台操作失败：{safe_error_summary(exc, 240)}"}), 500
        return jsonify({"success": False, "message": "未知工作台操作。"}), 400

    async def studio_asset(self):
        slot = str(request.args.get("slot", "") or "").strip()
        asset_id = str(request.args.get("id", "") or "").strip()
        path = self.plugin.studio_mgr.get_asset_path(slot, asset_id)
        if path is None:
            return jsonify({"success": False, "message": "工作台图片不存在。"}), 404
        download = self._as_bool(request.args.get("download", False))
        if not download:
            try:
                preview = await asyncio.to_thread(
                    self._image_preview_data_url,
                    path,
                    max_bytes=180 * 1024,
                    max_edge=420,
                )
            except Exception as exc:
                return jsonify({
                    "success": False,
                    "message": f"工作台图片预览失败：{safe_error_summary(exc, 180)}",
                }), 500
            return jsonify({"success": True, "slot": slot, "id": asset_id, "preview": preview})
        return await send_file(
            path,
            mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            as_attachment=download,
            attachment_filename=f"linghui_studio_{asset_id}{path.suffix}",
            cache_timeout=0,
        )

    async def studio_generate(self):
        payload = await self._json_body()
        prompt = str(payload.get("prompt", "") or "").strip()[:12_000]
        if not prompt:
            return jsonify({"success": False, "message": "请填写试画提示词。"}), 400

        selections = payload.get("selections", [])
        if not isinstance(selections, list):
            selections = []
        images = await self.plugin.studio_mgr.load_selected_images(selections)
        uploads = payload.get("uploads", [])
        if isinstance(uploads, list):
            try:
                images.extend(self._decode_data_url(item) for item in uploads[:16])
            except ValueError as exc:
                return jsonify({"success": False, "message": str(exc)}), 400
        images = [item for item in images if isinstance(item, bytes) and item][:32]

        mode = str(payload.get("mode", "auto") or "auto").strip().lower()
        if mode not in {"auto", "text_to_image", "image_to_image"}:
            return jsonify({"success": False, "message": "试画模式无效。"}), 400
        if mode == "image_to_image" and not images:
            return jsonify({"success": False, "message": "图生图试画至少需要一张工作台参考图。"}), 400
        use_text_to_image_api = mode == "text_to_image" or (mode == "auto" and not images)
        if use_text_to_image_api:
            images = []

        preferred_channel_id = str(payload.get("channel_id", "") or "").strip()
        if preferred_channel_id and preferred_channel_id not in self._channel_ids():
            return jsonify({"success": False, "message": "指定绘图渠道不存在。"}), 400
        model = str(payload.get("model", "") or "").strip()[:200]
        if not model:
            model = str(
                self.plugin.conf.get("text_to_image_model" if use_text_to_image_api else "model", "") or ""
            ).strip()
        aspect_ratio = str(payload.get("aspect_ratio", self.plugin.conf.get("image_aspect_ratio", "")) or "").strip()[:40]
        resolution = str(payload.get("resolution", self.plugin.conf.get("image_resolution", "")) or "").strip()[:40]
        allow_fallback = self._as_bool(payload.get("allow_fallback", True))
        negative_prompt = str(payload.get("negative_prompt", "") or "").strip()[:12_000]
        task_type = "工作台文生图" if use_text_to_image_api else "工作台图生图"
        task, _ = await self.plugin.task_mgr.begin_task(
            request_id=f"dashboard-studio:{uuid.uuid4().hex}",
            session_id="dashboard:studio",
            user_id="dashboard",
            user_name="Dashboard",
            task_type=task_type,
            prompt=prompt,
            preset="工作台",
            requested_model=model,
            images=images,
            force=True,
            nonce=uuid.uuid4().hex,
            request={
                "use_text_to_image_api": use_text_to_image_api,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "preferred_channel_id": preferred_channel_id,
                "allow_fallback": allow_fallback,
            },
        )
        self._spawn_background_job(self._run_dashboard_generation(
            task_id=str(task.get("id", "")),
            session_id="dashboard:studio",
            user_id="dashboard",
            group_id="",
            user_name="Dashboard",
            group_name="",
            images=images,
            prompt=prompt,
            model=model,
            preset="工作台",
            task_type=task_type,
            use_text_to_image_api=use_text_to_image_api,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            negative_prompt=negative_prompt,
            preferred_channel_id=preferred_channel_id,
            allow_fallback=allow_fallback,
        ))
        return jsonify({
            "success": True,
            "message": "真实试画任务已创建，可在任务中心查看进度和结果。",
            "task": self._public_task(task),
        }), 202

    def _resolve_channel_config(self, channel_id: str) -> Dict[str, Any] | None:
        channel_id = str(channel_id or "").strip()
        channels = self.plugin.conf.get("drawing_channels", []) or []
        if channel_id:
            return next(
                (copy.deepcopy(item) for item in channels if isinstance(item, dict) and str(item.get("id", "")) == channel_id),
                None,
            )
        if channels:
            first = next((copy.deepcopy(item) for item in channels if isinstance(item, dict)), None)
            if first is not None:
                return first
        return {
            "id": "legacy",
            "name": "兼容单接口",
            "interface_mode": self.plugin.conf.get("interface_mode", "openai_chat"),
            "base_url": self.plugin.conf.get("base_url", ""),
            "api_keys": self.plugin.conf.get("api_keys", ""),
            "model": self.plugin.conf.get("model", ""),
        }

    async def channel_models(self):
        channel_id = str(request.args.get("channel_id", "") or "").strip()
        channel = self._resolve_channel_config(channel_id)
        if channel is None:
            return jsonify({"success": False, "message": "未找到绘图渠道。"}), 404

        interface_mode = str(channel.get("interface_mode", "openai_chat") or "openai_chat").strip()
        base_url = str(channel.get("base_url", "") or self.plugin.conf.get("base_url", "") or "").strip()
        root = normalize_api_root(base_url)
        if not root:
            return jsonify({"success": False, "message": "该渠道未配置接口地址。"}), 400
        normalize_keys = getattr(self.plugin.api_mgr, "_normalize_keys", None)
        keys = normalize_keys(channel.get("api_keys", "")) if callable(normalize_keys) else []
        if not keys and callable(normalize_keys):
            keys = normalize_keys(self.plugin.conf.get("api_keys", ""))
        key = keys[0] if keys else ""
        if not key:
            return jsonify({"success": False, "message": "该渠道未配置可用密钥。"}), 400

        if interface_mode == "gemini_official":
            candidates = [f"{root}/v1beta/models", f"{root}/v1/models"]
        else:
            candidates = [f"{root}/v1/models", f"{root}/models"]
        timeout = aiohttp.ClientTimeout(total=min(max(int(channel.get("timeout", 30) or 30), 10), 60))
        headers = {"Accept": "application/json"}
        params = None
        if interface_mode == "gemini_official":
            params = {"key": key}
        else:
            headers["Authorization"] = f"Bearer {key}"
        proxy = str(getattr(self.plugin.img_mgr, "proxy", "") or "").strip() or None

        last_error = ""
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in candidates:
                try:
                    async with session.get(url, headers=headers, params=params, proxy=proxy) as response:
                        text_body = await response.text()
                        if response.status >= 400:
                            last_error = f"HTTP {response.status}: {safe_error_summary(text_body, 200)}"
                            continue
                        try:
                            data = json.loads(text_body)
                        except json.JSONDecodeError:
                            last_error = "模型列表接口没有返回 JSON。"
                            continue
                        models: List[Dict[str, Any]] = []
                        if isinstance(data, dict) and isinstance(data.get("data"), list):
                            for item in data["data"]:
                                if isinstance(item, dict) and item.get("id"):
                                    models.append({
                                        "id": str(item.get("id", "")),
                                        "name": str(item.get("name", "") or item.get("id", "")),
                                    })
                        elif isinstance(data, dict) and isinstance(data.get("models"), list):
                            for item in data["models"]:
                                if not isinstance(item, dict):
                                    continue
                                model_id = str(item.get("name", "") or item.get("id", "")).removeprefix("models/")
                                if model_id:
                                    models.append({
                                        "id": model_id,
                                        "name": str(item.get("displayName", "") or model_id),
                                        "methods": item.get("supportedGenerationMethods", []),
                                    })
                        unique: Dict[str, Dict[str, Any]] = {}
                        for item in models:
                            unique.setdefault(item["id"], item)
                        ordered = [unique[key] for key in sorted(unique, key=str.casefold)]
                        return jsonify({
                            "success": True,
                            "channel_id": str(channel.get("id", "") or "legacy"),
                            "models": ordered,
                            "count": len(ordered),
                            "source_url": url,
                        })
                except Exception as exc:
                    last_error = safe_error_summary(exc, 220)
        return jsonify({
            "success": False,
            "message": f"刷新模型列表失败：{last_error or '接口不支持模型列表查询。'}",
        }), 502

    @staticmethod
    def _is_secret_config_key(key: str) -> bool:
        normalized = str(key or "").strip().lower()
        return normalized in {
            "api_key", "api_keys", "generic_api_keys", "gemini_api_keys",
            "text_to_image_api_keys", "prompt_processor_api_key",
        } or normalized.endswith("_api_key") or normalized.endswith("_api_keys")

    @staticmethod
    def _secret_placeholder(value: Any) -> str:
        return "__KEEP_EXISTING_SECRET__" if str(value or "").strip() else ""

    def _schema(self) -> Dict[str, Dict[str, Any]]:
        path = Path(__file__).with_name("_conf_schema.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning("Linghui Dashboard could not read config schema: %s", exc)
            return {}

    def _export_config_document(self) -> Dict[str, Any]:
        schema = self._schema()
        exported: Dict[str, Any] = {}
        redacted_fields: List[str] = []
        for key in schema:
            value = copy.deepcopy(self.plugin.conf.get(key, schema[key].get("default")))
            if key == "drawing_channels" and isinstance(value, list):
                for index, channel in enumerate(value):
                    if not isinstance(channel, dict):
                        continue
                    if str(channel.get("api_keys", "") or "").strip():
                        channel["api_keys"] = self._secret_placeholder(channel.get("api_keys"))
                        redacted_fields.append(f"drawing_channels[{index}].api_keys")
            elif self._is_secret_config_key(key):
                if str(value or "").strip():
                    value = self._secret_placeholder(value)
                    redacted_fields.append(key)
            exported[key] = value
        return {
            "format": "linghui-studio-config",
            "format_version": 1,
            "plugin": PLUGIN_NAME,
            "plugin_version": PLUGIN_VERSION,
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "secrets_redacted": True,
            "redacted_fields": redacted_fields,
            "config": exported,
        }

    async def config_export(self):
        document = self._export_config_document()
        if self._as_bool(request.args.get("download", False)):
            payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
            return await send_file(
                io.BytesIO(payload),
                mimetype="application/json; charset=utf-8",
                as_attachment=True,
                attachment_filename=f"linghui_studio_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                cache_timeout=0,
            )
        return jsonify({"success": True, "document": document})

    @staticmethod
    def _preview_config_value(key: str, value: Any) -> Any:
        if LinghuiDashboardApi._is_secret_config_key(key):
            return "已配置" if str(value or "").strip() else "未配置"
        if key == "drawing_channels" and isinstance(value, list):
            return [
                {
                    "id": str(item.get("id", "") or ""),
                    "name": str(item.get("name", "") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "interface_mode": str(item.get("interface_mode", "") or ""),
                    "base_url": str(item.get("base_url", "") or ""),
                    "model": str(item.get("model", "") or ""),
                    "has_api_keys": bool(str(item.get("api_keys", "") or "").strip()),
                }
                for item in value if isinstance(item, dict)
            ]
        return value

    def _normalize_import_config(self, raw_config: Dict[str, Any]) -> tuple[Dict[str, Any], List[str], List[Dict[str, Any]]]:
        schema = self._schema()
        normalized: Dict[str, Any] = {}
        ignored = [str(key) for key in raw_config if key not in schema]
        existing_channels = self._existing_channels()

        for key, raw_value in raw_config.items():
            definition = schema.get(key)
            if not isinstance(definition, dict):
                continue
            if self._is_secret_config_key(key) and str(raw_value or "").strip() == "__KEEP_EXISTING_SECRET__":
                continue
            field_type = str(definition.get("type", "string") or "string")
            if key == "drawing_channels":
                incoming = copy.deepcopy(raw_value) if isinstance(raw_value, list) else []
                for channel in incoming:
                    if not isinstance(channel, dict):
                        continue
                    channel_id = str(channel.get("id", "") or "").strip()
                    imported_key = str(channel.get("api_keys", "") or "").strip()
                    if imported_key == "__KEEP_EXISTING_SECRET__":
                        channel["api_keys"] = ""
                        channel["original_id"] = channel_id
                    elif not imported_key and channel_id in existing_channels:
                        channel["original_id"] = channel_id
                normalized[key] = self._normalize_channels(incoming)
                continue
            if field_type == "bool":
                value = self._as_bool(raw_value)
            elif field_type == "int":
                value = self._as_int(
                    raw_value,
                    int(definition.get("min", -2_147_483_648)),
                    int(definition.get("max", 2_147_483_647)),
                )
            elif field_type == "float":
                value = self._as_float(
                    raw_value,
                    float(definition.get("min", -1_000_000.0)),
                    float(definition.get("max", 1_000_000.0)),
                )
            elif field_type == "list":
                if not isinstance(raw_value, list):
                    raise ValueError(f"配置项 {key} 必须是列表。")
                value = copy.deepcopy(raw_value[:2_000])
            else:
                value = str(raw_value or "")[:50_000]
            options = definition.get("options")
            if isinstance(options, list) and options and value not in options:
                raise ValueError(f"配置项 {key} 的值不在允许范围内。")
            normalized[key] = value

        preflight: List[Dict[str, Any]] = []
        channels = normalized.get("drawing_channels")
        if not isinstance(channels, list):
            channels = copy.deepcopy(self.plugin.conf.get("drawing_channels", []) or [])
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            channel_id = str(channel.get("id", "") or "")
            issues: List[str] = []
            if not str(channel.get("base_url", "") or "").strip():
                issues.append("未配置接口地址")
            if not str(channel.get("model", "") or "").strip() and not str(channel.get("text_to_image_model", "") or "").strip():
                issues.append("未配置模型")
            effective_keys = str(channel.get("api_keys", "") or "").strip()
            if not effective_keys and channel_id in existing_channels:
                effective_keys = str(existing_channels[channel_id].get("api_keys", "") or "").strip()
            if not effective_keys:
                issues.append("未配置密钥")
            preflight.append({
                "id": channel_id,
                "name": str(channel.get("name", "") or channel_id),
                "ready": not issues,
                "issues": issues,
            })
        return normalized, ignored, preflight

    async def config_import(self):
        payload = await self._json_body()
        document = payload.get("document", payload.get("config", {}))
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except json.JSONDecodeError as exc:
                return jsonify({"success": False, "message": f"配置 JSON 无法解析：{exc}"}), 400
        if isinstance(document, dict) and isinstance(document.get("config"), dict):
            raw_config = document["config"]
        elif isinstance(document, dict):
            raw_config = document
        else:
            return jsonify({"success": False, "message": "导入内容必须是灵绘工坊配置 JSON。"}), 400

        try:
            normalized, ignored, preflight = self._normalize_import_config(raw_config)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        changed = [
            key for key, value in normalized.items()
            if self.plugin.conf.get(key) != value
        ]
        diff = [
            {
                "key": key,
                "before": self._preview_config_value(key, self.plugin.conf.get(key)),
                "after": self._preview_config_value(key, normalized[key]),
            }
            for key in changed[:300]
        ]
        action = str(payload.get("action", "preview") or "preview").strip().lower()
        if action == "preview":
            return jsonify({
                "success": True,
                "message": f"导入预览完成，共 {len(changed)} 项变化。",
                "changed_keys": changed,
                "ignored_keys": ignored,
                "diff": diff,
                "channel_preflight": preflight,
            })
        if action != "apply":
            return jsonify({"success": False, "message": "未知配置导入操作。"}), 400

        snapshot = {key: copy.deepcopy(self.plugin.conf.get(key)) for key in normalized}
        try:
            async with self._lock:
                for key, value in normalized.items():
                    self.plugin.conf[key] = copy.deepcopy(value)
                self.plugin.data_mgr.reload_prompts()
                self.plugin._load_persona_scenes()
                self.plugin._persona_mode = self._as_bool(self.plugin.conf.get("enable_persona_mode", False))
                self.plugin.img_mgr.max_retries = self._as_int(self.plugin.conf.get("download_retries", 3), 0, 10)
                self.plugin.img_mgr.table_quality = str(self.plugin.conf.get("preset_table_quality", "高清") or "高清")
                self.plugin.img_mgr.table_columns = self._as_int(self.plugin.conf.get("preset_table_columns", 5), 2, 10)
                self.plugin._save_config(changed)
                await self.plugin.api_mgr.refresh()
        except Exception as exc:
            for key, value in snapshot.items():
                self.plugin.conf[key] = value
            try:
                self.plugin.data_mgr.reload_prompts()
                self.plugin._load_persona_scenes()
                self.plugin._persona_mode = self._as_bool(
                    self.plugin.conf.get("enable_persona_mode", False)
                )
                self.plugin.img_mgr.max_retries = self._as_int(
                    self.plugin.conf.get("download_retries", 3), 0, 10
                )
                self.plugin.img_mgr.table_quality = str(
                    self.plugin.conf.get("preset_table_quality", "高清") or "高清"
                )
                self.plugin.img_mgr.table_columns = self._as_int(
                    self.plugin.conf.get("preset_table_columns", 5), 2, 10
                )
                # The import may have reached the native config write before a
                # later refresh failed. Persist the restored snapshot as well,
                # otherwise a plugin restart could resurrect the failed import.
                self.plugin._save_config(changed)
                await self.plugin.api_mgr.refresh()
            except Exception:
                logger.exception("Linghui Dashboard could not persist every rollback side effect")
            logger.exception("Linghui Dashboard config import rolled back")
            return jsonify({
                "success": False,
                "message": f"导入失败，已回滚：{safe_error_summary(exc, 240)}",
            }), 500
        return jsonify({
            "success": True,
            "message": f"配置已导入并保存，共更新 {len(changed)} 项。",
            "changed_keys": changed,
            "ignored_keys": ignored,
            "channel_preflight": preflight,
        })

    async def close(self) -> None:
        jobs = list(self._background_jobs)
        self._background_jobs.clear()
        for job in jobs:
            if not job.done():
                job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

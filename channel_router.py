"""Ordered drawing-channel routing with automatic fallback."""

from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List

from astrbot import logger

from .api_manager import ApiManager
from .prompt_processor import PromptProcessor


class DrawingChannelRouter:
    """Dispatch generation requests to the active channel then fallbacks."""

    def __init__(self, config: Any):
        self.config = config
        self.prompt_processor = PromptProcessor(config)
        self._clients: Dict[str, ApiManager] = {}
        self._last_metrics: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _normalize_keys(value: Any) -> List[str]:
        return ApiManager._normalize_keys(value)

    def _raw_channels(self) -> List[Dict[str, Any]]:
        raw = self.config.get("drawing_channels", [])
        if not isinstance(raw, list):
            return []
        channels: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            channel_id = str(item.get("id", "") or "").strip()
            if not channel_id:
                channel_id = f"channel_{index}"
            if channel_id in seen:
                logger.warning("Linghui ignored duplicate drawing channel id: %s", channel_id)
                continue
            seen.add(channel_id)
            copied = copy.deepcopy(item)
            copied["id"] = channel_id
            channels.append(copied)
        return channels

    def channels(self) -> List[Dict[str, Any]]:
        return self._raw_channels()

    def has_available_keys(self) -> bool:
        channels = self._raw_channels()
        shared_keys = (
            self._normalize_keys(self.config.get("api_keys"))
            or self._normalize_keys(self.config.get("generic_api_keys"))
            or self._normalize_keys(self.config.get("gemini_api_keys"))
        )
        if channels:
            # A channel inherits the legacy/global key pool when its own key
            # field is empty, matching _channel_config's merge behavior.
            return any(self._normalize_keys(channel.get("api_keys")) or shared_keys for channel in channels)
        return bool(shared_keys)

    def _ordered_channels(self) -> List[Dict[str, Any]]:
        channels = self._raw_channels()
        if not channels:
            return []
        selected = str(self.config.get("active_drawing_channel", "") or "").strip()
        if not selected or selected.lower() in {"auto", "自动"}:
            primary = channels[0]
        else:
            active = next((item for item in channels if item["id"] == selected), None)
            if active is None:
                logger.warning("Linghui active drawing channel %s is unavailable; using configured order.", selected)
                primary = channels[0]
            else:
                primary = active

        # The primary channel is always tried. Every later candidate must opt
        # into fallback, including when the configured order is used in auto
        # mode. This makes the Dashboard toggle match runtime behavior.
        fallbacks = [
            item for item in channels
            if item["id"] != primary["id"] and item.get("fallback_enabled", True) is not False
        ]
        return [primary] + fallbacks

    def _channel_config(self, channel: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(self.config)
        # Channel keys intentionally override only transport/model fields. Global
        # image handling, limits, proxy, and logging settings remain shared.
        for key in (
            "interface_mode",
            "base_url",
            "api_keys",
            "model",
            "text_to_image_model",
            "text_to_image_api_url",
            "text_to_image_api_keys",
            "timeout",
            "use_stream",
            "generic_prefer_images_api",
        ):
            value = channel.get(key)
            if value not in (None, "", []):
                merged[key] = value
        return merged

    @staticmethod
    def _is_success(result: Any) -> bool:
        return isinstance(result, bytes) and bool(result)

    @staticmethod
    def _should_try_fallback(result: Any) -> bool:
        # A returned string is the existing ApiManager's structured failure
        # contract.  Let every independently configured channel have a chance.
        return not isinstance(result, bytes)

    def _resolve_model(self, requested: str, channel: Dict[str, Any], text_to_image: bool) -> str:
        global_default = str(self.config.get("model", "") or "").strip()
        global_t2i = str(self.config.get("text_to_image_model", "") or "").strip()
        channel_default = str(channel.get("model", "") or "").strip()
        channel_t2i = str(channel.get("text_to_image_model", "") or "").strip()
        requested = str(requested or "").strip()
        if text_to_image and channel_t2i and (not requested or requested in {global_default, global_t2i}):
            return channel_t2i
        if channel_default and (not requested or requested == global_default):
            return channel_default
        return requested or channel_t2i or channel_default or global_t2i or global_default

    async def _client_for(self, channel: Dict[str, Any]) -> ApiManager:
        channel_id = channel["id"]
        async with self._lock:
            client = self._clients.get(channel_id)
            if client is None:
                client = ApiManager(self._channel_config(channel))
                self._clients[channel_id] = client
            return client

    async def call_api(
        self,
        images: List[bytes],
        prompt: str,
        model: str,
        legacy_use_power_or_proxy: Any = None,
        proxy: str | None = None,
        use_text_to_image_api: bool = False,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
    ) -> bytes | str:
        prepared_prompt = await self.prompt_processor.prepare(prompt)
        channels = self._ordered_channels()
        if not channels:
            # Compatibility path for an installation upgraded from upstream.
            client = await self._client_for({"id": "legacy"})
            result = await client.call_api(
                images, prepared_prompt, model, legacy_use_power_or_proxy, proxy,
                use_text_to_image_api=use_text_to_image_api,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )
            self._last_metrics = {**client.get_last_metrics(), "channel_id": "legacy", "fallback_count": 0}
            return result

        failures: List[str] = []
        for index, channel in enumerate(channels):
            client = await self._client_for(channel)
            resolved_model = self._resolve_model(model, channel, use_text_to_image_api)
            try:
                result = await client.call_api(
                    images, prepared_prompt, resolved_model, legacy_use_power_or_proxy, proxy,
                    use_text_to_image_api=use_text_to_image_api,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                )
            except Exception as exc:
                logger.exception("Linghui channel %s raised an unexpected error.", channel["id"])
                result = f"渠道异常：{type(exc).__name__}: {exc}"
            self._last_metrics = {
                **client.get_last_metrics(),
                "channel_id": channel["id"],
                "channel_name": str(channel.get("name", channel["id"])),
                "model": resolved_model,
                "fallback_count": index,
            }
            if self._is_success(result):
                if index:
                    logger.info("Linghui drawing succeeded via fallback channel %s.", channel["id"])
                return result
            failures.append(f"{channel['id']}: {str(result)[:180]}")
            if not self._should_try_fallback(result):
                break

        return "绘图渠道均失败：" + " | ".join(failures)

    def get_last_metrics(self) -> Dict[str, Any]:
        return dict(self._last_metrics)

    async def refresh(self) -> None:
        old_clients = list(self._clients.values())
        self._clients.clear()
        for client in old_clients:
            session = getattr(client, "_session", None)
            if session is not None and not session.closed:
                await session.close()

    async def close(self) -> None:
        await self.refresh()

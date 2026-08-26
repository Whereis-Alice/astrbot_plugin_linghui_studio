"""Ordered drawing-channel routing with classified fallback and health state."""

from __future__ import annotations

import asyncio
import copy
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from astrbot import logger

from .api_manager import ApiManager
from .error_classify import GenerationError, classify_generation_error, safe_error_summary
from .prompt_processor import PromptProcessor
from .utils import append_final_instruction, append_negative_prompt


class DrawingChannelRouter:
    """Dispatch generation requests to the active channel then fallbacks.

    Provider errors are classified before the router decides whether to rotate
    a key, switch channels, or temporarily cool down an unstable route. Route
    metrics use task-local storage so concurrent batches do not read another
    request's final channel by mistake.
    """

    def __init__(self, config: Any):
        self.config = config
        self.prompt_processor = PromptProcessor(config)
        self._clients: Dict[str, ApiManager] = {}
        self._last_metrics: Dict[str, Any] = {}
        self._metrics_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
            f"linghui_route_metrics_{id(self)}", default=None
        )
        self._health: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._session_store: Any = None

    @staticmethod
    def _normalize_keys(value: Any) -> List[str]:
        return ApiManager._normalize_keys(value)

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, minimum), maximum)

    def _failure_threshold(self) -> int:
        return self._bounded_int(self.config.get("channel_failure_threshold", 3), 3, 1, 20)

    def _cooldown_seconds(self) -> int:
        return self._bounded_int(self.config.get("channel_cooldown_seconds", 90), 90, 5, 3_600)

    def _key_retry_count(self) -> int:
        return self._bounded_int(self.config.get("channel_key_retry_count", 1), 1, 0, 20)

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

    def _shared_keys(self) -> List[str]:
        return (
            self._normalize_keys(self.config.get("api_keys"))
            or self._normalize_keys(self.config.get("generic_api_keys"))
            or self._normalize_keys(self.config.get("gemini_api_keys"))
        )

    def _channel_keys(self, channel: Dict[str, Any]) -> List[str]:
        return self._normalize_keys(channel.get("api_keys")) or self._shared_keys()

    def has_available_keys(self) -> bool:
        channels = self._raw_channels()
        shared_keys = self._shared_keys()
        if channels:
            return any(self._normalize_keys(channel.get("api_keys")) or shared_keys for channel in channels)
        return bool(shared_keys)

    def _health_state(self, channel_id: str) -> Dict[str, Any]:
        state = self._health.get(channel_id)
        if state is None:
            state = {
                "successes": 0,
                "failures": 0,
                "fallback_successes": 0,
                "consecutive_failures": 0,
                "cooldown_until": 0.0,
                "last_error_category": "",
                "last_error": "",
                "last_success_at": "",
                "last_failure_at": "",
                "durations": [],
            }
            self._health[channel_id] = state
        return state

    def _cooldown_remaining(self, channel_id: str) -> float:
        state = self._health_state(channel_id)
        return max(0.0, float(state.get("cooldown_until", 0.0) or 0.0) - time.monotonic())

    def _ordered_channels(
        self,
        requires_input_image: bool = False,
        selected_override: str = "",
        allow_fallback: bool = True,
    ) -> List[Dict[str, Any]]:
        channels = self._raw_channels()
        if not channels:
            return []

        if requires_input_image:
            channels = [
                item for item in channels
                if item.get("reference_image_enabled", True) is not False
            ]
            if not channels:
                logger.warning("Linghui has no enabled drawing channel for reference-image requests.")
                return []
            selected = str(
                selected_override
                or self.config.get("reference_image_drawing_channel", "")
                or ""
            ).strip()
        else:
            selected = str(selected_override or self.config.get("active_drawing_channel", "") or "").strip()

        if not selected or selected.lower() in {"auto", "自动"}:
            primary = channels[0]
        else:
            active = next((item for item in channels if item["id"] == selected), None)
            if active is None:
                route_name = "reference-image" if requires_input_image else "active drawing"
                logger.warning(
                    "Linghui %s channel %s is unavailable; using configured order.",
                    route_name,
                    selected,
                )
                primary = channels[0]
            else:
                primary = active

        if not allow_fallback:
            return [primary]

        fallbacks = [
            item for item in channels
            if item["id"] != primary["id"] and item.get("fallback_enabled", True) is not False
        ]
        ordered = [primary] + fallbacks

        available = [item for item in ordered if self._cooldown_remaining(item["id"]) <= 0]
        if available:
            skipped = [item["id"] for item in ordered if item not in available]
            if skipped:
                logger.info("Linghui skipped cooling channels: %s", ", ".join(skipped))
            return available

        earliest = min(ordered, key=lambda item: self._cooldown_remaining(item["id"]))
        logger.warning("All Linghui drawing channels are cooling down; probing %s.", earliest["id"])
        return [earliest]

    def _channel_config(self, channel: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(self.config)
        for key in (
            "interface_mode",
            "base_url",
            "api_keys",
            "model",
            "image_edit_model",
            "text_to_image_model",
            "text_to_image_api_url",
            "text_to_image_api_keys",
            "timeout",
            "use_stream",
            "generic_prefer_images_api",
            "image_edit_transport",
            "protocol",
        ):
            value = channel.get(key)
            if value not in (None, "", []):
                merged[key] = value
        return merged

    @staticmethod
    def _is_success(result: Any) -> bool:
        return isinstance(result, bytes) and bool(result)

    def _should_try_fallback(self, classification: GenerationError) -> bool:
        if classification.category == "safety":
            value = self.config.get("fallback_on_safety_error", False)
            return bool(value) if not isinstance(value, str) else value.strip().lower() in {"1", "true", "yes", "on"}
        return classification.try_next_channel

    def _resolve_model(
        self,
        requested: str,
        channel: Dict[str, Any],
        text_to_image: bool,
        has_input_image: bool = False,
    ) -> str:
        global_default = str(self.config.get("model", "") or "").strip()
        global_t2i = str(self.config.get("text_to_image_model", "") or "").strip()
        channel_default = str(channel.get("model", "") or "").strip()
        channel_image_edit = str(channel.get("image_edit_model", "") or "").strip()
        channel_t2i = str(channel.get("text_to_image_model", "") or "").strip()
        requested = str(requested or "").strip()
        if has_input_image and channel_image_edit and (
            not requested or requested in {global_default, global_t2i}
        ):
            return channel_image_edit
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

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = int(round((len(ordered) - 1) * percentile))
        return float(ordered[min(max(index, 0), len(ordered) - 1)])

    def _record_success(self, channel_id: str, duration: float, fallback: bool) -> None:
        state = self._health_state(channel_id)
        state["successes"] += 1
        if fallback:
            state["fallback_successes"] += 1
        state["consecutive_failures"] = 0
        state["cooldown_until"] = 0.0
        state["last_success_at"] = time.time()
        durations = list(state.get("durations", []))
        durations.append(max(0.0, float(duration)))
        state["durations"] = durations[-200:]

    def _record_failure(self, channel_id: str, classification: GenerationError, error: Any) -> None:
        state = self._health_state(channel_id)
        state["failures"] += 1
        state["consecutive_failures"] += 1
        state["last_failure_at"] = time.time()
        state["last_error_category"] = classification.category
        state["last_error"] = safe_error_summary(error, 240)
        if (
            classification.cooldown_recommended
            and state["consecutive_failures"] >= self._failure_threshold()
        ):
            state["cooldown_until"] = time.monotonic() + self._cooldown_seconds()
            logger.warning(
                "Linghui channel %s entered cooldown for %ss after %s consecutive failures.",
                channel_id,
                self._cooldown_seconds(),
                state["consecutive_failures"],
            )

    def bind_session_store(self, store: Any) -> None:
        """Attach the per-session model/channel override store."""

        self._session_store = store

    def _flag(self, key: str, default: bool = True) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开", "是"}
        return default if value is None else bool(value)

    def _session_channel_override(self, session_id: str) -> str:
        """Channel id pinned to this chat, if session overrides are enabled."""

        store = self._session_store
        if not store or not session_id or not self._flag("enable_session_channel_override", True):
            return ""
        try:
            return str(store.get_channel(session_id) or "").strip()
        except Exception:  # pragma: no cover - defensive, store is best effort
            logger.debug("Linghui session channel override lookup failed.", exc_info=True)
            return ""

    def _session_model_override(self, session_id: str, requested: str) -> str:
        """Model pinned to this chat; an explicit request always wins."""

        store = self._session_store
        if not store or not session_id or not self._flag("enable_session_model_override", True):
            return ""
        requested = str(requested or "").strip()
        global_default = str(self.config.get("model", "") or "").strip()
        global_t2i = str(self.config.get("text_to_image_model", "") or "").strip()
        if requested and requested not in {global_default, global_t2i}:
            return ""
        try:
            return str(store.get_model(session_id) or "").strip()
        except Exception:  # pragma: no cover - defensive
            logger.debug("Linghui session model override lookup failed.", exc_info=True)
            return ""

    def _set_last_metrics(self, metrics: Dict[str, Any]) -> None:
        copied = dict(metrics)
        self._last_metrics = copied
        self._metrics_context.set(copied)

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
        negative_prompt: str | None = None,
        final_instruction: str | None = None,
        preferred_channel_id: str | None = None,
        allow_fallback: bool = True,
        session_id: str = "",
    ) -> bytes | str:
        prepared_prompt = await self.prompt_processor.prepare(prompt)
        prepared_prompt = append_negative_prompt(prepared_prompt, negative_prompt)
        # Hard constraints are appended after translation/optimization and
        # after the optional negative prompt. Every fallback receives this
        # exact same final prompt, so mention privacy cannot disappear when a
        # route changes.
        prepared_prompt = append_final_instruction(prepared_prompt, final_instruction)
        session_key = str(session_id or "").strip()
        channel_override = str(preferred_channel_id or "").strip()
        session_scoped_channel = ""
        if not channel_override:
            session_scoped_channel = self._session_channel_override(session_key)
            channel_override = session_scoped_channel
        session_scoped_model = self._session_model_override(session_key, model)
        if session_scoped_model:
            model = session_scoped_model
        override_meta = {
            "session_id": session_key,
            "session_channel_override": session_scoped_channel,
            "session_model_override": session_scoped_model,
        }
        channels = self._ordered_channels(
            requires_input_image=bool(images),
            selected_override=channel_override,
            allow_fallback=allow_fallback,
        )
        if images and not channels and self._raw_channels():
            return (
                "没有可用于带参考图请求的绘图渠道：请在 Dashboard 的绘图渠道中，"
                "至少为一个已启用渠道打开‘允许带参考图请求’。"
            )
        if not channels:
            client = await self._client_for({"id": "legacy"})
            started = time.monotonic()
            result = await client.call_api(
                images, prepared_prompt, model, legacy_use_power_or_proxy, proxy,
                use_text_to_image_api=use_text_to_image_api,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )
            duration = time.monotonic() - started
            classification = None if self._is_success(result) else classify_generation_error(result)
            metrics = {
                **client.get_last_metrics(),
                "channel_id": "legacy",
                "channel_name": "兼容单接口",
                "model": model,
                "fallback_count": 0,
                **override_meta,
                "has_input_image": bool(images),
                "attempt_chain": [{
                    "channel_id": "legacy",
                    "channel_name": "兼容单接口",
                    "model": model,
                    "duration": duration,
                    "success": self._is_success(result),
                    "error_category": classification.category if classification else "",
                    "error_label": classification.label if classification else "",
                    "error": safe_error_summary(result, 240) if classification else "",
                    "key_attempt": 1,
                }],
                "route_duration": duration,
                "error_category": classification.category if classification else "",
            }
            self._set_last_metrics(metrics)
            return result

        failures: List[str] = []
        attempt_chain: List[Dict[str, Any]] = []
        route_started = time.monotonic()
        final_classification: Optional[GenerationError] = None

        for channel_index, channel in enumerate(channels):
            client = await self._client_for(channel)
            resolved_model = self._resolve_model(
                model,
                channel,
                use_text_to_image_api,
                has_input_image=bool(images),
            )
            key_count = max(1, len(self._channel_keys(channel)))
            key_attempt_limit = min(key_count, 1 + self._key_retry_count())
            channel_result: Any = "渠道未执行"
            classification: Optional[GenerationError] = None
            channel_duration = 0.0

            for key_attempt in range(1, key_attempt_limit + 1):
                attempt_started = time.monotonic()
                try:
                    channel_result = await client.call_api(
                        images, prepared_prompt, resolved_model, legacy_use_power_or_proxy, proxy,
                        use_text_to_image_api=use_text_to_image_api,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Linghui channel %s raised an unexpected error.", channel["id"])
                    channel_result = f"渠道异常：{type(exc).__name__}: {exc}"
                duration = time.monotonic() - attempt_started
                channel_duration += duration

                if self._is_success(channel_result):
                    attempt_chain.append({
                        "channel_id": channel["id"],
                        "channel_name": str(channel.get("name", channel["id"]) or channel["id"]),
                        "model": resolved_model,
                        "duration": duration,
                        "success": True,
                        "error_category": "",
                        "error_label": "",
                        "error": "",
                        "key_attempt": key_attempt,
                    })
                    self._record_success(channel["id"], channel_duration, channel_index > 0)
                    metrics = {
                        **client.get_last_metrics(),
                        "channel_id": channel["id"],
                        "channel_name": str(channel.get("name", channel["id"]) or channel["id"]),
                        "model": resolved_model,
                        "fallback_count": channel_index,
                        **override_meta,
                        "has_input_image": bool(images),
                        "attempt_chain": attempt_chain,
                        "route_duration": time.monotonic() - route_started,
                        "error_category": "",
                    }
                    self._set_last_metrics(metrics)
                    if channel_index:
                        logger.info("Linghui drawing succeeded via fallback channel %s.", channel["id"])
                    return channel_result

                classification = classify_generation_error(channel_result)
                final_classification = classification
                attempt_chain.append({
                    "channel_id": channel["id"],
                    "channel_name": str(channel.get("name", channel["id"]) or channel["id"]),
                    "model": resolved_model,
                    "duration": duration,
                    "success": False,
                    "error_category": classification.category,
                    "error_label": classification.label,
                    "error": safe_error_summary(channel_result, 240),
                    "status_code": classification.status_code,
                    "key_attempt": key_attempt,
                })
                if classification.try_next_key and key_attempt < key_attempt_limit:
                    logger.warning(
                        "Linghui channel %s %s; rotating to the next configured key (%s/%s).",
                        channel["id"],
                        classification.label,
                        key_attempt + 1,
                        key_attempt_limit,
                    )
                    continue
                break

            classification = classification or classify_generation_error(channel_result)
            self._record_failure(channel["id"], classification, channel_result)
            failures.append(f"{channel['id']}[{classification.label}]: {safe_error_summary(channel_result, 180)}")
            metrics = {
                **client.get_last_metrics(),
                "channel_id": channel["id"],
                "channel_name": str(channel.get("name", channel["id"]) or channel["id"]),
                "model": resolved_model,
                "fallback_count": channel_index,
                **override_meta,
                "has_input_image": bool(images),
                "attempt_chain": attempt_chain,
                "route_duration": time.monotonic() - route_started,
                "error_category": classification.category,
                "error_label": classification.label,
            }
            self._set_last_metrics(metrics)
            if not self._should_try_fallback(classification):
                break

        final_label = final_classification.label if final_classification else "未知错误"
        return f"绘图渠道均失败（最终分类：{final_label}）：" + " | ".join(failures)

    def get_last_metrics(self) -> Dict[str, Any]:
        contextual = self._metrics_context.get()
        return dict(contextual if isinstance(contextual, dict) else self._last_metrics)

    def get_health_snapshot(self) -> Dict[str, Any]:
        configured = {item["id"]: item for item in self._raw_channels()}
        rows: List[Dict[str, Any]] = []
        channel_ids = list(configured) + [item for item in self._health if item not in configured]
        for channel_id in channel_ids:
            channel = configured.get(channel_id, {})
            state = self._health_state(channel_id)
            successes = int(state.get("successes", 0) or 0)
            failures = int(state.get("failures", 0) or 0)
            total = successes + failures
            durations = [float(item) for item in state.get("durations", []) if float(item) >= 0]
            remaining = self._cooldown_remaining(channel_id)
            rows.append({
                "id": channel_id,
                "name": str(channel.get("name", "") or channel_id),
                "enabled": bool(channel.get("enabled", True)) if channel else False,
                "successes": successes,
                "failures": failures,
                "success_rate": round(successes / total, 4) if total else 0.0,
                "fallback_successes": int(state.get("fallback_successes", 0) or 0),
                "consecutive_failures": int(state.get("consecutive_failures", 0) or 0),
                "cooling_down": remaining > 0,
                "cooldown_remaining": round(remaining, 1),
                "last_error_category": str(state.get("last_error_category", "") or ""),
                "last_error": str(state.get("last_error", "") or ""),
                "p50_duration": round(self._percentile(durations, 0.50), 3),
                "p95_duration": round(self._percentile(durations, 0.95), 3),
                "average_duration": round(sum(durations) / len(durations), 3) if durations else 0.0,
            })
        return {
            "channels": rows,
            "failure_threshold": self._failure_threshold(),
            "cooldown_seconds": self._cooldown_seconds(),
        }

    async def refresh(self) -> None:
        old_clients = list(self._clients.values())
        self._clients.clear()
        for client in old_clients:
            session = getattr(client, "_session", None)
            if session is not None and not session.closed:
                await session.close()

    async def close(self) -> None:
        await self.refresh()

"""Ordered drawing-channel routing with classified fallback and health state."""

from __future__ import annotations

import asyncio
import copy
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional
from pathlib import Path

from astrbot import logger

from .channel_policy import ChannelDailyQuota
from .channel_health import ChannelHealthStore
from .generation_params import resolve_image_generation_params
from .utils import normalize_model_list
from .api_manager import ApiManager
from .error_classify import GenerationError, classify_generation_error, safe_error_summary
from .prompt_processor import PromptProcessor
from .utils import append_final_instruction, append_negative_prompt
from .preset_options import preset_channels_map, resolve_preset_generation_options
from .configuration_cleanup import REMOVED_CONFIG_KEYS


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
        self._health_store = ChannelHealthStore()
        self._health: Dict[str, Dict[str, Any]] = self._health_store.channels
        self._lock = asyncio.Lock()
        self._session_store: Any = None
        self.daily_quota = ChannelDailyQuota()

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
        return sorted(channels, key=lambda item: -self._bounded_int(item.get("priority", 0), 0, -1000000, 1000000))

    def channels(self) -> List[Dict[str, Any]]:
        return self._raw_channels()

    def _channel_keys(self, channel: Dict[str, Any]) -> List[str]:
        return self._normalize_keys(channel.get("api_keys"))

    def has_available_keys(self) -> bool:
        return any(self._channel_keys(channel) for channel in self._raw_channels())

    def _health_state(self, channel_id: str) -> Dict[str, Any]:
        return self._health_store.state(channel_id)

    def _cooldown_remaining(self, channel_id: str) -> float:
        state = self._health_state(channel_id)
        return max(0.0, float(state.get("cooldown_until", 0.0) or 0.0) - time.monotonic())

    def _ordered_channels(
        self,
        requires_input_image: bool = False,
        selected_override: str = "",
        allow_fallback: bool = True,
        allowed_channel_ids: List[str] | None = None,
    ) -> List[Dict[str, Any]]:
        channels = self._raw_channels()
        if allowed_channel_ids is not None:
            by_id = {channel["id"]: channel for channel in channels}
            channels = [by_id[channel_id] for channel_id in allowed_channel_ids if channel_id in by_id]
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
            return [] if self.daily_quota.snapshot(primary)["quota_exhausted"] else [primary]

        fallbacks = [
            item for item in channels
            if item["id"] != primary["id"] and (allowed_channel_ids is not None or item.get("fallback_enabled", True) is not False)
        ]
        ordered = [primary] + fallbacks
        ordered = [item for item in ordered if not self.daily_quota.snapshot(item)["quota_exhausted"]]
        if not ordered:
            return []

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
        merged = {key: value for key, value in self.config.items() if key not in REMOVED_CONFIG_KEYS}
        for key in (
            "interface_mode",
            "base_url",
            "api_keys",
            "model",
            "image_edit_model",
            "text_to_image_model",
            "timeout",
            "image_edit_transport",
            "protocol",
            "image_resolution",
            "image_aspect_ratio",
            "image_quality",
            "image_output_format",
            "image_frame_count",
            "image_frame_duration_ms",
            "image_complexity",
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
        self, requested: str, channel: Dict[str, Any], text_to_image: bool, has_input_image: bool = False,
    ) -> str:
        requested = str(requested or "").strip()
        if requested:
            return requested
        if has_input_image and channel.get("image_edit_model"):
            return str(channel["image_edit_model"]).strip()
        if text_to_image and channel.get("text_to_image_model"):
            return str(channel["text_to_image_model"]).strip()
        return str(channel.get("model", "") or "").strip()

    def bind_daily_quota(self, data_dir: Path) -> None:
        self.daily_quota = ChannelDailyQuota(Path(data_dir) / "channel_daily_quota.json")
        self._health_store = ChannelHealthStore(Path(data_dir) / "channel_health.json")
        self._health = self._health_store.channels

    def restore_health_history(self, tasks: List[Dict[str, Any]], generation_history: List[Dict[str, Any]]) -> bool:
        """Recover known channel outcomes once, after task and history loading."""
        if not self._health_store.needs_history_seed:
            return False
        from .channel_health_history import reconstruct_channel_health
        return self._health_store.seed_history(reconstruct_channel_health(tasks, generation_history))

    def _models_for(self, requested: str, channel: Dict[str, Any], text_to_image: bool, has_input_image: bool) -> List[str]:
        if requested:
            return [requested]
        models = normalize_model_list(channel.get("models", []))
        specialized = str(channel.get("image_edit_model" if has_input_image else "text_to_image_model", "") or "").strip()
        if models:
            return list(dict.fromkeys(([specialized] if specialized and (has_input_image or text_to_image) else []) + models))
        return [self._resolve_model(requested, channel, text_to_image, has_input_image)]

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
        self._health_store.changed(timestamp=state["last_success_at"])

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
        self._health_store.changed(timestamp=state["last_failure_at"])

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
        if requested:
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

    def _preset_channel_bindings(self, preset_name: str) -> List[str]:
        """Resolve the unified ordered allowlist, including unmigrated old configs."""
        return preset_channels_map(self.config).get(preset_name, []) if preset_name else []

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
        prefer_config_aspect_ratio: bool = False,
        preset_name: str = "",
        routing_profile: str = "",
    ) -> bytes | str:
        self._set_last_metrics({})
        explicit_channel = str(preferred_channel_id or "").strip()
        preset_name = str(preset_name or "").strip()
        routing_profile = str(routing_profile or "").strip()
        try:
            preset_channels = [] if explicit_channel else self._preset_channel_bindings(preset_name)
            profile_channels = []
            if routing_profile == "persona" and not explicit_channel and not preset_channels:
                raw = self.config.get("persona_channel_ids", [])
                if (not isinstance(raw, list) or len(raw) > 8
                        or any(not isinstance(value, str) or not value.strip() for value in raw)):
                    raise ValueError("自拍渠道列表无效，请重新选择主用和备用渠道。")
                profile_channels = [value.strip() for value in raw]
                if len(set(profile_channels)) != len(profile_channels):
                    raise ValueError("自拍渠道列表重复，请重新选择主用和备用渠道。")
            selected_channels = preset_channels or profile_channels
            preset_channel = preset_channels[0] if preset_channels else ""
            image_options = resolve_preset_generation_options(self.config, preset_name)
        except ValueError as exc:
            return str(exc)
        strict_channel = explicit_channel or (selected_channels[0] if selected_channels else "")
        strict_label = f"预设 [{preset_name}] 绑定的渠道" if preset_channel else ("自拍配置的渠道" if profile_channels else "指定渠道")
        required_channels = selected_channels or ([explicit_channel] if explicit_channel else [])
        enabled_ids = {channel["id"] for channel in self._raw_channels()}
        for channel_id in required_channels:
            if channel_id not in enabled_ids:
                return f"{strict_label} {channel_id} 不存在或已被手动禁用，请检查 WebUI 的渠道 ID。"
        if len(selected_channels) == 1:
            allow_fallback = False
        prepared_prompt = await self.prompt_processor.prepare(prompt)
        parameter_prompt = prepared_prompt
        prepared_prompt = append_negative_prompt(prepared_prompt, negative_prompt)
        # Hard constraints are appended after translation/optimization and
        # after the optional negative prompt. Every fallback receives this
        # exact same final prompt, so mention privacy cannot disappear when a
        # route changes.
        prepared_prompt = append_final_instruction(prepared_prompt, final_instruction)
        session_key = str(session_id or "").strip()
        channel_override = strict_channel
        session_scoped_channel = ""
        if not channel_override:
            session_scoped_channel = self._session_channel_override(session_key)
            channel_override = session_scoped_channel
        session_scoped_model = "" if selected_channels else self._session_model_override(session_key, model)
        if session_scoped_model:
            model = session_scoped_model
        override_meta = {
            "session_id": session_key,
            "session_channel_override": session_scoped_channel,
            "session_model_override": session_scoped_model,
            "preset_name": preset_name,
            "routing_profile": routing_profile,
            "profile_channel_ids": list(profile_channels),
            "preset_channel_binding": preset_channel,
            "preset_channel_bindings": list(preset_channels),
            "preset_image_options": dict(image_options),
        }
        for channel_id in required_channels:
            # A service edit during prompt preprocessing must not open the allowlist.
            selected = next((c for c in self._raw_channels() if c["id"] == channel_id), None)
            if selected is None:
                return f"{strict_label} {channel_id} 不存在或已被手动禁用，请检查 WebUI 的渠道 ID。"
            if images and selected.get("reference_image_enabled", True) is False:
                return f"{strict_label} {channel_id} 不允许带参考图请求。"
        channels = self._ordered_channels(
            requires_input_image=bool(images),
            selected_override=channel_override,
            allow_fallback=allow_fallback,
            allowed_channel_ids=selected_channels or None,
        )
        if images and not channels and self._raw_channels() and not any(c.get("reference_image_enabled", True) is not False for c in self._raw_channels()):
            return (
                "没有可用于带参考图请求的绘图渠道：请在 Dashboard 的绘图渠道中，"
                "至少为一个已启用渠道打开‘允许带参考图请求’。"
            )
        if not channels and self.config.get("drawing_channels"):
            if strict_channel and images:
                selected = next(c for c in self._raw_channels() if c["id"] == strict_channel)
                if selected.get("reference_image_enabled", True) is False:
                    return f"{strict_label} {strict_channel} 不允许带参考图请求。"
            target = f"{strict_label} {strict_channel} 的" if strict_channel else "绘图渠道的"
            return f"{target}每日总额度已用完或没有启用的可用渠道；日额度在北京时间次日 00:00 自动恢复。"
        if not channels:
            return "未配置可用绘图渠道，请在绘图渠道中添加并启用渠道。"

        failures: List[str] = []
        attempt_chain: List[Dict[str, Any]] = []
        route_started = time.monotonic()
        final_classification: Optional[GenerationError] = None
        safety_stopped = False

        for channel_index, channel in enumerate(channels):
            client = await self._client_for(channel)
            key_count = max(1, len(self._channel_keys(channel)))
            key_attempt_limit = min(key_count, 1 + self._key_retry_count())
            channel_result: Any = "渠道未执行"
            classification: Optional[GenerationError] = None
            channel_duration = 0.0

            try:
                reservation = self.daily_quota.reserve(channel)
            except Exception:
                logger.exception("Linghui could not persist channel daily quota.")
                return "渠道日额度保存失败，本次未调用上游；请检查插件数据目录后重试。"
            if reservation is None:
                failures.append(f"{channel['id']}：每日总额度已用完（北京时间次日 00:00 恢复）")
                continue
            succeeded = False
            params = resolve_image_generation_params(parameter_prompt,
                channel.get("image_resolution") or "1K",
                channel.get("image_aspect_ratio") or "1:1",
                resolution=resolution, aspect_ratio=aspect_ratio)
            effective_ratio = params["aspect_ratio"] if channel.get("image_aspect_ratio") or prefer_config_aspect_ratio else aspect_ratio
            effective_resolution = params["resolution"] if channel.get("image_resolution") or prefer_config_aspect_ratio else resolution
            try:
                for resolved_model in self._models_for(model, channel, use_text_to_image_api, bool(images)):
                    for key_attempt in range(1, key_attempt_limit + 1):
                        attempt_started = time.monotonic()
                        try:
                            channel_result = await client.call_api(
                                images, prepared_prompt, resolved_model, legacy_use_power_or_proxy, proxy,
                                use_text_to_image_api=use_text_to_image_api,
                                aspect_ratio=effective_ratio,
                                resolution=effective_resolution,
                                **({"image_options": image_options} if image_options else {}),
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
                            succeeded = True
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

                    if classification and not self._should_try_fallback(classification):
                        break
            finally:
                if not succeeded:
                    self.daily_quota.refund(reservation)

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
            if classification.category == "safety" and not self._should_try_fallback(classification):
                safety_stopped = True
                metrics["fallback_stop_reason"] = "safety"
                metrics["fallback_stopped"] = True
            self._set_last_metrics(metrics)
            if not self._should_try_fallback(classification):
                break

        final_label = final_classification.label if final_classification else "未知错误"
        stop_notice = "（内容审核拦截，未继续切换渠道）" if safety_stopped else ""
        return f"绘图渠道均失败（最终分类：{final_label}）：" + " | ".join(failures) + stop_notice

    def get_last_metrics(self) -> Dict[str, Any]:
        contextual = self._metrics_context.get()
        return dict(contextual if isinstance(contextual, dict) else self._last_metrics)

    def get_health_snapshot(self) -> Dict[str, Any]:
        configured = {str(item.get("id") or f"channel_{index + 1}"): item for index, item in enumerate(self.config.get("drawing_channels", []) or []) if isinstance(item, dict)}
        rows: List[Dict[str, Any]] = []
        channel_ids = list(configured)
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
                **self.daily_quota.snapshot({**channel, "id": channel_id}),
                "priority": channel.get("priority", 0),
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
                "duration_sample_count": len(durations),
            })
        return {
            **self._health_store.metadata(),
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
        self._health_store.save()
        await self.refresh()

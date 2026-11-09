"""Bound presets stay on their configured channel, including GIF requests."""

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from test_linghui_features import _load_module


class PresetRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_module("channel_router")
        self.calls = []
        self.outcomes = {}
        self.config = {
            "model": "default",
            "text_to_image_model": "text-default",
            "active_drawing_channel": "a",
            "drawing_channels": [
                {"id": "a", "base_url": "a", "model": "still-model", "api_keys": "a-key"},
                {"id": "b", "base_url": "b", "model": "gif-model", "api_keys": "b-key"},
            ],
            "preset_channel_bindings": [{"preset": "动图", "channel_id": "b"}],
        }
        self.router = self.module.DrawingChannelRouter(self.config)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router.bind_daily_quota(Path(self.temp.name))
        self.addAsyncCleanup(self.router.close)

        async def client_for(channel):
            async def generate(images, prompt, model, *args, **kwargs):
                self.calls.append((channel["id"], model, prompt))
                await asyncio.sleep(0)
                return self.outcomes.get((channel["id"], model), b"generated")

            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})

        self.router._client_for = client_for
        self.router.bind_session_store(types.SimpleNamespace(
            get_channel=lambda session: "a",
            get_model=lambda session: "session-still-model",
        ))

    async def test_bound_preset_overrides_session_channel_and_model(self):
        result = await self.router.call_api([], "cat blinks", "text-default",
            use_text_to_image_api=True, session_id="chat", preset_name="动图")
        self.assertEqual(result, b"generated")
        self.assertEqual(self.calls, [("b", "gif-model", "cat blinks")])
        metrics = self.router.get_last_metrics()
        self.assertEqual(metrics["preset_channel_binding"], "b")
        self.assertEqual(metrics["session_channel_override"], "")
        self.assertEqual(metrics["session_model_override"], "")

    async def test_explicit_channel_wins_over_preset_binding(self):
        self.router.bind_session_store(None)
        await self.router.call_api([], "cat", "default", preset_name="动图",
            preferred_channel_id="a", allow_fallback=False)
        self.assertEqual(self.calls, [("a", "still-model", "cat")])
        self.assertEqual(self.router.get_last_metrics()["preset_channel_binding"], "")

    async def test_explicit_model_is_respected_with_binding(self):
        await self.router.call_api([], "cat", "explicit-gif-model", preset_name="动图", session_id="chat")
        self.assertEqual(self.calls[0][1], "explicit-gif-model")

    async def test_unbound_preset_keeps_session_overrides_and_channel_fallback(self):
        self.outcomes[("a", "session-still-model")] = "API Error 503"
        await self.router.call_api([], "cat", "default", preset_name="静态", session_id="chat")
        self.assertEqual([call[:2] for call in self.calls], [
            ("a", "session-still-model"), ("b", "session-still-model"),
        ])
        self.assertEqual(self.router.get_last_metrics()["session_channel_override"], "a")

    async def test_bound_failure_never_switches_channel_and_refunds_channel_quota(self):
        self.outcomes[("b", "gif-model")] = "API Error 503"
        result = await self.router.call_api([], "cat", "default", preset_name="动图")
        self.assertIn("均失败", result)
        self.assertEqual([call[0] for call in self.calls], ["b"])
        self.assertEqual(self.router.daily_quota.snapshot({"id": "b"})["daily_used"], 0)

    async def test_bound_channel_can_try_its_configured_models(self):
        self.config["drawing_channels"][1]["models"] = ["first-gif", "second-gif"]
        self.outcomes[("b", "first-gif")] = "API Error 404 model_not_found"
        await self.router.call_api([], "cat", "default", preset_name="动图")
        self.assertEqual([call[:2] for call in self.calls], [("b", "first-gif"), ("b", "second-gif")])

    async def test_missing_disabled_reference_disabled_and_exhausted_bindings_fail_closed(self):
        channel = self.config["drawing_channels"][1]
        for change, images, expected in [
            ({"id": "renamed"}, [], "不存在"),
            ({"id": "b", "enabled": False}, [], "禁用"),
            ({"enabled": True, "reference_image_enabled": False}, [b"reference"], "不允许"),
            ({"reference_image_enabled": True, "daily_limit": 0}, [], "每日总额度"),
        ]:
            with self.subTest(change=change):
                channel.update(change)
                self.router._set_last_metrics({"channel_id": "previous"})
                result = await self.router.call_api(images, "cat", "default", preset_name="动图")
                self.assertIn(expected, result)
                self.assertEqual(self.router.get_last_metrics(), {})
                self.assertEqual(self.calls, [])

    async def test_binding_does_not_use_legacy_when_channels_are_empty(self):
        self.config["drawing_channels"] = []
        result = await self.router.call_api([], "cat", "default", preset_name="动图")
        self.assertIn("不存在", result)
        self.assertEqual(self.calls, [])

    async def test_invalid_binding_does_not_silently_generate_on_default(self):
        for entries in [
            {},
            [{"preset": "动图", "channel_id": None}],
            [{"preset": "动图", "channel_id": ""}],
            [{"preset": "动图", "channel_id": "a"}, {"preset": "动图", "channel_id": "b"}],
        ]:
            with self.subTest(entries=entries):
                self.config["preset_channel_bindings"] = entries
                result = await self.router.call_api([], "cat", "default", preset_name="动图")
                self.assertIsInstance(result, str)
                self.assertIn("预设", result)
                self.assertEqual(self.calls, [])

    async def test_channel_removed_during_prompt_processing_fails_closed(self):
        async def remove_channel(prompt):
            self.config["drawing_channels"] = self.config["drawing_channels"][:1]
            return prompt

        self.router.prompt_processor.prepare = remove_channel
        result = await self.router.call_api([], "cat", "default", preset_name="动图")
        self.assertIn("不存在", result)
        self.assertEqual(self.calls, [])

    async def test_concurrent_requests_keep_each_preset_and_route_metrics(self):
        self.router.bind_session_store(None)
        self.config["preset_channel_bindings"].append({"preset": "照片", "channel_id": "a"})

        async def generate(preset):
            result = await self.router.call_api([], preset, "default", preset_name=preset)
            return result, self.router.get_last_metrics()

        first, second = await asyncio.gather(generate("动图"), generate("照片"))
        self.assertEqual(first[1]["channel_id"], "b")
        self.assertEqual(first[1]["preset_name"], "动图")
        self.assertEqual(second[1]["channel_id"], "a")
        self.assertEqual(second[1]["preset_name"], "照片")

    async def test_binding_is_read_again_after_configuration_changes(self):
        self.router.bind_session_store(None)
        await self.router.call_api([], "cat", "default", preset_name="动图")
        self.config["preset_channel_bindings"][0]["channel_id"] = "a"
        await self.router.call_api([], "cat", "default", preset_name="动图")
        self.assertEqual([call[0] for call in self.calls], ["b", "a"])

    def test_channel_animation_options_override_globals_including_auto(self):
        self.config.update(image_output_format="gif", image_frame_count=16,
            image_frame_duration_ms=400, image_complexity="high")
        merged = self.router._channel_config({"image_output_format": "auto", "image_frame_count": 9,
            "image_frame_duration_ms": 200, "image_complexity": "auto"})
        self.assertEqual([merged[key] for key in (
            "image_output_format", "image_frame_count", "image_frame_duration_ms", "image_complexity",
        )], ["auto", 9, 200, "auto"])


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for channel scheduling, budgets and server migration."""

import asyncio
import copy
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from test_linghui_features import _load_module


class DailyQuotaTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_module("channel_policy")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "quota.json"
        self.now = datetime(2026, 10, 22, 23, 59, tzinfo=timezone(timedelta(hours=8)))
        self.quota = self.module.ChannelDailyQuota(self.path, lambda: self.now)
        self.channel = {"id": "a", "daily_limit": 2}

    def test_restart_keeps_usage_and_new_day_recovers(self):
        self.quota.reserve(self.channel)
        token = self.quota.reserve(self.channel)
        self.assertIsNone(self.quota.reserve(self.channel))
        restored = self.module.ChannelDailyQuota(self.path, lambda: self.now)
        self.assertEqual(restored.snapshot(self.channel)["daily_used"], 2)
        self.now += timedelta(minutes=2)
        self.assertEqual(restored.snapshot(self.channel)["daily_remaining"], 2)
        restored.reserve(self.channel)
        restored.refund(token)  # Yesterday's failed task cannot erase today's spend.
        self.assertEqual(restored.snapshot(self.channel)["daily_used"], 1)

    def test_failed_write_does_not_call_it_reserved(self):
        with patch.object(self.quota, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.quota.reserve(self.channel)
        self.assertEqual(self.quota.snapshot(self.channel)["daily_used"], 0)

    def test_corrupt_state_is_not_silently_reset(self):
        self.path.write_text('{"day":"2026-10-22","used":{"a":-1}}')
        with self.assertRaises(ValueError):
            self.module.ChannelDailyQuota(self.path)

    def test_validation_rejects_bad_limits_models_and_dimensions(self):
        for raw in ({"daily_limit": -2}, {"priority": True}, {"models": [5]},
                    {"image_resolution": "8K"}, {"image_aspect_ratio": "7:7"}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.module.normalize_policy(raw)
        self.assertEqual(self.module.normalize_policy({"models": "one\ntwo\none"})["models"], ["one", "two"])

    def test_channel_option_does_not_change_ordinary_prompts(self):
        extract = self.module.extract_channel_option
        self.assertEqual(extract("--渠道 test_one a blue cat"), ("a blue cat", "test_one"))
        self.assertEqual(extract("--channel=test-two a blue cat"), ("a blue cat", "test-two"))
        self.assertEqual(extract("a cat --channel abc"), ("a cat --channel abc", ""))
        self.assertEqual(extract("--channels of water"), ("--channels of water", ""))
        with self.assertRaises(ValueError):
            extract("--渠道")


class RouterPolicyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_module("channel_router")
        self.old = self.module.ApiManager
        self.calls = []
        self.outcomes = {}
        self.gate = None
        owner = self

        class Client:
            _normalize_keys = staticmethod(owner.old._normalize_keys)
            _session = None

            def __init__(self, config):
                self.config = config

            async def call_api(self, images, prompt, model, *args, **kwargs):
                name = self.config["base_url"]
                owner.calls.append((name, model, kwargs, prompt))
                if owner.gate:
                    await owner.gate.wait()
                return owner.outcomes.get((name, model), b"generated")

            def get_last_metrics(self):
                return {}

        self.module.ApiManager = Client
        self.addCleanup(setattr, self.module, "ApiManager", self.old)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"model": "default", "text_to_image_model": "default",
                       "drawing_channels": [{"id": "a", "base_url": "a", "api_keys": "one"},
                                            {"id": "b", "base_url": "b", "api_keys": "two"}]}

    def router(self):
        router = self.module.DrawingChannelRouter(self.config)
        router.bind_daily_quota(Path(self.temp.name))
        self.addAsyncCleanup(router.close)
        return router

    async def test_priority_then_stable_order_and_explicit_primary(self):
        self.config["drawing_channels"] += [{"id": "c", "base_url": "c", "api_keys": "three", "priority": 8}]
        self.config["drawing_channels"][1]["priority"] = 8
        router = self.router()
        self.assertEqual([c["id"] for c in router._ordered_channels()], ["b", "c", "a"])
        self.config["active_drawing_channel"] = "a"
        self.assertEqual([c["id"] for c in router._ordered_channels()], ["a", "b", "c"])
        await router.call_api([], "cat", "default", preferred_channel_id="c", allow_fallback=False)
        self.assertEqual(self.calls[0][0], "c")

    async def test_models_are_exhausted_before_switching_channel(self):
        self.config["drawing_channels"][0]["models"] = ["first", "second"]
        self.outcomes = {("a", "first"): "API Error 404 model_not_found", ("a", "second"): "API Error 503"}
        router = self.router()
        self.assertEqual(await router.call_api([], "cat", "default"), b"generated")
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("a", "first"), ("a", "second"), ("b", "default")])
        self.assertEqual(router.daily_quota.snapshot({"id": "a"})["daily_used"], 0)
        self.assertEqual(router.daily_quota.snapshot({"id": "b"})["daily_used"], 1)
        self.assertEqual(len(router.get_last_metrics()["attempt_chain"]), 3)

    async def test_specialized_and_explicit_models_and_safety_policy(self):
        self.config["drawing_channels"][0].update({"image_edit_model": "edit", "models": ["one", "two"]})
        router = self.router()
        self.assertEqual(router._models_for("default", router.channels()[0], False, True), ["edit", "one", "two"])
        self.outcomes[("a", "edit")] = "content_filter safety"
        await router.call_api([b"reference"], "cat", "default")
        self.assertEqual([(c[0], c[1]) for c in self.calls], [("a", "edit")])
        self.calls.clear()
        await router.call_api([], "cat", "user-model")
        self.assertEqual(self.calls[0][1], "user-model")

    async def test_budget_is_shared_and_reserved_before_concurrent_dispatch(self):
        self.config["drawing_channels"][0]["daily_limit"] = 1
        self.config["drawing_channels"][1]["daily_limit"] = 0
        router = self.router()
        self.gate = asyncio.Event()
        first = asyncio.create_task(router.call_api([], "cat", "default", session_id="one"))
        await asyncio.sleep(0)
        second = await router.call_api([], "cat", "default", session_id="two")
        self.assertIn("每日总额度", second)
        self.assertEqual(len(self.calls), 1)
        self.gate.set()
        self.assertEqual(await first, b"generated")
        self.assertTrue(router.get_health_snapshot()["channels"][0]["quota_exhausted"])
        self.assertTrue(self.config["drawing_channels"][0].get("enabled", True))
        restored = self.router()
        self.assertIn("每日总额度", await restored.call_api([], "cat", "default"))

    async def test_cancel_refunds_and_exhausted_primary_routes_to_backup(self):
        self.config["drawing_channels"][0]["daily_limit"] = 1
        router = self.router()
        self.gate = asyncio.Event()
        task = asyncio.create_task(router.call_api([], "cat", "default"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(router.daily_quota.snapshot(router.channels()[0])["daily_used"], 0)
        self.gate = None
        await router.call_api([], "cat", "default")
        await router.call_api([], "cat", "default")
        self.assertEqual(self.calls[-1][0], "b")

    async def test_target_never_switches_channel_or_uses_legacy(self):
        router = self.router()
        self.outcomes[("a", "default")] = "API Error 503"
        failure = await router.call_api([], "cat", "default", preferred_channel_id="a", allow_fallback=False)
        self.assertIn("均失败", failure)
        self.assertEqual([c[0] for c in self.calls], ["a"])
        self.calls.clear()
        self.assertIn("不存在", await router.call_api([], "cat", "default", preferred_channel_id="missing"))
        self.config["drawing_channels"][0]["reference_image_enabled"] = False
        self.assertIn("不允许", await router.call_api([b"ref"], "cat", "default", preferred_channel_id="a", allow_fallback=False))
        for c in self.config["drawing_channels"]:
            c["enabled"] = False
        self.assertIn("可用渠道", await router.call_api([], "cat", "default"))
        self.assertEqual(self.calls, [])

    async def test_channel_sizes_and_prompt_explicit_precedence(self):
        self.config["drawing_channels"][0].update({"image_resolution": "2K", "image_aspect_ratio": "16:9"})
        self.config["drawing_channels"][1].update({"image_resolution": "1K", "image_aspect_ratio": "1:1"})
        self.outcomes[("a", "default")] = "API Error 503"
        router = self.router()
        await router.call_api([b"ref"], "cat", "default")
        self.assertEqual([(c[2]["resolution"], c[2]["aspect_ratio"]) for c in self.calls], [("2K", "16:9"), ("1K", "1:1")])
        self.calls.clear()
        await router.call_api([], "cat 4K 9:16", "default", resolution="1K")
        self.assertEqual([(c[2]["resolution"], c[2]["aspect_ratio"]) for c in self.calls], [("1K", "9:16"), ("1K", "9:16")])


class ChannelMigrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_module("dashboard_api", with_quart=True)
        self.config = {"drawing_channels": [{"id": "a", "base_url": "a", "api_keys": "source-key",
                                             "models": ["first", "second"], "daily_limit": 5, "priority": 9,
                                             "image_resolution": "2K", "image_aspect_ratio": "16:9"}],
                       "active_drawing_channel": "a", "global_total_limit": 100}
        self.plugin = types.SimpleNamespace(conf=self.config, _save_config=Mock(),
                                            api_mgr=types.SimpleNamespace(refresh=AsyncMock(),
                                                _channel_keys=lambda c: [c["api_keys"]] if c.get("api_keys") else ["shared-key"]))
        self.api = self.module.LinghuiDashboardApi(self.plugin)

    async def import_document(self, document):
        self.api._json_body = AsyncMock(return_value={"document": document})
        with patch.object(self.module, "jsonify", side_effect=lambda p: p):
            return await self.api.channels_import()

    async def test_roundtrip_keys_and_policy_without_changing_other_settings(self):
        document = self.api._export_channels_document()
        self.assertEqual(document["channels"][0]["api_keys"], "source-key")
        self.config["drawing_channels"] = [{"id": "a", "api_keys": "destination-key"}]
        response = await self.import_document(document)
        self.assertTrue(response["success"])
        self.assertEqual(self.config["drawing_channels"][0]["api_keys"], "source-key")
        self.assertEqual(self.config["drawing_channels"][0]["models"], ["first", "second"])
        self.assertEqual(self.config["drawing_channels"][0]["daily_limit"], 5)
        self.assertEqual(self.config["global_total_limit"], 100)
        self.assertNotIn("api_keys", self.api._public_channel(self.config["drawing_channels"][0], 0))

    async def test_invalid_imports_are_atomic_and_empty_keys_replace(self):
        document = self.api._export_channels_document()
        before = copy.deepcopy(self.config)
        for change in (lambda d: d["channels"].append(copy.deepcopy(d["channels"][0])),
                       lambda d: d["channels"][0].update(daily_limit=-2),
                       lambda d: d.update(active_drawing_channel="unknown"),
                       lambda d: d.update(format_version=2)):
            invalid = copy.deepcopy(document)
            change(invalid)
            response, status = await self.import_document(invalid)
            self.assertEqual(status, 400)
            self.assertFalse(response["success"])
            self.assertEqual(self.config, before)
        document["channels"][0]["api_keys"] = ""
        self.assertTrue((await self.import_document(document))["success"])
        self.assertEqual(self.config["drawing_channels"][0]["api_keys"], "")

    async def test_save_failure_rolls_back_and_shared_keys_are_exported(self):
        self.config["drawing_channels"][0]["api_keys"] = ""
        document = self.api._export_channels_document()
        self.assertEqual(document["channels"][0]["api_keys"], "shared-key")
        before = copy.deepcopy(self.config)
        self.plugin._save_config.side_effect = [OSError("disk full"), None]
        response, status = await self.import_document(document)
        self.assertEqual(status, 500)
        self.assertEqual(self.config, before)


if __name__ == "__main__":
    unittest.main()

"""Selfies select their own ordered channels without leaking into other requests."""

import ast
import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import types
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import unittest
from unittest.mock import AsyncMock, Mock

from test_linghui_features import ROOT, _load_module


class PersonaRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.outcomes = {}
        self.config = {
            "active_drawing_channel": "m", "reference_image_drawing_channel": "m",
            "persona_channel_ids": ["orb", "miku"],
            "drawing_channels": [
                {"id": "m", "model": "ordinary", "api_keys": "m-key", "priority": 999},
                {"id": "miku", "model": "best-backup", "api_keys": "miku-key", "fallback_enabled": False},
                {"id": "orb", "model": "best-primary", "api_keys": "orb-key"},
            ],
        }
        self.router = _load_module("channel_router").DrawingChannelRouter(self.config)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.router.bind_daily_quota(Path(self.temporary.name))
        self.addAsyncCleanup(self.router.close)
        self.router.bind_session_store(types.SimpleNamespace(get_channel=lambda _: "m", get_model=lambda _: "session-model"))

        async def client_for(channel):
            async def generate(images, prompt, model, *args, **kwargs):
                self.calls.append((channel["id"], model))
                await asyncio.sleep(0)
                return self.outcomes.get(channel["id"], b"generated")
            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})
        self.router._client_for = client_for

    async def test_selfie_uses_chosen_order_models_and_selected_backup_even_when_general_fallback_disabled(self):
        self.outcomes["orb"] = "HTTP 524 timeout"
        result = await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona", session_id="chat")
        self.assertEqual(result, b"generated")
        self.assertEqual(self.calls, [("orb", "best-primary"), ("miku", "best-backup")])
        metrics = self.router.get_last_metrics()
        self.assertEqual(metrics["routing_profile"], "persona")
        self.assertEqual(metrics["profile_channel_ids"], ["orb", "miku"])
        self.assertEqual(metrics["session_model_override"], "")

    async def test_selfie_never_falls_back_to_unselected_services(self):
        self.outcomes.update(orb="HTTP 429 quota exceeded", miku="HTTP 503 unavailable")
        result = await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona")
        self.assertIn("均失败", result)
        self.assertEqual([row[0] for row in self.calls], ["orb", "miku"])

    async def test_ordinary_edit_keeps_normal_session_route(self):
        await self.router.call_api([b"reference"], "edit", "", session_id="chat")
        self.assertEqual(self.calls, [("m", "session-model")])

    async def test_empty_selfie_selection_follows_normal_route(self):
        self.config["persona_channel_ids"] = []
        await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona", session_id="chat")
        self.assertEqual(self.calls, [("m", "session-model")])

    async def test_explicit_channel_and_preset_selection_take_priority(self):
        await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona",
                                   preferred_channel_id="m", allow_fallback=False)
        self.assertEqual(self.calls, [("m", "ordinary")])
        self.calls.clear()
        self.config.update(preset_config_version=1,
            preset_generation_options=[{"preset": "portrait", "channel_ids": ["miku"]}])
        await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona", preset_name="portrait")
        self.assertEqual(self.calls, [("miku", "best-backup")])

    async def test_explicit_model_remains_respected(self):
        await self.router.call_api([b"reference"], "portrait", "explicit-model", routing_profile="persona")
        self.assertEqual(self.calls, [("orb", "explicit-model")])

    async def test_invalid_disabled_missing_or_reference_disabled_selection_fails_closed(self):
        for selection in ("orb", ["orb", "orb"], [None], ["orb", "missing"]):
            with self.subTest(selection=selection):
                self.config["persona_channel_ids"] = selection
                result = await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona")
                self.assertIsInstance(result, str)
                self.assertEqual(self.calls, [])
        self.config["persona_channel_ids"] = ["orb", "miku"]
        for field in ("enabled", "reference_image_enabled"):
            with self.subTest(field=field):
                self.config["drawing_channels"][1][field] = False
                result = await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona")
                self.assertIsInstance(result, str)
                self.assertEqual(self.calls, [])
                self.config["drawing_channels"][1].pop(field)

    async def test_exhausted_primary_uses_selected_backup(self):
        self.config["drawing_channels"][2]["daily_limit"] = 0
        await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona")
        self.assertEqual(self.calls, [("miku", "best-backup")])

    async def test_safety_stop_does_not_switch_selfie_channels(self):
        self.outcomes["orb"] = "HTTP 400 content_policy_violation"
        result = await self.router.call_api([b"reference"], "portrait", "", routing_profile="persona")
        self.assertEqual(self.calls, [("orb", "best-primary")])
        self.assertTrue(result.endswith("（内容审核拦截，未继续切换渠道）"))
        self.assertEqual(self.router.get_last_metrics()["fallback_stop_reason"], "safety")

    async def test_concurrent_selfie_and_ordinary_routes_keep_separate_metrics(self):
        async def generate(profile):
            await self.router.call_api([b"reference"], "portrait", "", routing_profile=profile)
            return self.router.get_last_metrics()
        selfie, ordinary = await asyncio.gather(generate("persona"), generate(""))
        self.assertEqual((selfie["channel_id"], ordinary["channel_id"]), ("orb", "m"))

    async def test_retired_single_service_is_never_used_or_inherited(self):
        self.config.update(base_url="https://obsolete.example", api_keys="obsolete-key", model="old-model")
        merged = self.router._channel_config({"id": "empty"})
        self.assertNotIn("base_url", merged)
        self.assertNotIn("api_keys", merged)
        self.assertNotIn("model", merged)
        self.config.update(drawing_channels=[], persona_channel_ids=[])
        result = await self.router.call_api([], "portrait", "")
        self.assertIn("未配置可用绘图渠道", result)
        self.assertFalse(self.router.has_available_keys())
        self.assertEqual(self.calls, [])


class PersonaTaskProfileTest(unittest.IsolatedAsyncioTestCase):
    def harness(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
        methods = [node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name in {"_begin_generation_task", "_call_generation_api_task", "_run_batch_image_to_image",
                                     "on_persona_photo_cmd", "_register_pending_generation",
                                     "_complete_pending_generation", "_get_pending_generation_count"}]
        for method in methods:
            method.decorator_list = []
        namespace = {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
                     "AstrMessageEvent": Any, "asyncio": asyncio, "norm_id": str,
                     "Awaitable": Awaitable, "Callable": Callable, "datetime": datetime,
                     "logger": Mock(), "Image": types.SimpleNamespace(fromBytes=lambda data: data), "Plain": str,
                     "append_negative_prompt": _load_module("utils").append_negative_prompt,
                     "keep_unspecified_suggestions": lambda scene, *args, **kwargs: scene,
                     "has_outfit_request": lambda _: False, "has_expression_request": lambda _: False,
                     "QuotaDeniedError": _load_module("quota").QuotaDeniedError}
        compiled = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
        exec(compile(ast.fix_missing_locations(compiled), str(ROOT / "main.py"), "exec"), namespace)
        harness = namespace["Harness"]()
        harness._event_identity_labels = lambda _: ("", "")
        harness._event_request_id = lambda _: "message"
        harness.task_mgr = types.SimpleNamespace(begin_task=AsyncMock(return_value=({"id": "task"}, False)),
            attach_runtime_task=AsyncMock(), detach_runtime_task=AsyncMock())
        harness.api_mgr = types.SimpleNamespace(call_api=AsyncMock(return_value=b"generated"))
        harness.img_mgr = types.SimpleNamespace(proxy=None)
        return harness

    async def test_task_save_and_runtime_call_retain_selfie_profile(self):
        plugin = self.harness()
        event = types.SimpleNamespace(unified_msg_origin="session", get_sender_id=lambda: "10001", get_group_id=lambda: "20001")
        await plugin._begin_generation_task(event, images=[b"reference"], prompt="portrait", model="",
                                            preset_name="人设-日常", task_type="人设拍照", routing_profile="persona")
        self.assertEqual(plugin.task_mgr.begin_task.call_args.kwargs["request"]["routing_profile"], "persona")
        result = await plugin._call_generation_api_task("task", [b"reference"], "portrait", "", routing_profile="persona")
        self.assertEqual(result, b"generated")
        self.assertEqual(plugin.api_mgr.call_api.call_args.kwargs["routing_profile"], "persona")

    async def test_batch_selfies_store_each_photo_profile_and_finalize_each_delivery(self):
        plugin = self.harness()
        plugin.conf = {"enable_preset_ref_images": False, "batch_retries": 0, "batch_concurrency": 2}
        plugin._effective_identity = lambda event, uid, gid: (uid, gid)
        plugin._charge_quota = AsyncMock(return_value=True)
        plugin._make_batch_guard = lambda: _load_module("batch_policy").BatchFailureGuard("skip", 3)
        plugin._finalize_batch_guard = AsyncMock(return_value="")
        plugin._snapshot_generation_route_metrics = lambda: {"channel_id": "orb", "model": "best"}
        plugin._prepare_send_image_bytes = AsyncMock(side_effect=lambda value: value)
        plugin._record_generation_result = AsyncMock(return_value={"id": "record"})
        plugin.task_mgr.mark_generated = AsyncMock()
        plugin._maybe_stage_after_grace = AsyncMock(return_value=False)
        plugin._send_generated_result = AsyncMock(return_value="sent")
        plugin._finalize_generated_delivery = AsyncMock()
        plugin._complete_pending_generation = AsyncMock()
        plugin._send_task_notice = AsyncMock()
        completion = AsyncMock()
        event = types.SimpleNamespace(unified_msg_origin="session", chain_result=lambda chain: chain,
            get_sender_id=lambda: "10001", get_group_id=lambda: "20001")
        result = await plugin._run_batch_image_to_image(event, [b"reference"], "portrait", "人设-日常", {},
            "10001", "20001", 2, hide_text=True, routing_profile="persona", photo_completion=completion)
        self.assertEqual((result["success"], result["fail"]), (2, 0))
        self.assertEqual(plugin.task_mgr.begin_task.await_count, 2)
        for call in plugin.task_mgr.begin_task.call_args_list:
            self.assertEqual(call.kwargs["request"]["routing_profile"], "persona")
        self.assertEqual(plugin._finalize_generated_delivery.await_count, 2)
        self.assertEqual(completion.await_count, 2)

    async def test_direct_selfie_preserves_existing_same_session_pending_generation(self):
        plugin = self.harness()
        plugin.conf = {"persona_name": "Test"}
        plugin._persona_mode = True
        plugin.context = types.SimpleNamespace()
        plugin._pending_generation_tasks = {"session": 2, "other-session": 4}
        plugin._pending_generation_lock = asyncio.Lock()
        plugin._load_persona_ref_images = AsyncMock(return_value=[b"reference"])
        plugin.img_mgr.extract_images_from_event = AsyncMock(return_value=[])
        plugin._get_bot_id = lambda _: "90001"
        plugin.ctx_mgr = types.SimpleNamespace(get_recent_messages=AsyncMock(return_value=[]))
        plugin._match_persona_scene = lambda *args, **kwargs: ("日常", "sunny window")
        plugin._build_persona_prompt = lambda scene: "portrait " + scene
        plugin._build_current_time_persona_hint = AsyncMock(return_value="daytime")
        plugin._check_command_image_cooldown = lambda *args: (False, 0)
        plugin._check_quota = AsyncMock(return_value={"allowed": True})
        plugin._update_command_image_cooldown = Mock()
        plugin._plan_persona_photos = AsyncMock(return_value=("plan", ["portrait at sunny window"]))
        plugin._get_persona_negative_prompt = lambda: ""
        plugin.persona_planner = types.SimpleNamespace(complete_photo=AsyncMock(), release=AsyncMock())

        async def consume_own_pending_task(**kwargs):
            self.assertEqual(await plugin._get_pending_generation_count("session"), 3)
            self.assertEqual(kwargs["routing_profile"], "persona")
            await plugin._complete_pending_generation("session", 1)
            return True, ""

        plugin._run_background_task = AsyncMock(side_effect=consume_own_pending_task)
        event = types.SimpleNamespace(message_str="人设拍照 日常", unified_msg_origin="session",
            chain_result=lambda chain: chain, get_sender_id=lambda: "10001", get_group_id=lambda: "20001")
        feedback = [result async for result in plugin.on_persona_photo_cmd(event)]

        self.assertEqual(len(feedback), 1)
        plugin._run_background_task.assert_awaited_once()
        self.assertEqual(await plugin._get_pending_generation_count("session"), 2)
        self.assertEqual(await plugin._get_pending_generation_count("other-session"), 4)
        plugin.persona_planner.complete_photo.assert_awaited_once_with("plan", 0, True)
        plugin.persona_planner.release.assert_awaited_once_with("plan")

    def test_all_selfie_entrypoints_opt_into_profile_and_retired_commands_are_removed(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
        methods = {node.name: node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("on_add_key", "on_list_keys", "on_delete_key", "on_switch_mode"):
            self.assertNotIn(name, methods)
        for name, expected in (("persona_photo_tool", 2), ("on_persona_photo_cmd", 1)):
            calls = [node for node in ast.walk(methods[name]) if isinstance(node, ast.Call)
                     and any(keyword.arg == "routing_profile" and isinstance(keyword.value, ast.Constant)
                             and keyword.value.value == "persona" for keyword in node.keywords)]
            self.assertEqual(len(calls), expected, name)


if __name__ == "__main__":
    unittest.main()

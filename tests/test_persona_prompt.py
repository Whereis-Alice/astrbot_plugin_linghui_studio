"""Verify generic persona directions, fallback precedence and request quality."""
import ast
from pathlib import Path
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch
from typing import Dict, List, Optional

import test_api_transport_fallback as transport_test
import test_dashboard_channel_modes as dashboard_test
from test_api_transport_fallback import png_bytes, form_values
from test_linghui_features import _load_module

ROOT = Path(__file__).resolve().parents[1]
PROMPT = _load_module("persona_prompt")
PLANNER = _load_module("persona_planner")


def compose_harness():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
    methods = [node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name in {
        "_plan_persona_photos", "_build_current_time_persona_hint"}]
    namespace = {"AstrMessageEvent": object, "Optional": Optional, "List": List, "Dict": Dict,
        "photo_plan_prompt": PLANNER.photo_plan_prompt, "logger": Mock()}
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Harness"]


class PersonaPromptTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"persona_name": "角色 A", "persona_description": "银色短发，绿色眼睛。",
            "persona_default_outfit": "白色工作制服", "persona_photo_style": "水彩插画",
            "persona_outfit_mode": "llm", "persona_expression_mode": "context"}
        self.planner = PLANNER.PersonaPhotoPlanner(Path(self.temp.name), self.config)
        self.plugin = compose_harness()()
        self.plugin.persona_planner = self.planner
        self.plugin.persona_profile = types.SimpleNamespace(get_state=AsyncMock(return_value={"outfit": "灰色卫衣", "mood": "安静"}))
        self.plugin._get_conf_bool = lambda key, default: True
        self.event = types.SimpleNamespace(unified_msg_origin="test:group:1")

    async def compose(self, request, *, scene="阳台香草架，站立", visual="", photos=None):
        base = PROMPT.build_persona_base_prompt(self.config, scene)
        token, prompts = await self.plugin._plan_persona_photos(self.event, request, base, 1,
            photos=photos, user_request=request, visual_request=visual, scene_request=scene)
        await self.planner.release(token)
        return prompts[0]

    async def test_llm_outfit_removes_both_daily_and_default_outfit(self):
        prompt = await self.compose("看看你", photos=[{"outfit": "蓝色夹克和长裤", "expression": "认真", "pose": "站在香草架旁", "gaze": "看向镜头"}])
        self.assertIn("蓝色夹克和长裤", prompt)
        self.assertNotIn("灰色卫衣", prompt)
        self.assertNotIn("白色工作制服", prompt)
        self.assertEqual(prompt.count("User request (authoritative):"), 1)
        self.assertIn("银色短发", prompt)

    async def test_original_user_outfit_and_happiness_remove_conflicting_llm_suggestions(self):
        request = "穿绿色外套，在公园快乐地玩，拍一张"
        prompt = await self.compose(request, scene="公园，白天", visual="红色制服，表情害羞，远处有树",
            photos=[{"outfit": "黑色礼服", "expression": "难过", "pose": "自然站立", "gaze": "看向镜头"}])
        self.assertEqual(prompt.count(request), 1)
        for forbidden in ("红色制服", "黑色礼服", "害羞", "难过", "灰色卫衣", "白色工作制服"):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("远处有树", prompt)

    async def test_freeform_direction_is_kept_once_and_not_used_as_two_fields(self):
        text = "穿蓝色外套，认真地看向镜头"
        prompt = await self.compose("看看你", visual=text)
        self.assertEqual(prompt.count("穿蓝色外套"), 1)
        self.assertEqual(prompt.count("认真地看向镜头"), 1)
        self.assertNotIn("outfit: 穿蓝色外套", prompt)
        self.assertNotIn("expression: 穿蓝色外套", prompt)

    async def test_default_outfit_is_used_only_after_daily_state_and_llm_are_absent(self):
        self.plugin.persona_profile.get_state.return_value = {}
        prompt = await self.compose("看看你")
        self.assertIn("白色工作制服", prompt)
        self.plugin.persona_profile.get_state.return_value = {"outfit": "灰色卫衣"}
        prompt = await self.compose("看看你")
        self.assertIn("灰色卫衣", prompt)
        self.assertNotIn("白色工作制服", prompt)

    async def test_legacy_scene_direction_suppresses_visual_defaults(self):
        prompt = await self.compose("看看你", scene="公园，穿蓝色夹克，快乐地看向树梢")
        self.assertEqual(prompt.count("蓝色夹克"), 1)
        for forbidden in ("灰色卫衣", "白色工作制服", "根据本次场景与心情", "自然看向镜头"):
            self.assertNotIn(forbidden, prompt)

    async def test_structured_directions_override_duplicate_freeform_fields(self):
        prompt = await self.compose("看看你", visual="蓝色夹克，难过的表情，远处有树",
            photos=[{"outfit": "蓝色夹克", "expression": "认真专注", "gaze": "看向镜头", "pose": "站立"}])
        self.assertEqual(prompt.count("蓝色夹克"), 1)
        self.assertNotIn("难过", prompt)
        self.assertIn("远处有树", prompt)

    async def test_explicit_scene_does_not_inherit_old_pose_or_time(self):
        name, scene = PROMPT.resolve_persona_scene("明亮的书店，站在窗边", "蓝色夹克", "昨晚阳台上的花盆",
            {"阳台": "清晨，蹲着给花盆浇水"}, "傍晚的河岸")
        prompt = await self.compose("看看你", scene=scene)
        self.assertEqual(name, "明亮的书店，站在窗边")
        self.assertIn("明亮的书店", prompt)
        for old in ("浇水", "蹲着", "清晨", "傍晚的河岸"):
            self.assertNotIn(old, prompt)
        self.assertEqual(PROMPT.resolve_persona_scene("阳台", "", "", {"阳台": "盆栽阳台"}, ""), ("阳台", "盆栽阳台"))

    async def test_daily_hint_excludes_visual_defaults_and_respects_explicit_time(self):
        manager = _load_module("persona_profile").PersonaProfileManager(Path(self.temp.name), self.config)
        manager.now = lambda: datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
        manager._state = {"date": "2026-11-06", "outfit": "灰色卫衣", "mood": "害羞"}
        prompt = await manager.build_prompt_hint("傍晚的河边", include_visual_defaults=False)
        self.assertNotIn("灰色卫衣", prompt)
        self.assertNotIn("害羞", prompt)
        self.assertNotIn("Current time-of-day", prompt)

    async def test_appearance_configuration_is_preserved_without_automatic_splitting(self):
        self.config["persona_description"] = "短发。穿着旧版制服。"
        before = dict(self.config)
        prompt = PROMPT.build_persona_base_prompt(self.config)
        self.assertEqual(self.config, before)
        self.assertIn("穿着旧版制服", prompt)


class ImagesQualityTest(unittest.IsolatedAsyncioTestCase):
    manager = transport_test.TransportFallbackTest.manager

    async def test_gpt_image_json_passes_quality_and_preserves_prompt_and_model(self):
        manager, session = self.manager([(200, png_bytes())], image_quality="high")
        prompt = "A watercolor robot reading at a window."
        await manager.call_images_api([], prompt, "gpt-image-2.5", "fixture-key", "https://relay.example.com",
            generation_params={"resolution": "2K", "aspect_ratio": "4:3", "size": "2048x1536"})
        body = session.calls[0][1]["json"]
        self.assertEqual(body["quality"], "high")
        self.assertEqual(body["size"], "2048x1536")
        self.assertEqual(body["prompt"], prompt)
        self.assertEqual(body["model"], "gpt-image-2.5")
        self.assertNotIn("input_fidelity", body)

    async def test_gpt_image_multipart_keeps_reference_and_quality(self):
        manager, session = self.manager([(200, png_bytes())], image_quality="xhigh")
        original = png_bytes("green")
        await manager.call_images_api([original], "A robot wearing a blue coat.", "gpt-image-2.5", "fixture-key", "https://relay.example.com")
        fields = session.calls[0][1]
        self.assertEqual(form_values(fields, "quality"), ["xhigh"])
        self.assertEqual(form_values(fields, "image"), [original])
        self.assertEqual(form_values(fields, "input_fidelity"), [])

    async def test_non_gpt_model_does_not_receive_unsupported_quality_option(self):
        manager, session = self.manager([(200, png_bytes())], image_quality="high")
        await manager.call_images_api([], "A robot.", "other-image-model", "fixture-key", "https://relay.example.com")
        self.assertNotIn("quality", session.calls[0][1]["json"])


class PersonaConfigAndQualityTest(unittest.IsolatedAsyncioTestCase):
    setUp = dashboard_test.DashboardChannelModesTest.setUp
    save_channels = dashboard_test.DashboardChannelModesTest.save_channels

    async def test_persona_appearance_and_default_outfit_save_independently(self):
        self.plugin.conf["persona_description"] = "旧外观"
        self.api._json_body = AsyncMock(return_value={"persona": {"description": "新外观", "default_outfit": "蓝色工作服"}})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            response = await self.api.save_config()
        self.assertTrue(response["success"])
        self.assertEqual(self.plugin.conf["persona_description"], "新外观")
        self.assertEqual(self.plugin.conf["persona_default_outfit"], "蓝色工作服")
        self.plugin._save_config.assert_called_once_with(["persona_default_outfit", "persona_description"])

    async def test_channel_quality_round_trips_and_invalid_quality_is_rejected(self):
        response = await self.save_channels([{"id": "primary", "image_quality": "high", "api_keys": ""}])
        self.assertTrue(response["success"])
        self.assertEqual(self.api._public_channel(self.plugin.conf["drawing_channels"][0], 0)["image_quality"], "high")
        with self.assertRaises(ValueError):
            _load_module("channel_policy").normalize_policy({"image_quality": "ultra"})

    async def test_retired_global_quality_cannot_be_written_back(self):
        self.api._json_body = AsyncMock(return_value={"settings": {"image_quality": "high"}})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            response = await self.api.save_config()
        self.assertTrue(response["success"])
        self.assertNotIn("image_quality", self.plugin.conf)
        self.plugin._save_config.assert_not_called()
        self.api._json_body = AsyncMock(return_value={"settings": {"image_quality": "ultra"}})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            response = await self.api.save_config()
        self.assertTrue(response["success"])
        self.assertNotIn("image_quality", self.plugin.conf)
        self.plugin._save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()

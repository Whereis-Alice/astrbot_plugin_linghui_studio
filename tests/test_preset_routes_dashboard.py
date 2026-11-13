"""Preset route persistence and GIF settings across Dashboard API surfaces."""

import copy
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_full_image import load_module


class PresetRoutesDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.conf = {"prompt_list": ["动画:make it move", "静态:keep still"],
                     "drawing_channels": [{"id": "gif", "model": "gpt-image-2.6-flare", "enabled": True},
                                          {"id": "static", "enabled": True}]}
        self.manager = load_module("data_manager").DataManager(pathlib.Path(self.directory.name), self.conf)
        self.plugin = types.SimpleNamespace(conf=self.conf, data_mgr=self.manager, _save_config=Mock(),
            _load_persona_scenes=Mock(), api_mgr=types.SimpleNamespace(refresh=AsyncMock()))
        self.api = self.module.LinghuiDashboardApi(self.plugin)
        mocked_json = patch.object(self.module, "jsonify", side_effect=lambda body: body)
        mocked_json.start()
        self.addCleanup(mocked_json.stop)

    async def save(self, payload):
        self.api._json_body = AsyncMock(return_value=payload)
        return await self.api.save_config()

    def bind(self, name="动画", channel="gif"):
        self.conf["preset_channel_bindings"] = [{"preset": name, "channel_id": channel}]
        self.conf.pop("preset_config_version", None)

    async def test_bulk_save_and_legacy_payload_preserve_binding_and_name_rename(self):
        response = await self.save({"presets": [
            {"name": "动画", "prompt": "loop", "channel_id": "gif"},
            {"name": "静态", "prompt": "still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.api._preset_binding_map(), {"动画": "gif"})
        self.assertIn("preset_channel_bindings", self.plugin._save_config.call_args.args[0])
        response = await self.save({"presets": [
            {"name": "改名动画", "original_name": "动画", "prompt": "loop twice"},
            {"name": "静态", "prompt": "still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.api._preset_binding_map(), {"改名动画": "gif"})

    async def test_clearing_or_deleting_preset_removes_binding(self):
        self.bind()
        response = await self.save({"presets": [{"name": "动画", "prompt": "loop", "channel_id": ""}]})
        self.assertTrue(response["success"])
        self.assertNotIn("preset_channel_bindings", self.conf)
        self.assertEqual(self.api._preset_channels_map(), {})
        self.bind()
        response = await self.save({"presets": []})
        self.assertTrue(response["success"])
        self.assertNotIn("preset_channel_bindings", self.conf)
        self.assertEqual(self.conf["preset_generation_options"], [])

    async def test_renaming_channel_updates_binding(self):
        self.bind()
        self.conf.update(active_drawing_channel="gif", reference_image_drawing_channel="gif")
        response = await self.save({"channels": [{"id": "animated", "original_id": "gif"}, {"id": "static"}],
                                   "active_drawing_channel": "gif"})
        self.assertTrue(response["success"])
        self.assertEqual(self.api._preset_binding_map(), {"动画": "animated"})
        self.assertEqual(self.conf["active_drawing_channel"], "animated")
        self.assertEqual(self.conf["reference_image_drawing_channel"], "animated")

    async def test_deleting_or_disabling_bound_channel_rejects_before_any_mutation(self):
        self.bind()
        before = copy.deepcopy(self.conf)
        for channels in ([{"id": "static"}], [{"id": "gif", "enabled": False}, {"id": "static"}]):
            response, status = await self.save({"channels": channels, "settings": {"model": "unexpected"}})
            self.assertEqual(status, 400)
            self.assertIn("解除绑定", response["message"])
            self.assertEqual(self.conf, before)
        self.plugin._save_config.assert_not_called()

    async def test_unavailable_saved_target_is_visible_and_can_be_cleared(self):
        self.bind(channel="removed")
        row = next(row for row in self.api._preset_rows() if row["name"] == "动画")
        self.assertEqual(row["channel_id"], "removed")
        response = await self.save({"presets": [{"name": "动画", "prompt": "loop", "channel_id": ""}]})
        self.assertTrue(response["success"])
        self.assertNotIn("preset_channel_bindings", self.conf)
        self.assertEqual(self.api._preset_channels_map(), {})

    async def test_create_preset_binds_only_existing_enabled_channel(self):
        for channel in ("absent", 17, None, ["gif"]):
            self.api._json_body = AsyncMock(return_value={"name": "新动画", "prompt": "loop", "channel_id": channel})
            _, status = await self.api.preset_create()
            self.assertEqual(status, 400)
            self.assertNotIn("新动画", self.manager.user_prompts)
        self.api._json_body = AsyncMock(return_value={"name": "新动画", "prompt": "loop", "channel_id": "gif"})
        result = await self.api.preset_create()
        self.assertTrue(result["success"])
        self.assertEqual(result["preset"]["channel_id"], "gif")
        self.assertIn("preset_channel_bindings", self.plugin._save_config.call_args.args[0])

    async def test_create_failure_restores_binding_and_prompt(self):
        self.plugin._save_config.side_effect = OSError("disk full")
        self.api._json_body = AsyncMock(return_value={"name": "新动画", "prompt": "loop", "channel_id": "gif"})
        _, status = await self.api.preset_create()
        self.assertEqual(status, 500)
        self.assertNotIn("preset_channel_bindings", self.conf)
        self.assertNotIn("新动画", self.manager.user_prompts)

    def test_config_export_import_round_trips_template_binding_and_gif_options(self):
        self.bind()
        self.conf["preset_generation_options"] = [{"preset": "动画", "image_output_format": "gif",
            "image_frame_count": 12, "image_frame_duration_ms": 100, "image_complexity": "high"}]
        document = self.api._export_config_document()
        normalized, ignored, _ = self.api._normalize_import_config(document["config"])
        self.assertIsInstance(normalized["preset_generation_options"], list)
        self.assertEqual(normalized["preset_generation_options"][0]["channel_ids"], ["gif"])
        self.assertNotIn("preset_channel_bindings", normalized)
        self.assertEqual(normalized["preset_generation_options"][0]["image_frame_duration_ms"], 100)
        self.assertNotIn("image_frame_duration_ms", normalized)
        self.assertEqual(ignored, [])
        for malformed in ("not a list", [{"preset": "动画", "channel_id": 4}],
                          [{"preset": "动画", "channel_id": "gif"}] * 2,
                          [{"preset": "absent", "channel_id": "gif"}]):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                self.api._normalize_import_config({"preset_channel_bindings": malformed})

    async def test_invalid_gif_options_reject_without_partial_mutation(self):
        before = copy.deepcopy(self.conf)
        cases = ({"image_frame_count": 1}, {"image_frame_count": True}, {"image_frame_count": 17},
                 {"image_frame_duration_ms": 125}, {"image_frame_duration_ms": 2010},
                 {"image_output_format": "svg"}, {"image_complexity": "max"})
        for option in cases:
            _, status = await self.save({"channels": [{"id": "gif", **option}]})
            self.assertEqual(status, 400)
            self.assertEqual(self.conf, before)
            with self.assertRaises(ValueError):
                self.api._normalize_import_config({"drawing_channels": [{"id": "gif", **option}]})

    async def test_dashboard_retry_preserves_preset_and_session_routing(self):
        self.plugin._call_generation_api_task = AsyncMock(return_value="mock upstream failure")
        self.plugin._snapshot_generation_route_metrics = lambda: {}
        self.plugin.task_mgr = types.SimpleNamespace(finish_failure=AsyncMock())
        await self.api._run_dashboard_generation(
            task_id="retry", session_id="qq:GroupMessage:100", user_id="1", group_id="100",
            user_name="", group_name="", images=[], prompt="loop", model="default",
            preset="动画", task_type="文生图", use_text_to_image_api=True,
            aspect_ratio="", resolution="",
        )
        arguments = self.plugin._call_generation_api_task.call_args.kwargs
        self.assertEqual(arguments["preset_name"], "动画")
        self.assertEqual(arguments["session_id"], "qq:GroupMessage:100")
        self.plugin.task_mgr.finish_failure.assert_awaited_once()

    async def test_channel_gif_options_round_trip_and_retired_globals_are_ignored(self):
        options = {"image_output_format": "gif", "image_frame_count": 12,
                   "image_frame_duration_ms": 100, "image_complexity": "medium"}
        result = await self.save({"settings": options, "channels": [{"id": "gif", **options}]})
        self.assertTrue(result["success"])
        channel = self.api._public_channel(self.conf["drawing_channels"][0], 0)
        for key, value in options.items():
            self.assertNotIn(key, self.conf)
            self.assertEqual(channel[key], value)


if __name__ == "__main__":
    unittest.main()

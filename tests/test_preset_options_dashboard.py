"""Per-preset image settings persist independently of reusable drawing channels."""

import copy
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_full_image import load_module


class PresetImageOptionsDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conf = {
            "prompt_list": ["动图画:make a loop", "静态:keep still"],
            "drawing_channels": [{"id": "miku", "enabled": True, "model": "gpt-image-2.5-sunburst",
                                  "image_output_format": "auto"},
                                 {"id": "orb", "enabled": True, "model": "gpt-image-2.6-sunburst"}],
        }
        self.manager = load_module("data_manager").DataManager(pathlib.Path(directory.name), self.conf)
        self.plugin = types.SimpleNamespace(
            conf=self.conf, data_mgr=self.manager, _save_config=Mock(), _load_persona_scenes=Mock(),
            api_mgr=types.SimpleNamespace(refresh=AsyncMock()),
        )
        self.api = self.module.LinghuiDashboardApi(self.plugin)
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda body: body)
        json_patch.start()
        self.addCleanup(json_patch.stop)

    def options(self, name="动图画", **overrides):
        return {"preset": name, "image_output_format": "gif", "image_frame_count": 12,
                "image_frame_duration_ms": 170, "image_complexity": "high", **overrides}

    async def save(self, payload):
        self.api._json_body = AsyncMock(return_value=payload)
        return await self.api.save_config()

    def row(self, name="动图画"):
        return next(row for row in self.api._preset_rows() if row["name"] == name)

    async def test_save_preset_gif_and_channel_in_one_request_without_mutating_channel(self):
        channels = copy.deepcopy(self.conf["drawing_channels"])
        response = await self.save({"presets": [
            {"name": "动图画", "prompt": "make a loop", "channel_id": "miku",
             **{key: value for key, value in self.options().items() if key != "preset"}},
            {"name": "静态", "prompt": "keep still"},
        ]})
        self.assertTrue(response["success"])
        for key, value in self.options().items():
            if key != "preset":
                self.assertEqual(self.row()[key], value)
        self.assertEqual(self.row()["channel_id"], "miku")
        self.assertEqual(self.conf["drawing_channels"], channels)
        self.assertIn("preset_generation_options", self.plugin._save_config.call_args.args[0])
        self.assertEqual(self.row("静态")["image_output_format"], "auto")

    async def test_legacy_payload_and_rename_keep_options_then_deletion_removes_them(self):
        self.conf["preset_generation_options"] = [self.options()]
        response = await self.save({"presets": [
            {"name": "改名动图", "original_name": "动图画", "prompt": "still loops"},
            {"name": "静态", "prompt": "keep still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row("改名动图")["image_frame_duration_ms"], 170)
        self.assertEqual(self.conf["preset_generation_options"][0]["preset"], "改名动图")
        response = await self.save({"presets": [{"name": "静态", "prompt": "keep still"}]})
        self.assertTrue(response["success"])
        self.assertEqual(self.conf["preset_generation_options"], [])

    async def test_partial_row_update_preserves_other_animation_fields(self):
        self.conf["preset_generation_options"] = [self.options()]
        response = await self.save({"presets": [
            {"name": "动图画", "prompt": "make a loop", "image_frame_count": 8},
            {"name": "静态", "prompt": "keep still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["image_frame_count"], 8)
        self.assertEqual(self.row()["image_output_format"], "gif")
        self.assertEqual(self.row()["image_frame_duration_ms"], 170)

    async def test_multiple_selected_channels_preserve_order_primary_compatibility_and_gif_options(self):
        response = await self.save({"presets": [
            {"name": "动图画", "prompt": "make a loop", "channel_ids": ["orb", "miku"],
             "channel_id": "ignored-old-value", "image_output_format": "gif"},
            {"name": "静态", "prompt": "keep still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["channel_ids"], ["orb", "miku"])
        self.assertEqual(self.row()["channel_id"], "orb")
        saved = self.conf["preset_channel_bindings"][0]
        self.assertEqual(saved["channel_ids"], ["orb", "miku"])
        self.assertEqual(saved["channel_id"], "orb")
        self.assertEqual(self.row()["image_output_format"], "gif")
        response = await self.save({"presets": [
            {"name": "改名动图", "original_name": "动图画", "prompt": "make a loop"},
            {"name": "静态", "prompt": "keep still"},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row("改名动图")["channel_ids"], ["orb", "miku"])

    async def test_channel_rename_updates_all_selected_preset_channels_without_losing_order(self):
        self.conf["preset_channel_bindings"] = [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": ["miku", "orb"]},
        ]
        response = await self.save({"channels": [
            {"id": "new_miku", "original_id": "miku", "enabled": True},
            {"id": "new_orb", "original_id": "orb", "enabled": True},
        ]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["channel_ids"], ["new_miku", "new_orb"])
        self.assertEqual(self.row()["channel_id"], "new_miku")

    async def test_invalid_multi_selection_or_disabling_secondary_channel_rejects_atomically(self):
        self.conf["preset_channel_bindings"] = [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": ["miku", "orb"]},
        ]
        before = copy.deepcopy(self.conf)
        invalid = ("miku", ["miku", "miku"], ["miku", "missing"], ["miku", 1], [""])
        for selection in invalid:
            with self.subTest(selection=selection):
                _, status = await self.save({"presets": [
                    {"name": "动图画", "prompt": "loop", "channel_ids": selection},
                ], "settings": {"model": "must-not-change"}})
                self.assertEqual(status, 400)
                self.assertEqual(self.conf, before)
        _, status = await self.save({"channels": [{"id": "miku"}, {"id": "orb", "enabled": False}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.conf, before)

    async def test_empty_channel_selection_unbinds_and_legacy_selection_remains_supported(self):
        response = await self.save({"presets": [{"name": "动图画", "prompt": "loop", "channel_id": "miku"}]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["channel_ids"], ["miku"])
        response = await self.save({"presets": [{"name": "动图画", "prompt": "loop", "channel_ids": []}]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["channel_ids"], [])
        self.assertEqual(self.row()["channel_id"], "")

    async def test_native_empty_default_list_preserves_legacy_binding_but_post_empty_clears(self):
        self.conf["preset_channel_bindings"] = [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": []},
        ]
        self.assertEqual(self.row()["channel_ids"], ["miku"])
        exported = self.api._export_config_document()
        self.assertEqual(exported["config"]["preset_channel_bindings"][0]["channel_ids"], ["miku"])
        normalized, _, _ = self.api._normalize_import_config({"preset_channel_bindings": [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": []},
        ]})
        self.assertEqual(normalized["preset_channel_bindings"][0]["channel_ids"], ["miku"])
        result = await self.save({"preset_channel_bindings": [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": []},
        ]})
        self.assertTrue(result["success"])
        self.assertEqual(self.conf["preset_channel_bindings"], [])

    async def test_top_level_parameter_list_roundtrip_and_explicit_clear(self):
        response = await self.save({"preset_generation_options": [self.options()]})
        self.assertTrue(response["success"])
        self.assertEqual(self.row()["image_output_format"], "gif")
        response = await self.save({"preset_generation_options": []})
        self.assertTrue(response["success"])
        self.assertEqual(self.conf["preset_generation_options"], [])
        self.assertEqual(self.row()["image_output_format"], "auto")
        del self.conf["preset_generation_options"]
        response = await self.save({"preset_generation_options": []})
        self.assertTrue(response["success"])
        self.assertIn("preset_generation_options", self.conf)

    async def test_invalid_row_options_reject_before_channel_prompt_or_setting_changes(self):
        self.conf["preset_generation_options"] = [self.options()]
        before = copy.deepcopy(self.conf)
        invalid = ({"image_frame_count": 1}, {"image_frame_count": True},
                   {"image_frame_duration_ms": 171}, {"image_output_format": "svg"},
                   {"image_complexity": "max"})
        for values in invalid:
            with self.subTest(values=values):
                result, status = await self.save({"settings": {"model": "must-not-change"}, "presets": [
                    {"name": "动图画", "prompt": "must not replace prompt", "channel_id": "orb", **values},
                ]})
                self.assertEqual(status, 400)
                self.assertFalse(result["success"])
                self.assertEqual(self.conf, before)
                self.assertEqual(self.manager.user_prompts, {})
        self.plugin._save_config.assert_not_called()

    async def test_invalid_top_level_options_reject_atomically(self):
        before = copy.deepcopy(self.conf)
        invalid = ("not-list", [self.options("missing")], [self.options(), self.options()],
                   [self.options(image_output_format=None)])
        for values in invalid:
            _, status = await self.save({"settings": {"model": "must-not-change"},
                                        "preset_generation_options": values})
            self.assertEqual(status, 400)
            self.assertEqual(self.conf, before)

    async def test_create_gif_preset_validates_options_and_existing_channel_before_writing(self):
        self.api._json_body = AsyncMock(return_value={
            "name": "新动图", "prompt": "loop", "channel_id": "miku",
            **{key: value for key, value in self.options().items() if key != "preset"},
        })
        result = await self.api.preset_create()
        self.assertTrue(result["success"])
        self.assertEqual(result["preset"]["image_output_format"], "gif")
        self.assertEqual(result["preset"]["image_frame_count"], 12)
        self.assertEqual(result["preset"]["channel_id"], "miku")
        self.assertEqual(self.conf["drawing_channels"][0]["image_output_format"], "auto")
        self.api._json_body = AsyncMock(return_value={
            "name": "坏参数", "prompt": "loop", "image_output_format": "gif", "image_frame_count": 99,
        })
        _, status = await self.api.preset_create()
        self.assertEqual(status, 400)
        self.assertNotIn("坏参数", self.manager.user_prompts)

    async def test_failed_create_rolls_back_new_options_without_erasing_existing_options(self):
        self.conf["preset_generation_options"] = [self.options()]
        before = copy.deepcopy(self.conf)
        self.plugin._save_config.side_effect = OSError("disk full")
        self.api._json_body = AsyncMock(return_value={
            "name": "新动图", "prompt": "loop", "channel_id": "orb", "image_output_format": "gif",
        })
        _, status = await self.api.preset_create()
        self.assertEqual(status, 500)
        self.assertEqual(self.conf, before)
        self.assertNotIn("新动图", self.manager.user_prompts)

    async def test_create_multiple_channels_preserves_order_and_rejects_bad_secondary_before_write(self):
        self.api._json_body = AsyncMock(return_value={
            "name": "新动图", "prompt": "loop", "channel_ids": ["orb", "miku"], "image_output_format": "gif",
        })
        result = await self.api.preset_create()
        self.assertTrue(result["success"])
        self.assertEqual(result["preset"]["channel_ids"], ["orb", "miku"])
        self.assertEqual(result["preset"]["channel_id"], "orb")
        before = copy.deepcopy(self.conf)
        self.api._json_body = AsyncMock(return_value={
            "name": "坏渠道", "prompt": "loop", "channel_ids": ["orb", "missing"], "image_output_format": "gif",
        })
        _, status = await self.api.preset_create()
        self.assertEqual(status, 400)
        self.assertEqual(self.conf, before)
        self.assertNotIn("坏渠道", self.manager.user_prompts)

    async def test_create_does_not_clear_native_migrated_legacy_binding(self):
        self.conf["preset_channel_bindings"] = [
            {"preset": "动图画", "channel_id": "miku", "channel_ids": []},
        ]
        self.api._json_body = AsyncMock(return_value={
            "name": "新动图", "prompt": "loop", "channel_ids": ["orb", "miku"], "image_output_format": "gif",
        })
        result = await self.api.preset_create()
        self.assertTrue(result["success"])
        self.assertEqual(self.row()["channel_ids"], ["miku"])

    def test_export_import_preserves_preset_options_as_objects(self):
        self.conf["preset_generation_options"] = [self.options()]
        document = self.api._export_config_document()
        options = document["config"]["preset_generation_options"]
        self.assertIsInstance(options, list)
        self.assertEqual(options[0]["image_frame_count"], 12)
        normalized, ignored, _ = self.api._normalize_import_config(document["config"])
        self.assertEqual(ignored, [])
        self.assertIsInstance(normalized["preset_generation_options"], list)
        self.assertEqual(normalized["preset_generation_options"][0]["image_output_format"], "gif")
        self.assertEqual(normalized["preset_generation_options"][0]["image_frame_duration_ms"], 170)
        with self.assertRaises(ValueError):
            self.api._normalize_import_config({"preset_generation_options": [self.options("missing")]})

    def test_export_does_not_add_options_for_an_absent_builtin_preset(self):
        document = self.api._export_config_document()
        self.assertEqual(document["config"]["preset_generation_options"], [])
        normalized, _, _ = self.api._normalize_import_config(document["config"])
        self.assertEqual(normalized["preset_generation_options"], [])

    def test_export_import_preserves_full_ordered_channel_selection(self):
        self.conf["preset_channel_bindings"] = [
            {"preset": "动图画", "channel_id": "orb", "channel_ids": ["orb", "miku"]},
        ]
        document = self.api._export_config_document()
        normalized, _, _ = self.api._normalize_import_config(document["config"])
        self.assertEqual(normalized["preset_channel_bindings"][0]["channel_ids"], ["orb", "miku"])
        self.assertEqual(normalized["preset_channel_bindings"][0]["channel_id"], "orb")
        # Importing only generation parameters must leave selected channels untouched.
        normalized, _, _ = self.api._normalize_import_config({"preset_generation_options": [self.options()]})
        self.assertNotIn("preset_channel_bindings", normalized)

    async def test_export_includes_chat_preset_prompt_so_its_gif_options_import_on_another_instance(self):
        await self.manager.add_user_prompt("聊天动图", "chat-created loop")
        self.conf["preset_generation_options"] = [self.options("聊天动图")]
        document = self.api._export_config_document()
        self.assertIn("聊天动图:chat-created loop", document["config"]["prompt_list"])
        fresh = self.module.LinghuiDashboardApi(types.SimpleNamespace(
            conf={"prompt_list": [], "drawing_channels": copy.deepcopy(self.conf["drawing_channels"])},
            data_mgr=types.SimpleNamespace(user_prompts={}),
        ))
        normalized, _, _ = fresh._normalize_import_config(document["config"])
        self.assertEqual(normalized["preset_generation_options"][0]["preset"], "聊天动图")


if __name__ == "__main__":
    unittest.main()

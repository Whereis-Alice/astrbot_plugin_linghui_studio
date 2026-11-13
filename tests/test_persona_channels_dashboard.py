"""Selfie channel selection is ordered, validated, portable, and independent."""

import copy
import asyncio
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_full_image import load_module


class PersonaChannelsDashboardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conf = {
            "prompt_list": ["照片:portrait"],
            "drawing_channels": [
                {"id": "best", "model": "best-model", "enabled": True, "api_keys": "best-secret", "base_url": "https://best.example"},
                {"id": "backup", "model": "backup-model", "enabled": True, "api_keys": "backup-secret", "base_url": "https://backup.example"},
                {"id": "text", "enabled": True, "reference_image_enabled": False},
                {"id": "disabled", "enabled": False},
            ],
        }
        self.manager = load_module("data_manager").DataManager(pathlib.Path(directory.name), self.conf)
        self.plugin = types.SimpleNamespace(
            conf=self.conf, data_mgr=self.manager, _save_config=Mock(), _load_persona_scenes=Mock(),
            api_mgr=types.SimpleNamespace(refresh=AsyncMock()),
        )
        self.api = self.module.LinghuiDashboardApi(self.plugin)
        self.api._command_prefix_display = lambda: "#"
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda body: body)
        json_patch.start()
        self.addCleanup(json_patch.stop)

    async def save(self, payload):
        self.api._json_body = AsyncMock(return_value=payload)
        return await self.api.save_config()

    async def test_ordered_selection_is_saved_and_returned_without_single_endpoint_fields(self):
        result = await self.save({"settings": {"persona_channel_ids": ["backup", "best"]}})
        self.assertTrue(result["success"])
        self.assertEqual(self.conf["persona_channel_ids"], ["backup", "best"])
        self.plugin._save_config.assert_called_once_with(["persona_channel_ids"])
        public = await self.api.get_config()
        self.assertEqual(public["settings"]["persona_channel_ids"], ["backup", "best"])
        removed = load_module("configuration_cleanup").REMOVED_CONFIG_KEYS
        self.assertTrue(set(removed).isdisjoint(public["settings"]))

    async def test_invalid_selection_rejects_related_mutations_atomically(self):
        for value in (["best", "best"], ["missing"], ["disabled"], ["text"], [""], [3], "best", ["best"] * 9):
            before = copy.deepcopy(self.conf)
            with self.subTest(value=value):
                result, status = await self.save({"settings": {"persona_channel_ids": value, "show_model_info": True}})
                self.assertEqual(status, 400)
                self.assertFalse(result["success"])
                self.assertEqual(self.conf, before)
        self.plugin._save_config.assert_not_called()

    async def test_channel_rename_updates_selection_and_preserves_order(self):
        self.conf["persona_channel_ids"] = ["backup", "best"]
        channels = copy.deepcopy(self.conf["drawing_channels"])
        channels[0].update(id="renamed", original_id="best")
        result = await self.save({"channels": channels})
        self.assertTrue(result["success"])
        self.assertEqual(self.conf["persona_channel_ids"], ["backup", "renamed"])
        self.assertEqual(self.conf["drawing_channels"][0]["api_keys"], "best-secret")

    async def test_selected_channel_disable_delete_and_reference_disable_are_rejected(self):
        self.conf["persona_channel_ids"] = ["best", "backup"]
        for operation in ("delete", "disable", "disable_ref"):
            channels = copy.deepcopy(self.conf["drawing_channels"])
            if operation == "delete":
                channels.pop(0)
            elif operation == "disable":
                channels[0]["enabled"] = False
            else:
                channels[0]["reference_image_enabled"] = False
            before = copy.deepcopy(self.conf)
            with self.subTest(operation=operation):
                _, status = await self.save({"channels": channels})
                self.assertEqual(status, 400)
                self.assertEqual(self.conf, before)

    async def test_clear_selection_and_delete_channel_in_one_save(self):
        self.conf["persona_channel_ids"] = ["best"]
        result = await self.save({"settings": {"persona_channel_ids": []}, "channels": self.conf["drawing_channels"][1:]})
        self.assertTrue(result["success"])
        self.assertEqual(self.conf["persona_channel_ids"], [])
        self.assertNotIn("best", {row["id"] for row in self.conf["drawing_channels"]})

    def test_config_export_import_keeps_profile_and_ignores_retired_top_level_fields(self):
        self.conf["persona_channel_ids"] = ["backup", "best"]
        self.conf.update(base_url="https://retired.example", api_keys="retired-secret", image_output_format="gif")
        document = self.api._export_config_document()
        self.assertEqual(document["config"]["persona_channel_ids"], ["backup", "best"])
        removed = set(load_module("configuration_cleanup").REMOVED_CONFIG_KEYS)
        self.assertTrue(removed.isdisjoint(document["config"]))
        normalized, ignored, _ = self.api._normalize_import_config({
            "persona_channel_ids": ["best", "backup"], "base_url": "https://old.example", "api_keys": "old-key",
            "image_output_format": "gif", "image_frame_count": 12,
        })
        self.assertEqual(normalized["persona_channel_ids"], ["best", "backup"])
        self.assertEqual(set(ignored), {"base_url", "api_keys", "image_output_format", "image_frame_count"})
        self.assertTrue(removed.isdisjoint(normalized))
        for value in (["text"], ["missing"], ["best", "best"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api._normalize_import_config({"persona_channel_ids": value})

    async def test_channel_export_import_keeps_profile_and_does_not_inherit_old_keys(self):
        self.conf["persona_channel_ids"] = ["backup", "best"]
        self.conf["api_keys"] = "retired-secret"
        self.conf["drawing_channels"][0]["api_keys"] = ""
        document = self.api._export_channels_document()
        self.assertEqual(document["persona_channel_ids"], ["backup", "best"])
        self.assertEqual(document["channels"][0]["api_keys"], "")
        self.conf["persona_channel_ids"] = []
        self.api._json_body = AsyncMock(return_value={"document": document})
        result = await self.api.channels_import()
        self.assertTrue(result["success"])
        self.assertEqual(self.conf["persona_channel_ids"], ["backup", "best"])

    async def test_old_global_settings_payload_cannot_restore_removed_fields(self):
        result = await self.save({"settings": {"model": "old-model", "image_resolution": "4K",
            "image_output_format": "gif", "image_frame_count": 12, "timeout": 900,
            "enable_binary_image_response": False}})
        self.assertTrue(result["success"])
        removed = set(load_module("configuration_cleanup").REMOVED_CONFIG_KEYS)
        self.assertTrue(removed.isdisjoint(self.conf))

    def test_import_cannot_disable_a_selected_selfie_channel(self):
        self.conf["persona_channel_ids"] = ["best"]
        channels = copy.deepcopy(self.conf["drawing_channels"])
        channels[0]["enabled"] = False
        before = copy.deepcopy(self.conf)
        with self.assertRaises(ValueError):
            self.api._normalize_import_config({"drawing_channels": channels})
        self.assertEqual(self.conf, before)

    async def test_failed_profile_import_restores_original_selection(self):
        self.conf["persona_channel_ids"] = ["best"]
        before = copy.deepcopy(self.conf)
        self.plugin.img_mgr = types.SimpleNamespace()
        self.plugin.api_mgr.refresh = AsyncMock(side_effect=[RuntimeError("refresh failed"), None])
        self.api._json_body = AsyncMock(return_value={"action": "apply", "config": {"persona_channel_ids": ["backup"]}})
        result, status = await self.api.config_import()
        self.assertEqual(status, 500)
        self.assertFalse(result["success"])
        self.assertEqual(self.conf, before)

    async def test_old_selfie_rerun_uses_persona_profile_and_preserves_it_in_new_task(self):
        previous = {"id": "old", "task_type": "人设拍照", "prompt": "portrait", "requested_model": "retired-default",
                    "input_count": 1, "session_id": "qq:GroupMessage:100", "request": {}}
        new_task = {**previous, "id": "new"}
        self.plugin.task_mgr = types.SimpleNamespace(
            get_task=AsyncMock(return_value=previous), get_request_images=AsyncMock(return_value=[b"reference"]),
            begin_task=AsyncMock(return_value=(new_task, False)),
        )
        runner = AsyncMock()
        self.api._run_dashboard_generation = runner
        jobs = []
        self.api._spawn_background_job = lambda coro: jobs.append(asyncio.create_task(coro))
        self.api._json_body = AsyncMock(return_value={"action": "rerun", "id": "old"})
        result, status = await self.api.generation_task()
        await asyncio.gather(*jobs)
        self.assertEqual(status, 202)
        self.assertTrue(result["success"])
        saved = self.plugin.task_mgr.begin_task.call_args.kwargs
        self.assertEqual(saved["request"]["routing_profile"], "persona")
        self.assertEqual(saved["requested_model"], "")
        self.assertEqual(runner.call_args.kwargs["routing_profile"], "persona")

    async def test_empty_channel_configuration_has_no_implicit_legacy_service(self):
        self.conf.update(drawing_channels=[], base_url="https://retired.example", api_keys="old-key")
        self.assertIsNone(self.api._resolve_channel_config(""))
        self.module.request = types.SimpleNamespace(args={})
        result, status = await self.api.channel_models()
        self.assertEqual(status, 404)
        self.assertFalse(result["success"])

    async def test_rerun_transport_preserves_persona_profile(self):
        self.plugin._call_generation_api_task = AsyncMock(return_value="HTTP 503")
        self.plugin._snapshot_generation_route_metrics = lambda: {}
        self.plugin.task_mgr = types.SimpleNamespace(finish_failure=AsyncMock())
        await self.api._run_dashboard_generation(
            task_id="retry", session_id="qq:GroupMessage:100", user_id="1", group_id="100",
            user_name="", group_name="", images=[b"ref"], prompt="portrait", model="",
            preset="", task_type="人设拍照", use_text_to_image_api=False,
            aspect_ratio="", resolution="", routing_profile="persona",
        )
        self.assertEqual(self.plugin._call_generation_api_task.call_args.kwargs["routing_profile"], "persona")
        self.assertTrue(self.plugin._call_generation_api_task.call_args.kwargs["prefer_config_aspect_ratio"])


if __name__ == "__main__":
    unittest.main()

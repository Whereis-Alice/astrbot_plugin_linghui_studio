"""Cross-instance configuration imports replace snapshots and roll back both stores."""

import copy
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_full_image import load_module


class ConfigImportSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda body: body)
        json_patch.start()
        self.addCleanup(json_patch.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.conf = {
            "prompt_list": ["动图画:destination loop", "静态:destination still"],
            "drawing_channels": [{"id": "miku", "enabled": True, "api_keys": "fake-miku-key"},
                                 {"id": "orb", "enabled": True, "api_keys": "fake-orb-key"}],
            "preset_config_version": 1,
            "preset_generation_options": [
                {"preset": "动图画", "channel_ids": ["orb", "miku"], "image_output_format": "gif"},
                {"preset": "静态", "channel_ids": ["orb"]},
            ],
        }
        self.manager = load_module("data_manager").DataManager(pathlib.Path(self.directory.name), self.conf)
        self.manager.reload_prompts()
        self.plugin = types.SimpleNamespace(
            conf=self.conf, data_mgr=self.manager, _save_config=Mock(), _load_persona_scenes=Mock(),
            img_mgr=types.SimpleNamespace(), api_mgr=types.SimpleNamespace(refresh=AsyncMock()),
        )
        self.api = self.module.LinghuiDashboardApi(self.plugin)

    def source_document(self, options):
        source_conf = {
            "prompt_list": ["动图画:source loop", "静态:source still"],
            "drawing_channels": copy.deepcopy(self.conf["drawing_channels"]),
            "preset_config_version": 1,
            "preset_generation_options": options,
        }
        source = self.module.LinghuiDashboardApi(types.SimpleNamespace(
            conf=source_conf, data_mgr=types.SimpleNamespace(user_prompts={}),
        ))
        return source._export_config_document()

    async def import_config(self, document, action="apply"):
        self.api._json_body = AsyncMock(return_value={"action": action, "document": document})
        return await self.api.config_import()

    def test_canonical_empty_snapshot_clears_all_destination_selections(self):
        document = self.source_document([])
        self.conf["preset_generation_options"][0]["channel_ids"] = ["removed-destination-channel"]
        normalized, _, _ = self.api._normalize_import_config(document["config"])
        self.assertEqual(normalized["preset_generation_options"], [])
        self.assertNotIn("preset_channel_bindings", normalized)

    def test_canonical_rows_missing_routes_and_missing_presets_follow_ordinary_routing(self):
        for version in (1, 2):
            with self.subTest(version=version):
                normalized, _, _ = self.api._normalize_import_config({
                    "preset_config_version": version,
                    "preset_generation_options": [{"preset": "动图画", "image_output_format": "gif"}],
                })
                rows = normalized["preset_generation_options"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["preset"], "动图画")
                self.assertEqual(rows[0]["channel_ids"], [])
                self.assertEqual(rows[0]["image_output_format"], "gif")

    def test_canonical_explicit_route_order_and_empty_selection_replace_destination(self):
        normalized, _, _ = self.api._normalize_import_config({
            "preset_config_version": 1,
            "preset_generation_options": [{"preset": "动图画", "channel_ids": ["miku", "orb"]},
                                          {"preset": "静态", "channel_ids": []}],
        })
        rows = {row["preset"]: row["channel_ids"] for row in normalized["preset_generation_options"]}
        self.assertEqual(rows, {"动图画": ["miku", "orb"], "静态": []})

    def test_legacy_image_only_and_partial_imports_keep_destination_routes(self):
        for payload in (
            {"preset_generation_options": [{"preset": "动图画", "image_output_format": "gif"}]},
            {"preset_config_version": 0, "preset_generation_options": []},
            {"drawing_channels": copy.deepcopy(self.conf["drawing_channels"])},
        ):
            with self.subTest(payload=payload):
                normalized, _, _ = self.api._normalize_import_config(payload)
                rows = {row["preset"]: row["channel_ids"] for row in normalized["preset_generation_options"]}
                self.assertEqual(rows, {"动图画": ["orb", "miku"], "静态": ["orb"]})

    async def test_full_import_preview_and_apply_replace_effective_prompts_and_persist(self):
        document = self.source_document([])
        # The native list can already match while chat persistence overrides it.
        self.conf["prompt_list"] = document["config"]["prompt_list"][:]
        await self.manager.add_user_prompt("动图画", "stale chat prompt")
        await self.manager.add_user_prompt("旧预设", "destination-only prompt")
        before_file = self.manager.user_prompts_file.read_bytes()
        preview = await self.import_config(document, "preview")
        self.assertTrue(preview["success"])
        self.assertIn("prompt_list", preview["changed_keys"])
        prompt_diff = next(row for row in preview["diff"] if row["key"] == "prompt_list")
        self.assertIn("动图画:stale chat prompt", prompt_diff["before"])
        self.assertEqual(self.manager.user_prompts_file.read_bytes(), before_file)
        self.assertEqual(self.manager.get_prompt("动图画"), "stale chat prompt")

        result = await self.import_config(document)
        self.assertTrue(result["success"])
        expected = {"动图画": "source loop", "静态": "source still"}
        self.assertEqual(self.manager.user_prompts, expected)
        self.assertEqual(self.manager.get_prompt("动图画"), "source loop")
        self.assertIsNone(self.manager.get_prompt("旧预设"))
        self.assertEqual(json.loads(self.manager.user_prompts_file.read_text(encoding="utf-8")), expected)
        self.assertEqual(self.conf["preset_generation_options"], [])
        self.assertEqual(self.conf["drawing_channels"][0]["api_keys"], "fake-miku-key")

    async def test_failed_full_import_restores_config_and_persisted_user_prompts(self):
        for failure in ("save", "refresh"):
            with self.subTest(failure=failure):
                await self.manager.add_user_prompt("动图画", "old effective prompt")
                document = self.source_document([])
                before = copy.deepcopy(self.conf)
                before_prompts = copy.deepcopy(self.manager.user_prompts)
                if failure == "save":
                    self.plugin._save_config.side_effect = [OSError("disk full"), None]
                    self.plugin.api_mgr.refresh.side_effect = None
                else:
                    self.plugin._save_config.side_effect = None
                    self.plugin.api_mgr.refresh.side_effect = [RuntimeError("refresh failed"), None]
                result, status = await self.import_config(document)
                self.assertEqual(status, 500)
                self.assertFalse(result["success"])
                self.assertEqual(self.conf, before)
                self.assertEqual(self.manager.user_prompts, before_prompts)
                self.assertEqual(self.manager.get_prompt("动图画"), "old effective prompt")
                self.assertEqual(json.loads(self.manager.user_prompts_file.read_text(encoding="utf-8")), before_prompts)

    async def test_partial_import_does_not_change_chat_presets(self):
        await self.manager.add_user_prompt("动图画", "keep local prompt")
        before = self.manager.user_prompts_file.read_bytes()
        result = await self.import_config({"show_model_info": True})
        self.assertTrue(result["success"])
        self.assertEqual(self.manager.user_prompts_file.read_bytes(), before)
        self.assertEqual(self.manager.get_prompt("动图画"), "keep local prompt")

    async def test_import_prompt_list_accepts_full_width_separator_and_clears_old_entries(self):
        await self.manager.add_user_prompt("旧预设", "remove this")
        result = await self.import_config({
            "prompt_list": ["新预设：新的提示词"],
            "preset_config_version": 1, "preset_generation_options": [],
        })
        self.assertTrue(result["success"])
        self.assertEqual(self.manager.user_prompts, {"新预设": "新的提示词"})
        self.assertEqual(self.manager.get_prompt("新预设"), "新的提示词")
        result = await self.import_config({"prompt_list": []})
        self.assertTrue(result["success"])
        self.assertEqual(self.manager.user_prompts, {})
        self.assertIsNone(self.manager.get_prompt("新预设"))


if __name__ == "__main__":
    unittest.main()

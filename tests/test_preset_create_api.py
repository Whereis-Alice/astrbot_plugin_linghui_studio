import asyncio
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_generation_full_image import load_module


class PresetCreateApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.conf = {"prompt_list": ["旧预设:keep old"], "image_resolution": "2K"}
        self.manager = load_module("data_manager").DataManager(pathlib.Path(self.directory.name), self.conf)
        self.plugin = types.SimpleNamespace(conf=self.conf, data_mgr=self.manager, _save_config=Mock())
        self.api = self.module.LinghuiDashboardApi(self.plugin)
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda body: body)
        json_patch.start()
        self.addCleanup(json_patch.stop)

    async def create(self, name="新预设", prompt="new prompt"):
        self.api._json_body = AsyncMock(return_value={"name": name, "prompt": prompt})
        return await self.api.preset_create()

    async def test_one_create_persists_and_preserves_unrelated_config_and_presets(self):
        response = await self.create(" 新预设 ", "first\nsecond")
        self.assertTrue(response["success"])
        self.assertEqual(response["preset"]["name"], "新预设")
        self.assertEqual(self.conf["prompt_list"], ["旧预设:keep old", "新预设:first\nsecond"])
        self.assertEqual(self.conf["image_resolution"], "2K")
        self.plugin._save_config.assert_called_once_with(["prompt_list"])
        stored = json.loads(self.manager.user_prompts_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["新预设"], "first\nsecond")
        self.assertEqual(self.manager.prompt_map["新预设"], "first\nsecond")

    async def test_duplicate_config_and_chat_presets_are_rejected(self):
        await self.manager.add_user_prompt("聊天预设", "keep chat")
        for name in ("旧预设", "聊天预设"):
            response, status = await self.create(name)
            self.assertEqual(status, 409)
            self.assertFalse(response["success"])
        self.assertEqual(self.manager.user_prompts["聊天预设"], "keep chat")
        self.plugin._save_config.assert_not_called()

    async def test_invalid_inputs_never_create_partial_presets(self):
        cases = [("", "text"), ("cat", ""), ("a:b", "text"), ("a：b", "text"), ("a\nb", "text"), ("a" * 49, "text"), ("cat", "a" * 12001)]
        for name, prompt in cases:
            response, status = await self.create(name, prompt)
            self.assertEqual(status, 400)
            self.assertFalse(response["success"])
        self.assertEqual(self.manager.user_prompts, {})
        self.assertEqual(self.conf["prompt_list"], ["旧预设:keep old"])

    async def test_preset_limit_is_checked_before_writing(self):
        self.conf["prompt_list"] = [f"preset{index}:text" for index in range(200)]
        _, status = await self.create()
        self.assertEqual(status, 400)
        self.assertEqual(self.manager.user_prompts, {})

    async def test_failed_config_write_rolls_back_new_preset(self):
        self.plugin._save_config.side_effect = RuntimeError("private/server/path")
        response, status = await self.create()
        self.assertEqual(status, 500)
        self.assertNotIn("private", str(response))
        self.assertEqual(self.conf["prompt_list"], ["旧预设:keep old"])
        self.assertNotIn("新预设", self.manager.user_prompts)
        self.assertNotIn("新预设", self.manager.prompt_map)

    async def test_concurrent_duplicate_requests_save_only_once(self):
        self.api._json_body = AsyncMock(return_value={"name": "新预设", "prompt": "text"})
        results = await asyncio.gather(self.api.preset_create(), self.api.preset_create())
        self.assertEqual(sum(isinstance(result, dict) and result["success"] for result in results), 1)
        self.assertEqual(sum(isinstance(result, tuple) and result[1] == 409 for result in results), 1)
        self.assertEqual(self.conf["prompt_list"].count("新预设:text"), 1)


if __name__ == "__main__":
    unittest.main()

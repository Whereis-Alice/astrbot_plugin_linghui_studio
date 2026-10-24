import importlib
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_dashboard_modes_test_package"


def load_dashboard_api():
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot.logger = Mock()
        sys.modules["astrbot"] = astrbot
    if "quart" not in sys.modules:
        quart = types.ModuleType("quart")
        quart.jsonify = lambda payload: payload
        quart.request = types.SimpleNamespace(args={})
        async def send_file(*args, **kwargs):
            return args, kwargs

        quart.send_file = send_file
        sys.modules["quart"] = quart
    return importlib.import_module(f"{PACKAGE}.dashboard_api")


class DashboardChannelModesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_dashboard_api()
        self.plugin = types.SimpleNamespace(
            conf={"drawing_channels": [{
                "__template_key": "drawing_channel",
                "id": "primary",
                "interface_mode": "openai_image",
                "api_keys": "existing-test-key",
            }]},
            data_mgr=types.SimpleNamespace(reload_prompts=Mock()),
            _load_persona_scenes=Mock(),
            _save_config=Mock(),
            api_mgr=types.SimpleNamespace(refresh=AsyncMock()),
        )
        self.api = self.module.LinghuiDashboardApi(self.plugin)

    async def save_channels(self, channels):
        self.api._json_body = AsyncMock(return_value={"channels": channels})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            return await self.api.save_config()

    async def test_auto_mode_saves_and_round_trips_without_reentering_key(self):
        response = await self.save_channels([{
            "id": "primary",
            "interface_mode": "auto",
            "api_keys": "",
        }])

        self.assertTrue(response["success"])
        channel = self.plugin.conf["drawing_channels"][0]
        self.assertEqual(channel["interface_mode"], "auto")
        self.assertEqual(channel["api_keys"], "existing-test-key")
        self.assertEqual(channel["__template_key"], "drawing_channel")
        public = self.api._public_channel(channel, 0)
        self.assertEqual(public["interface_mode"], "auto")
        self.assertTrue(public["has_api_keys"])
        self.assertNotIn("api_keys", public)
        self.plugin._save_config.assert_called_once_with(["drawing_channels"])
        self.plugin.api_mgr.refresh.assert_awaited_once()

    async def test_renaming_channel_keeps_key_and_template_with_auto_mode(self):
        response = await self.save_channels([{
            "id": "renamed",
            "original_id": "primary",
            "interface_mode": "auto",
            "api_keys": "",
            "image_edit_transport": "json",
        }])

        self.assertTrue(response["success"])
        channel = self.plugin.conf["drawing_channels"][0]
        self.assertEqual(channel["id"], "renamed")
        self.assertEqual(channel["api_keys"], "existing-test-key")
        self.assertEqual(channel["__template_key"], "drawing_channel")
        self.assertEqual(channel["image_edit_transport"], "json")

    async def test_missing_mode_preserves_existing_choice_and_defaults_new_to_auto(self):
        response = await self.save_channels([
            {"id": "primary"},
            {"id": "new_channel", "api_keys": "new-test-key"},
        ])

        self.assertTrue(response["success"])
        existing, new = self.plugin.conf["drawing_channels"]
        self.assertEqual(existing["interface_mode"], "openai_image")
        self.assertEqual(existing["api_keys"], "existing-test-key")
        self.assertEqual(new["interface_mode"], "auto")
        self.assertEqual(new["__template_key"], "drawing_channel")

    async def test_auto_mode_still_honors_explicit_key_clear(self):
        response = await self.save_channels([{
            "id": "primary",
            "interface_mode": "auto",
            "api_keys": "",
            "clear_api_keys": True,
        }])

        self.assertTrue(response["success"])
        self.assertEqual(self.plugin.conf["drawing_channels"][0]["api_keys"], "")

    async def test_unknown_mode_is_rejected_without_saving(self):
        response, status = await self.save_channels([{
            "id": "primary",
            "interface_mode": "invalid_protocol",
        }])

        self.assertEqual(status, 400)
        self.assertFalse(response["success"])
        self.assertEqual(self.plugin.conf["drawing_channels"][0]["interface_mode"], "openai_image")
        self.plugin._save_config.assert_not_called()
        self.plugin.api_mgr.refresh.assert_not_awaited()

    async def test_persona_modes_save_with_scoped_persistence(self):
        self.api._json_body = AsyncMock(return_value={"persona": {
            "outfit_mode": "llm", "expression_mode": "diverse", "expression_history": 4,
        }})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            response = await self.api.save_config()
        self.assertTrue(response["success"])
        self.assertEqual(self.plugin.conf["persona_outfit_mode"], "llm")
        self.assertEqual(self.plugin.conf["persona_expression_mode"], "diverse")
        self.assertEqual(self.plugin.conf["persona_expression_history"], 4)
        self.plugin._save_config.assert_called_once_with([
            "persona_expression_history", "persona_expression_mode", "persona_outfit_mode",
        ])

    async def test_bad_persona_mode_is_rejected_before_other_mutations(self):
        self.api._json_body = AsyncMock(return_value={"settings": {"model": "unexpected"},
            "persona": {"expression_mode": "unknown"}})
        with patch.object(self.module, "jsonify", side_effect=lambda payload: payload):
            response, status = await self.api.save_config()
        self.assertEqual(status, 400)
        self.assertFalse(response["success"])
        self.assertNotIn("model", self.plugin.conf)
        self.plugin._save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()

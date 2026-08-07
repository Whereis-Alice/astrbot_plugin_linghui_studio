import importlib
import io
import pathlib
import sys
import tempfile
import types
import unittest

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_test_package"


def _ensure_test_package():
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package

    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot.logger = types.SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
            exception=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        )
        sys.modules["astrbot"] = astrbot


def _load_module(name, *, with_quart=False):
    _ensure_test_package()
    if with_quart and "quart" not in sys.modules:
        quart = types.ModuleType("quart")
        quart.jsonify = lambda payload: payload
        quart.request = types.SimpleNamespace(args={})

        async def send_file(*args, **kwargs):
            return args, kwargs

        quart.send_file = send_file
        sys.modules["quart"] = quart
    return importlib.import_module(f"{PACKAGE}.{name}")


class AccessPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.AccessPolicy = _load_module("access_control").AccessPolicy

    def test_group_whitelist_grants_access_without_granting_unlimited_quota(self):
        policy = self.AccessPolicy({"group_whitelist": ["10001"]})
        decision = policy.evaluate("20001", "10001")
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.unlimited)
        self.assertEqual(decision.level, "limited")

    def test_blacklist_has_priority_over_unlimited_lists(self):
        policy = self.AccessPolicy({
            "group_whitelist": ["10001"],
            "unlimited_users": ["20001"],
            "unlimited_groups": ["10001"],
            "blocked_users": ["20001"],
        })
        decision = policy.evaluate("20001", "10001")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.level, "blocked_user")

    def test_admin_still_needs_an_allowed_group(self):
        policy = self.AccessPolicy({"group_whitelist": ["10001"], "admins_unlimited": True})
        denied = policy.evaluate("admin", "10002", is_admin=True)
        allowed = policy.evaluate("admin", "10001", is_admin=True)
        self.assertFalse(denied.allowed)
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.unlimited)

    def test_private_messages_require_the_explicit_switch(self):
        disabled = self.AccessPolicy({"allow_private_messages": False}).evaluate("20001", "")
        enabled = self.AccessPolicy({"allow_private_messages": True}).evaluate("20001", "")
        self.assertFalse(disabled.allowed)
        self.assertTrue(enabled.allowed)


class PromptProcessorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.PromptProcessor = _load_module("prompt_processor").PromptProcessor

    def test_openai_compatible_endpoints_are_normalized(self):
        endpoint = self.PromptProcessor._endpoint
        self.assertEqual(endpoint("https://api.example.com"), "https://api.example.com/v1/chat/completions")
        self.assertEqual(endpoint("https://api.example.com/v1/"), "https://api.example.com/v1/chat/completions")
        self.assertEqual(
            endpoint("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1/chat/completions",
        )


class DrawingChannelRouterTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module("channel_router")
        cls.original_api_manager = cls.module.ApiManager

    def setUp(self):
        class FakeApiManager:
            calls = []
            outcomes = {}

            def __init__(self, config):
                self.config = dict(config)
                self._session = None

            @staticmethod
            def _normalize_keys(value):
                if isinstance(value, str):
                    return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
                if isinstance(value, (list, tuple, set)):
                    return [str(item).strip() for item in value if str(item).strip()]
                return []

            async def call_api(self, *args, **kwargs):
                name = self.config.get("base_url")
                FakeApiManager.calls.append(name)
                return FakeApiManager.outcomes[name]

            def get_last_metrics(self):
                return {"transport": self.config.get("base_url")}

        self.fake_api_manager = FakeApiManager
        self.module.ApiManager = FakeApiManager

    def tearDown(self):
        self.module.ApiManager = self.original_api_manager

    def _router(self, config):
        return self.module.DrawingChannelRouter(config)

    async def test_auto_mode_skips_channels_not_enabled_for_fallback(self):
        self.fake_api_manager.outcomes = {
            "primary": "primary failed",
            "disabled-backup": b"should not be used",
            "backup": b"fallback result",
        }
        router = self._router({
            "drawing_channels": [
                {"id": "primary", "base_url": "primary", "api_keys": "one"},
                {"id": "disabled", "base_url": "disabled-backup", "api_keys": "two", "fallback_enabled": False},
                {"id": "backup", "base_url": "backup", "api_keys": "three", "fallback_enabled": True},
            ]
        })
        result = await router.call_api([], "prompt", "model")
        self.assertEqual(result, b"fallback result")
        self.assertEqual(self.fake_api_manager.calls, ["primary", "backup"])
        self.assertEqual(router.get_last_metrics()["channel_id"], "backup")
        await router.close()

    async def test_selected_channel_is_primary_and_model_overrides_are_resolved(self):
        router = self._router({
            "model": "global-model",
            "text_to_image_model": "global-text-model",
            "active_drawing_channel": "backup",
            "drawing_channels": [
                {"id": "primary", "base_url": "primary", "api_keys": "one", "fallback_enabled": False},
                {
                    "id": "backup",
                    "base_url": "backup",
                    "api_keys": "two",
                    "model": "backup-model",
                    "text_to_image_model": "backup-text-model",
                },
            ],
        })
        self.assertEqual([item["id"] for item in router._ordered_channels()], ["backup"])
        channel = router.channels()[1]
        self.assertEqual(router._resolve_model("", channel, True), "backup-text-model")
        self.assertEqual(router._resolve_model("custom-model", channel, True), "custom-model")
        await router.close()

    def test_channels_without_own_key_can_use_the_shared_legacy_pool(self):
        router = self._router({
            "api_keys": "shared-key",
            "drawing_channels": [{"id": "primary", "base_url": "primary"}],
        })
        self.assertTrue(router.has_available_keys())


class DashboardRegistrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashboardApi = _load_module("dashboard_api", with_quart=True).LinghuiDashboardApi

    def test_registers_the_routes_used_by_the_plugin_page(self):
        registered = []

        class Context:
            def register_web_api(self, path, handler, methods, description):
                registered.append((path, methods, description))

        plugin = types.SimpleNamespace(context=Context(), conf={}, data_mgr=types.SimpleNamespace(user_prompts={}))
        self.DashboardApi(plugin).register()
        self.assertEqual(
            [path for path, _, _ in registered],
            [
                "/astrbot_plugin_linghui_studio/get_config",
                "/astrbot_plugin_linghui_studio/save_config",
                "/astrbot_plugin_linghui_studio/get_usage",
                "/astrbot_plugin_linghui_studio/adjust_credit",
                "/astrbot_plugin_linghui_studio/reset_credit",
                "/astrbot_plugin_linghui_studio/reference",
                "/astrbot_plugin_linghui_studio/asset",
            ],
        )

    def test_dashboard_preset_rows_include_chat_side_presets(self):
        plugin = types.SimpleNamespace(
            context=types.SimpleNamespace(),
            conf={"prompt_list": ["config:from config"]},
            data_mgr=types.SimpleNamespace(user_prompts={"chat": "from chat", "config": "chat override"}),
        )
        rows = self.DashboardApi(plugin)._preset_rows()
        self.assertEqual(rows, [
            {"name": "chat", "prompt": "from chat"},
            {"name": "config", "prompt": "chat override"},
        ])

    def test_dashboard_preserves_or_clears_a_masked_channel_key_explicitly(self):
        plugin = types.SimpleNamespace(
            context=types.SimpleNamespace(),
            conf={
                "drawing_channels": [
                    {"id": "primary", "api_keys": "existing-key", "base_url": "https://old.example"}
                ]
            },
            data_mgr=types.SimpleNamespace(user_prompts={}),
        )
        api = self.DashboardApi(plugin)
        renamed = api._normalize_channels([
            {
                "id": "renamed",
                "original_id": "primary",
                "base_url": "https://new.example",
                "api_keys": "",
            }
        ])
        cleared = api._normalize_channels([
            {
                "id": "primary",
                "api_keys": "",
                "clear_api_keys": True,
            }
        ])
        self.assertEqual(renamed[0]["api_keys"], "existing-key")
        self.assertEqual(cleared[0]["api_keys"], "")


class ReferenceImageStorageTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DataManager = _load_module("data_manager").DataManager

    async def test_reference_image_uses_an_extension_matching_its_actual_format(self):
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(output, "JPEG")
        with tempfile.TemporaryDirectory() as directory:
            manager = self.DataManager(pathlib.Path(directory), {})
            filename = await manager.save_preset_ref_image("portrait", output.getvalue())
            self.assertTrue(filename.endswith(".jpg"))
            self.assertTrue((manager.preset_ref_images_dir / filename).is_file())


if __name__ == "__main__":
    unittest.main()

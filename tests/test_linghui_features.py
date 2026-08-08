import base64
import importlib
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

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


class ConfigurationDocumentationTest(unittest.TestCase):
    def test_schema_fields_have_user_facing_hints(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

        for name, definition in schema.items():
            self.assertTrue(definition.get("hint"), f"{name} is missing a configuration hint")
            for template in (definition.get("templates") or {}).values():
                for field_name, field in (template.get("items") or {}).items():
                    self.assertTrue(field.get("hint"), f"{name}.{field_name} is missing a configuration hint")


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
            prompts = []
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
                FakeApiManager.prompts.append(args[1])
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

    def test_channel_specific_image_edit_transport_overrides_the_shared_default(self):
        router = self._router({
            "image_edit_transport": "json",
            "drawing_channels": [{
                "id": "primary",
                "base_url": "primary",
                "api_keys": "one",
                "image_edit_transport": "multipart",
            }],
        })
        channel_config = router._channel_config(router.channels()[0])
        self.assertEqual(channel_config["image_edit_transport"], "multipart")

    def test_channels_without_own_key_can_use_the_shared_legacy_pool(self):
        router = self._router({
            "api_keys": "shared-key",
            "drawing_channels": [{"id": "primary", "base_url": "primary"}],
        })
        self.assertTrue(router.has_available_keys())

    async def test_custom_negative_prompt_is_added_after_prompt_preparation(self):
        self.fake_api_manager.outcomes = {"primary": "primary failed", "backup": b"result"}
        router = self._router({
            "drawing_channels": [
                {"id": "primary", "base_url": "primary", "api_keys": "one"},
                {"id": "backup", "base_url": "backup", "api_keys": "two"},
            ],
        })

        class PreparedPrompt:
            async def prepare(self, prompt):
                return f"prepared: {prompt}"

        router.prompt_processor = PreparedPrompt()
        result = await router.call_api(
            [], "cinematic portrait", "model", negative_prompt="blurry, watermark"
        )
        self.assertEqual(result, b"result")
        self.assertEqual(
            self.fake_api_manager.prompts,
            [
                "prepared: cinematic portrait\n\nNegative prompt: blurry, watermark",
                "prepared: cinematic portrait\n\nNegative prompt: blurry, watermark",
            ],
        )
        await router.close()


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
                "/astrbot_plugin_linghui_studio/generation_history",
                "/astrbot_plugin_linghui_studio/generation_record",
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
                "image_edit_transport": "multipart",
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
        self.assertEqual(renamed[0]["image_edit_transport"], "multipart")
        self.assertEqual(cleared[0]["api_keys"], "")
        self.assertEqual(renamed[0]["__template_key"], "drawing_channel")
        self.assertEqual(cleared[0]["__template_key"], "drawing_channel")


class DashboardSaveTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashboardApi = _load_module("dashboard_api", with_quart=True).LinghuiDashboardApi

    async def test_custom_negative_prompt_is_saved_from_dashboard(self):
        class DataManager:
            user_prompts = {}

            def reload_prompts(self):
                pass

        async def refresh():
            pass

        plugin = types.SimpleNamespace(
            conf={},
            data_mgr=DataManager(),
            _load_persona_scenes=lambda: None,
            _save_config=lambda: None,
            api_mgr=types.SimpleNamespace(refresh=refresh),
        )
        api = self.DashboardApi(plugin)

        async def request_body():
            return {
                "settings": {"generation_cache_retention_days": 14},
                "prompt_tools": {"custom_drawing_negative_prompt": "blurry, watermark"},
            }

        api._json_body = request_body
        response = await api.save_config()
        self.assertTrue(response["success"])
        self.assertEqual(plugin.conf["custom_drawing_negative_prompt"], "blurry, watermark")
        self.assertEqual(plugin.conf["generation_cache_retention_days"], 14)


class DashboardUsageTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashboardApi = _load_module("dashboard_api", with_quart=True).LinghuiDashboardApi

    async def test_daily_usage_exposes_group_and_user_counts(self):
        today = datetime.now().strftime("%Y-%m-%d")
        manager = types.SimpleNamespace(
            user_counts={"user-1": 8},
            group_counts={"group-1": 12},
            user_checkin_data={},
            daily_stats={"date": today, "users": {"user-1": 3}, "groups": {"group-1": 2}},
            get_preset_ref_stats=lambda: {},
        )
        payload = await self.DashboardApi(types.SimpleNamespace(data_mgr=manager)).get_usage()
        self.assertEqual(payload["daily_stats"]["users"], {"user-1": 3})
        self.assertEqual(payload["daily_stats"]["groups"], {"group-1": 2})

    async def test_stale_daily_usage_is_not_returned_as_today(self):
        manager = types.SimpleNamespace(
            user_counts={},
            group_counts={},
            user_checkin_data={},
            daily_stats={"date": "2000-01-01", "users": {"user-1": 3}, "groups": {"group-1": 2}},
            get_preset_ref_stats=lambda: {},
        )
        payload = await self.DashboardApi(types.SimpleNamespace(data_mgr=manager)).get_usage()
        self.assertEqual(payload["daily_stats"]["users"], {})
        self.assertEqual(payload["daily_stats"]["groups"], {})


class DashboardReferencePreviewTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DashboardApi = _load_module("dashboard_api", with_quart=True).LinghuiDashboardApi
        cls.DataManager = _load_module("data_manager").DataManager

    async def test_persona_preview_is_inlined_for_the_authenticated_plugin_page(self):
        output = io.BytesIO()
        Image.new("RGBA", (900, 640), (20, 180, 240, 180)).save(output, "PNG")

        with tempfile.TemporaryDirectory() as directory:
            manager = self.DataManager(pathlib.Path(directory), {})
            await manager.save_preset_ref_image("_persona_", output.getvalue())
            plugin = types.SimpleNamespace(
                conf={"custom_drawing_negative_prompt": "blurry, watermark"},
                data_mgr=manager,
            )

            payload = await self.DashboardApi(plugin).get_config()
            preview = payload["persona"]["reference_images"][0]
            self.assertTrue(preview.startswith("data:image/jpeg;base64,"))
            self.assertNotIn(str(manager.preset_ref_images_dir), preview)
            self.assertEqual(
                payload["prompt_tools"]["custom_drawing_negative_prompt"],
                "blurry, watermark",
            )

            image_data = base64.b64decode(preview.split(",", 1)[1])
            with Image.open(io.BytesIO(image_data)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), 560)


class GenerationHistoryStorageTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.DataManager = _load_module("data_manager").DataManager

    @staticmethod
    def _png_bytes() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (480, 300), (30, 170, 230)).save(output, "PNG")
        return output.getvalue()

    async def test_protected_success_record_survives_cleanup_until_unprotected(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.DataManager(pathlib.Path(directory), {})
            record = await manager.save_generation_record(
                self._png_bytes(),
                prompt="cinematic portrait",
                user_id="10001",
                group_id="20002",
                model="image-model",
                preset="手办化",
                task_type="图生图",
            )
            self.assertIsNotNone(record)
            self.assertTrue(manager.get_generation_image_path(record).is_file())
            self.assertEqual(record["prompt"], "cinematic portrait")
            self.assertEqual(record["group_id"], "20002")

            manager.generation_history[0]["created_at"] = (
                datetime.now() - timedelta(days=10)
            ).isoformat(timespec="seconds")
            await manager.update_generation_record_flags(record["id"], favorite=True)
            protected_cleanup = await manager.cleanup_generation_cache(7)
            self.assertEqual(protected_cleanup["removed_records"], 0)
            self.assertEqual(len(manager.generation_history), 1)
            self.assertTrue(manager.get_generation_image_path(manager.generation_history[0]).is_file())

            await manager.update_generation_record_flags(record["id"], favorite=False, locked=True)
            locked_cleanup = await manager.cleanup_generation_cache(7)
            self.assertEqual(locked_cleanup["removed_records"], 0)
            self.assertEqual(len(manager.generation_history), 1)

            await manager.update_generation_record_flags(record["id"], locked=False)
            expired_cleanup = await manager.cleanup_generation_cache(7)
            self.assertEqual(expired_cleanup["removed_records"], 1)
            self.assertEqual(manager.generation_history, [])


class DashboardGenerationHistoryTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard_module = _load_module("dashboard_api", with_quart=True)
        cls.DashboardApi = cls.dashboard_module.LinghuiDashboardApi
        cls.DataManager = _load_module("data_manager").DataManager

    async def test_history_returns_inline_preview_and_favorite_operation(self):
        output = io.BytesIO()
        Image.new("RGB", (800, 500), (100, 60, 220)).save(output, "PNG")

        with tempfile.TemporaryDirectory() as directory:
            manager = self.DataManager(pathlib.Path(directory), {})
            record = await manager.save_generation_record(
                output.getvalue(),
                prompt="a violet neon city",
                user_id="user-1",
                group_id="group-1",
                model="test-model",
                task_type="文生图",
            )
            plugin = types.SimpleNamespace(
                conf={"generation_cache_retention_days": 7},
                data_mgr=manager,
            )
            api = self.DashboardApi(plugin)
            original_args = self.dashboard_module.request.args
            self.dashboard_module.request.args = {"limit": "24", "offset": "0"}
            try:
                payload = await api.generation_history()
            finally:
                self.dashboard_module.request.args = original_args

            self.assertTrue(payload["success"])
            self.assertEqual(payload["summary"]["groups"], 1)
            self.assertEqual(payload["records"][0]["prompt"], "a violet neon city")
            self.assertTrue(payload["records"][0]["preview"].startswith("data:image/jpeg;base64,"))
            self.assertNotIn(str(manager.generation_cache_dir), payload["records"][0]["preview"])

            async def body():
                return {"action": "favorite", "id": record["id"], "value": True}

            api._json_body = body
            updated = await api.generation_record()
            self.assertTrue(updated["success"])
            self.assertTrue(manager.generation_history[0]["favorite"])


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

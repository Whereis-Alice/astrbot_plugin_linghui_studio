import asyncio
import importlib
import io
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_advanced_test_package"


def load_module(name, *, with_quart=False):
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

    if with_quart and "quart" not in sys.modules:
        quart = types.ModuleType("quart")
        quart.jsonify = lambda payload: payload
        quart.request = types.SimpleNamespace(args={}, method="GET")

        async def send_file(*args, **kwargs):
            return args, kwargs

        quart.send_file = send_file
        sys.modules["quart"] = quart

    return importlib.import_module(f"{PACKAGE}.{name}")


def png_bytes(color=(50, 120, 220)):
    output = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(output, format="PNG")
    return output.getvalue()


class GenerationTaskManagerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.Manager = load_module("task_manager").GenerationTaskManager

    async def test_task_dedup_cache_metrics_and_listing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.Manager(pathlib.Path(temp_dir), {
                "task_dedup_seconds": 180,
                "task_history_limit": 100,
                "task_request_retention_days": 7,
            })
            await manager.initialize()
            task, duplicate = await manager.begin_task(
                request_id="message-1",
                session_id="qq:group:100",
                user_id="200",
                group_id="100",
                task_type="自定义图生图",
                prompt="portrait",
                requested_model="model-a",
                images=[b"source-image"],
            )
            same, is_duplicate = await manager.begin_task(
                request_id="message-1",
                session_id="qq:group:100",
                user_id="200",
                group_id="100",
                task_type="自定义图生图",
                prompt="portrait",
                requested_model="model-a",
                images=[b"source-image"],
            )

            self.assertFalse(duplicate)
            self.assertTrue(is_duplicate)
            self.assertEqual(same["id"], task["id"])
            self.assertEqual(await manager.get_request_images(task["id"]), [b"source-image"])

            finished = await manager.finish_success(
                task["id"],
                metrics={
                    "model": "actual-model",
                    "channel_id": "backup",
                    "channel_name": "备用线路",
                    "attempt_chain": [{"channel_id": "primary", "success": False}],
                },
                result_record_id="record-1",
                delivery_status="sent",
            )
            tasks, total, summary = await manager.list_tasks(status="succeeded")

            self.assertEqual(finished["channel_id"], "backup")
            self.assertEqual(finished["actual_model"], "actual-model")
            self.assertEqual(total, 1)
            self.assertEqual(tasks[0]["result_record_id"], "record-1")
            self.assertEqual(summary["succeeded"], 1)
            await manager.close()

    async def test_cancelling_a_task_cancels_its_runtime_coroutine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.Manager(pathlib.Path(temp_dir), {})
            task, _ = await manager.begin_task(prompt="wait", force=True)
            runtime = asyncio.create_task(asyncio.Event().wait())
            await manager.attach_runtime_task(task["id"], runtime)

            success, message = await manager.cancel_task(task["id"])
            await asyncio.sleep(0)
            stored = await manager.get_task(task["id"])

            self.assertTrue(success)
            self.assertIn("已取消", message)
            self.assertTrue(runtime.cancelled())
            self.assertEqual(stored["status"], "cancelled")
            await manager.close()


class PersonaProfileManagerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.Manager = load_module("persona_profile").PersonaProfileManager

    async def test_daily_state_is_stable_editable_and_respects_explicit_clothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = {
                "persona_name": "Alice",
                "persona_character_type": "anime",
                "enable_persona_daily_state": True,
                "persona_daily_outfits": ["蓝色外套", "白色针织衫"],
                "persona_daily_moods": ["轻松", "俏皮"],
                "persona_time_period_prompts": ["夜间:安静的夜景灯光"],
                "persona_state_timezone": "UTC",
            }
            manager = self.Manager(pathlib.Path(temp_dir), config)
            fixed = datetime(2026, 8, 24, 20, 30, tzinfo=timezone.utc)
            manager.now = lambda: fixed

            first = await manager.get_state(fixed)
            second = await manager.get_state(fixed)
            updated = await manager.update_state(outfit="黑色夹克", mood="很开心")
            explicit = await manager.build_prompt_hint("请穿上和服拍一张")

            self.assertEqual(first["outfit"], second["outfit"])
            self.assertEqual(first["period"], "evening")
            self.assertEqual(updated["outfit"], "黑色夹克")
            self.assertIn("anime or illustrated character", explicit)
            self.assertIn("explicitly requested clothing", explicit)
            self.assertNotIn("Today's stable outfit is", explicit)

            refreshed = await manager.refresh_state()
            self.assertEqual(refreshed["refresh_token"], 1)
            self.assertTrue((pathlib.Path(temp_dir) / "persona_daily_state.json").is_file())


class StudioAssetManagerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.Manager = load_module("studio_manager").StudioAssetManager

    async def test_assets_can_be_added_reordered_loaded_deleted_and_cleared(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = self.Manager(pathlib.Path(temp_dir))
            first = await manager.add_image("identity", png_bytes((255, 0, 0)), label="角色 A")
            second = await manager.add_image("identity", png_bytes((0, 255, 0)), label="角色 B")

            summary = manager.public_summary()
            identity = next(item for item in summary["slots"] if item["id"] == "identity")
            self.assertEqual(identity["count"], 2)
            self.assertNotIn("filename", identity["items"][0])

            reordered = await manager.reorder("identity", [second["id"], first["id"]])
            loaded = await manager.load_selected_images([
                {"slot": "identity", "id": reordered[0]["id"]},
                {"slot": "identity", "id": reordered[1]["id"]},
            ])
            self.assertEqual(len(loaded), 2)
            self.assertTrue(await manager.remove_image("identity", first["id"]))
            self.assertEqual(await manager.clear_slot("identity"), 1)
            self.assertEqual(manager.public_summary()["slots"][0]["count"], 0)


class AdvancedDashboardApiTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module("dashboard_api", with_quart=True)
        cls.DashboardApi = cls.module.LinghuiDashboardApi

    def test_export_redacts_top_level_and_channel_secrets(self):
        plugin = types.SimpleNamespace(
            conf={
                "api_keys": "top-secret",
                "model": "image-model",
                "drawing_channels": [{
                    "id": "primary",
                    "base_url": "https://example.com",
                    "model": "image-model",
                    "api_keys": "channel-secret",
                }],
            },
            version="3.6.0",
        )
        api = self.DashboardApi(plugin)
        api._schema = lambda: {
            "api_keys": {"type": "string", "default": ""},
            "model": {"type": "string", "default": ""},
            "drawing_channels": {"type": "list", "default": []},
        }

        document = api._export_config_document()
        serialized = str(document)

        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("channel-secret", serialized)
        self.assertEqual(document["config"]["api_keys"], "__KEEP_EXISTING_SECRET__")
        self.assertEqual(
            document["config"]["drawing_channels"][0]["api_keys"],
            "__KEEP_EXISTING_SECRET__",
        )

    async def test_failed_config_import_restores_runtime_and_persists_snapshot(self):
        refresh_calls = 0
        save_snapshots = []

        async def refresh():
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                raise RuntimeError("refresh failed")

        plugin = types.SimpleNamespace(
            conf={
                "model": "old-model",
                "enable_persona_mode": False,
                "download_retries": 3,
                "preset_table_quality": "高清",
                "preset_table_columns": 5,
            },
            data_mgr=types.SimpleNamespace(reload_prompts=lambda: None),
            _load_persona_scenes=lambda: None,
            _save_config=lambda changed: save_snapshots.append(plugin.conf["model"]),
            _persona_mode=False,
            img_mgr=types.SimpleNamespace(max_retries=3, table_quality="高清", table_columns=5),
            api_mgr=types.SimpleNamespace(refresh=refresh),
        )
        api = self.DashboardApi(plugin)
        api._schema = lambda: {"model": {"type": "string", "default": ""}}

        async def request_body():
            return {"action": "apply", "config": {"model": "new-model"}}

        api._json_body = request_body
        response, status = await api.config_import()

        self.assertEqual(status, 500)
        self.assertFalse(response["success"])
        self.assertEqual(plugin.conf["model"], "old-model")
        self.assertEqual(save_snapshots, ["new-model", "old-model"])
        self.assertEqual(refresh_calls, 2)
        self.assertFalse(plugin._persona_mode)
        self.assertEqual(plugin.img_mgr.max_retries, 3)

    async def test_dashboard_generation_records_route_and_finishes_task(self):
        calls = []

        async def generation_call(*args, **kwargs):
            return b"generated-image"

        async def prepare_image(data):
            return data

        async def record(*args, **kwargs):
            calls.append(("record", kwargs))
            return {"id": "record-7"}

        class TaskManager:
            async def mark_generated(self, task_id, **kwargs):
                calls.append(("generated", task_id, kwargs))

            async def finish_success(self, task_id, **kwargs):
                calls.append(("success", task_id, kwargs))

            async def finish_failure(self, task_id, error, **kwargs):
                calls.append(("failure", task_id, str(error)))

        class DataManager:
            async def update_generation_record_delivery(self, record_id, status):
                calls.append(("delivery", record_id, status))

        plugin = types.SimpleNamespace(
            _call_generation_api_task=generation_call,
            _snapshot_generation_route_metrics=lambda: {
                "channel_id": "backup",
                "channel_name": "备用线路",
                "model": "actual-model",
            },
            _prepare_send_image_bytes=prepare_image,
            _record_generation_result=record,
            task_mgr=TaskManager(),
            data_mgr=DataManager(),
        )
        api = self.DashboardApi(plugin)

        await api._run_dashboard_generation(
            task_id="task-7",
            session_id="dashboard:studio",
            user_id="dashboard",
            group_id="",
            user_name="Dashboard",
            group_name="",
            images=[b"source"],
            prompt="portrait",
            model="requested-model",
            preset="工作台",
            task_type="工作台图生图",
            use_text_to_image_api=False,
            aspect_ratio="1:1",
            resolution="1K",
        )

        self.assertTrue(any(item[0] == "record" for item in calls))
        self.assertTrue(any(item[0] == "generated" for item in calls))
        success = next(item for item in calls if item[0] == "success")
        self.assertEqual(success[2]["result_record_id"], "record-7")
        self.assertEqual(success[2]["delivery_status"], "dashboard_only")


class ErrorRedactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.safe_error_summary = staticmethod(
            load_module("error_classify").safe_error_summary
        )

    def test_provider_diagnostics_redact_common_secret_shapes(self):
        result = self.safe_error_summary(
            "URL https://example.com/v1/models?key=AIza-secret-value&foo=1 "
            "Authorization: Bearer bearer-secret api_key='plain-secret' "
            "x-goog-api-key: google-secret sk-1234567890abcdef"
        )

        self.assertNotIn("AIza-secret-value", result)
        self.assertNotIn("bearer-secret", result)
        self.assertNotIn("plain-secret", result)
        self.assertNotIn("google-secret", result)
        self.assertNotIn("1234567890abcdef", result)


if __name__ == "__main__":
    unittest.main()

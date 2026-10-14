import asyncio
import importlib
import io
import pathlib
import sys
import tempfile
import types
import unittest

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_image_path_test_package"


def _load_plugin():
    """Load the real plugin module against the installed AstrBot package.

    Other test modules replace ``astrbot`` with a lightweight shim, so the full
    plugin entrypoint can only be imported while the real runtime is available.
    """
    astrbot_module = sys.modules.get("astrbot")
    if astrbot_module is not None:
        if not hasattr(astrbot_module, "__path__") or "astrbot.api" not in sys.modules:
            raise unittest.SkipTest("AstrBot runtime is unavailable in this test process")

    try:
        from astrbot.api.star import StarTools
    except Exception as exc:  # pragma: no cover - depends on test environment
        raise unittest.SkipTest(f"AstrBot runtime is not importable: {exc}")

    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.main"), StarTools


class LlmImagePathReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.StarTools = _load_plugin()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self._original_data_dir = staticmethod(self.StarTools.get_data_dir)
        temp_root = pathlib.Path(self.directory.name)
        self.StarTools.get_data_dir = staticmethod(lambda: temp_root)
        self.addCleanup(
            lambda: setattr(self.StarTools, "get_data_dir", self._original_data_dir)
        )

        from unittest.mock import Mock

        self.plugin = self.module.LinghuiStudioPlugin(Mock(), {})

    def _make_record(self):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 120, 40)).save(buffer, "PNG")
        return asyncio.run(
            self.plugin.data_mgr.save_generation_record(
                buffer.getvalue(),
                prompt="prompt",
                user_id="10001",
                group_id="20002",
                preset="手办化",
                task_type="图生图",
            )
        )

    def test_report_contains_filename_and_absolute_path(self):
        record = self._make_record()
        report = self.plugin._format_llm_image_path_report([record])
        self.assertIn(record["filename"], report)
        self.assertIn("完整路径", report)
        self.assertIn("仅供你内部参考", report)

    def test_success_result_attaches_paths_when_enabled(self):
        record = self._make_record()
        text = self.plugin._finalize_llm_tool_success("已发送", result_files=[record])
        self.assertIn(record["filename"], text)
        self.assertIn("【对话要求】", text)

    def test_success_result_skips_paths_when_disabled(self):
        record = self._make_record()
        self.plugin.conf["llm_report_image_path"] = False
        text = self.plugin._finalize_llm_tool_success("已发送", result_files=[record])
        self.assertNotIn(record["filename"], text)

    def test_pending_path_is_injected_once_per_session(self):
        record = self._make_record()
        session = "umo://image-path-test"

        class Event:
            unified_msg_origin = session

        class Request:
            def __init__(self):
                self.extra_user_content_parts = []

        asyncio.run(self.plugin._remember_pending_image_path(session, record))
        first = Request()
        asyncio.run(self.plugin.on_llm_request_inject_image_paths(Event(), first))
        joined = " ".join(getattr(part, "text", "") for part in first.extra_user_content_parts)
        self.assertIn(record["filename"], joined)

        second = Request()
        asyncio.run(self.plugin.on_llm_request_inject_image_paths(Event(), second))
        self.assertEqual(second.extra_user_content_parts, [])

    def test_pending_injection_respects_disabled_config(self):
        record = self._make_record()
        session = "umo://image-path-disabled"
        self.plugin.conf["llm_report_image_path"] = False

        class Event:
            unified_msg_origin = session

        class Request:
            def __init__(self):
                self.extra_user_content_parts = []

        asyncio.run(self.plugin._remember_pending_image_path(session, record))
        request = Request()
        asyncio.run(self.plugin.on_llm_request_inject_image_paths(Event(), request))
        self.assertEqual(request.extra_user_content_parts, [])


if __name__ == "__main__":
    unittest.main()

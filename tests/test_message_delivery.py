"""Regression coverage for late OneBot receipts and post-send storage errors."""
import ast
import asyncio
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_command_avatar_routing import command_harness
from test_generation_full_image import image_bytes
from test_linghui_features import _load_module

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Load the real SDK exception definitions without importing aiocqhttp's Quart
# application into the existing lightweight Dashboard test doubles.
exception_path = importlib.metadata.distribution("aiocqhttp").locate_file("aiocqhttp/exceptions.py")
exception_spec = importlib.util.spec_from_file_location("aiocqhttp.exceptions", exception_path)
exceptions = importlib.util.module_from_spec(exception_spec)
exception_spec.loader.exec_module(exceptions)
ActionFailed, ApiNotAvailable, NetworkError = exceptions.ActionFailed, exceptions.ApiNotAvailable, exceptions.NetworkError


def delivery_harness():
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
    names = {"_send_generated_result", "_finalize_generated_delivery", "_run_background_task",
             "_image_delivery_platform", "_deliver_generated_chain", "_send_generated_to_session"}
    methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    utils = _load_module("utils")
    namespace = {"AstrMessageEvent": Any, "Optional": Optional, "List": List, "Tuple": Tuple,
        "Dict": Dict, "Any": Any, "Callable": Callable, "Awaitable": Awaitable,
        "asyncio": asyncio, "datetime": datetime, "time": time,
        "bounded_int": _load_module("image_delivery").bounded_int,
        "prepare_generated_chain": _load_module("image_delivery").prepare_generated_chain,
        "prepare_outgoing_messages": _load_module("message_presentation").prepare_outgoing_messages,
        "Image": Image, "Plain": Plain, "logger": Mock(),
        "safe_error_summary": _load_module("error_classify").safe_error_summary,
        "format_generation_success": _load_module("success_presentation").format_generation_success,
        "QuotaDeniedError": _load_module("quota").QuotaDeniedError}
    namespace.update({name: getattr(utils, name) for name in (
        "is_ambiguous_message_delivery_timeout", "append_negative_prompt", "append_final_instruction",
        "extract_image_urls_from_text", "has_mention_privacy_instruction", "normalize_model_list",
        "MENTION_AVATAR_PRIVACY_INSTRUCTION")})
    base = command_harness()
    base.on_figurine_request.__globals__.update(namespace)
    namespace["Base"] = base
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[ast.Name(id="Base", ctx=ast.Load())],
        keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Harness"]


class MessageDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = delivery_harness()()
        self.plugin.conf = {"prefix": True, "extra_prefix": "画"}
        self.plugin.context = types.SimpleNamespace(get_config=lambda umo=None: {"wake_prefix": ["#"]})
        self.plugin._COMMAND_ROUTES = []
        self.plugin.img_mgr = types.SimpleNamespace(
            extract_plain_text_without_mentions=lambda event: "#bnn 一只猫",
            extract_mentioned_user_ids=lambda *args, **kwargs: [],
            extract_images_from_event=AsyncMock(return_value=[]))
        self.plugin.task_mgr = types.SimpleNamespace(mark_generated=AsyncMock(),
            finish_success=AsyncMock(return_value={"status": "succeeded"}), finish_failure=AsyncMock())
        self.plugin.data_mgr = types.SimpleNamespace(update_generation_record_delivery=AsyncMock())
        self.plugin._decorate_requester = lambda event, chain: chain
        self.plugin._is_message_processed = lambda key: False
        self.plugin._check_command_image_cooldown = lambda *args: (False, 0)
        self.plugin._check_quota = AsyncMock(return_value={"allowed": True})
        self.plugin._charge_quota = AsyncMock(return_value=True)
        self.plugin._refund_quota = AsyncMock()
        self.plugin._get_bot_id = lambda event: "9000"
        self.plugin._looks_like_context_image_reference = lambda prompt: False
        self.plugin._update_command_image_cooldown = Mock()
        self.plugin._get_text_to_image_model = lambda: "test-model"
        self.plugin._get_custom_drawing_negative_prompt = lambda: ""
        self.plugin._log_prompt_preview = Mock()
        self.plugin._begin_generation_task = AsyncMock(return_value=({"id": "task-1"}, False))
        self.plugin._call_generation_api_task = AsyncMock(return_value=image_bytes())
        self.plugin._snapshot_generation_route_metrics = lambda: {"model": "test-model"}
        self.plugin._prepare_send_image_bytes = AsyncMock(side_effect=lambda data: data)
        self.plugin._record_generation_result = AsyncMock(return_value={"id": "record-1"})
        self.plugin._get_quota_str = lambda *args: "10"
        self.plugin._format_success_timing = lambda elapsed: "1秒"
        self.plugin._requester_prefix_text = lambda event: ""
        self.plugin._notice_nodes = lambda event, text: [Plain(text)]
        self.plugin._effective_identity = lambda event, uid, gid: (uid, gid)
        self.plugin._complete_pending_generation = AsyncMock()
        self.plugin._maybe_stage_after_grace = AsyncMock(return_value=False)
        self.plugin._is_transient_generation_error = lambda result: False
        self.plugin._should_show_debug_errors = lambda: False
        self.plugin._public_error_message = lambda error, default: str(error) or default
        async def send_notice(event, text):
            await event.send(event.chain_result([Plain(text)]))
        self.plugin._send_task_notice = AsyncMock(side_effect=send_notice)
        self.event = types.SimpleNamespace(message_str="#bnn 一只猫",
            message_obj=types.SimpleNamespace(message=[], message_id="message-1"),
            unified_msg_origin="qq:GroupMessage:100", is_at_or_wake_command=True,
            get_sender_id=lambda: "1001", get_group_id=lambda: "100", stop_event=Mock(),
            send=AsyncMock(), chain_result=lambda nodes: MessageChain(nodes))

    async def command(self):
        return [result async for result in self.plugin.on_figurine_request(self.event)]

    async def test_direct_command_waiting_for_websocket_receipt_is_not_failed_or_retried(self):
        async def send(chain):
            if any(isinstance(node, Image) for node in chain.chain):
                raise NetworkError("WebSocket API call timeout")
        self.event.send.side_effect = send
        self.assertEqual(await self.command(), [])
        self.assertEqual(self.event.send.await_count, 2)  # Progress notice + one image.
        self.plugin.task_mgr.finish_failure.assert_not_awaited()
        self.assertEqual(self.plugin.task_mgr.finish_success.call_args.kwargs["delivery_status"], "possibly_sent")
        self.plugin.data_mgr.update_generation_record_delivery.assert_awaited_once_with("record-1", "possibly_sent")
        self.plugin._refund_quota.assert_not_awaited()

    async def test_direct_command_history_save_error_does_not_report_send_failure(self):
        self.plugin.data_mgr.update_generation_record_delivery.side_effect = OSError("disk full")
        self.assertEqual(await self.command(), [])
        self.plugin.task_mgr.finish_success.assert_awaited_once()
        self.plugin.task_mgr.finish_failure.assert_not_awaited()
        self.assertEqual(self.event.send.await_count, 2)

    async def test_direct_command_task_save_error_still_attempts_history_save(self):
        self.plugin.task_mgr.finish_success.side_effect = OSError("disk full")
        self.assertEqual(await self.command(), [])
        self.plugin.data_mgr.update_generation_record_delivery.assert_awaited_once_with("record-1", "sent")
        self.plugin.task_mgr.finish_failure.assert_not_awaited()

    async def test_explicit_send_rejection_still_reports_failure(self):
        async def send(chain):
            if any(isinstance(node, Image) for node in chain.chain):
                raise ActionFailed({"retcode": 1200, "message": "permission denied"})
        self.event.send.side_effect = send
        results = await self.command()
        self.assertIn("这次发送失败", results[0].chain[0].text)
        self.assertEqual(self.plugin.task_mgr.finish_failure.call_args.kwargs["status"], "send_failed")
        self.plugin.task_mgr.finish_success.assert_not_awaited()

    async def test_background_receipt_timeout_and_history_error_keep_success(self):
        self.event.send.side_effect = NetworkError("WebSocket API call timeout")
        self.plugin.data_mgr.update_generation_record_delivery.side_effect = OSError("disk full")
        result = await self.plugin._run_background_task(self.event, [], "一只猫", "自定义",
            {"allowed": True}, "1001", "100", 1, use_text_to_image_api=True)
        self.assertEqual(result, (True, ""))
        self.event.send.assert_awaited_once()
        self.plugin.task_mgr.finish_failure.assert_not_awaited()
        self.plugin._send_task_notice.assert_not_awaited()

    async def test_real_aiocqhttp_timeout_occurs_after_image_payload_write(self):
        # The full adapter imports Quart. Run it separately to keep its real
        # application dependencies independent from Dashboard unit-test stubs.
        code = '''
import asyncio, importlib.util, json, sys
from types import SimpleNamespace
from aiocqhttp.api_impl import WebSocketReverseApi
from aiocqhttp.exceptions import NetworkError
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain
import astrbot.api.star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
spec = importlib.util.spec_from_file_location("delivery_utils", sys.argv[1])
utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils)
async def main():
    written = []
    async def write(payload):
        written.append(json.loads(payload))
    bot = WebSocketReverseApi({"9000": SimpleNamespace(send=write)}, set(), 0.01)
    try:
        await AiocqhttpMessageEvent.send_message(bot,
            MessageChain([Image.fromBytes(b"fixture-image")]), is_group=True, session_id="100")
    except NetworkError as exc:
        assert utils.is_ambiguous_message_delivery_timeout(exc)
    else:
        raise AssertionError("Expected a receipt timeout")
    assert len(written) == 1 and written[0]["action"] == "send_group_msg"
    assert written[0]["params"]["message"][0]["type"] == "image"
asyncio.run(main())
'''
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-c", code, str(ROOT / "utils.py")], capture_output=True,
            timeout=30, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))

    async def test_task_write_failure_keeps_in_memory_outcome_and_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = _load_module("task_manager").GenerationTaskManager(pathlib.Path(directory), {})
            await manager.initialize()
            request = dict(request_id="msg-1", session_id="qq:GroupMessage:100", user_id="1001",
                group_id="100", task_type="文生图", prompt="cat", preset="自定义", requested_model="model", images=[])
            task, _ = await manager.begin_task(**request)
            self.plugin.task_mgr = manager
            with patch.object(manager, "_save_locked", new=AsyncMock(side_effect=OSError("disk full"))):
                await self.plugin._finalize_generated_delivery(task["id"], "record-1", "possibly_sent")
            self.assertEqual((await manager.get_task(task["id"]))["status"], "possibly_sent")
            _, duplicate = await manager.begin_task(**request)
            self.assertTrue(duplicate)
            await manager.close()


class DeliveryTimeoutClassificationTest(unittest.TestCase):
    def test_only_known_post_write_receipt_timeouts_are_ambiguous(self):
        classify = _load_module("utils").is_ambiguous_message_delivery_timeout
        for error in (NetworkError("WebSocket API call timeout"),
            ActionFailed({"retcode": 1200, "message": "Timeout: sendMsg onMsgInfoListUpdate"}),
            "timeout waiting for send_group_msg", "timeout waiting for send_private_msg"):
            with self.subTest(error=error):
                self.assertTrue(classify(error))
        for error in (NetworkError("HTTP request failed"), ApiNotAvailable(), TimeoutError(),
            RuntimeError("WebSocket API call timeout"), "provider timeout",
            ActionFailed({"retcode": 1200, "message": "sendMsg permission denied"})):
            with self.subTest(error=error):
                self.assertFalse(classify(error))


class DashboardResendDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = _load_module("dashboard_api", with_quart=True)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "cached.png"
        self.path.write_bytes(image_bytes())
        self.task = {"id": "task-1", "result_record_id": "record-1", "status": "send_failed",
            "session_id": "qq:GroupMessage:100", "channel_id": "m", "actual_model": "model-2", "duration": 42}
        self.plugin = delivery_harness()()
        self.plugin.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin.data_mgr = types.SimpleNamespace(get_generation_record=AsyncMock(return_value={"id": "record-1"}),
            get_generation_image_path=lambda record: self.path, update_generation_record_delivery=AsyncMock())
        self.plugin.task_mgr = types.SimpleNamespace(get_task=AsyncMock(return_value=self.task),
            update_task=AsyncMock(return_value=None), finish_success=AsyncMock(), finish_failure=AsyncMock())
        self.api = self.module.LinghuiDashboardApi(self.plugin)
        self.api._json_body = AsyncMock(return_value={"action": "resend", "id": "task-1"})
        self.api._public_task = lambda task: task
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda payload: payload)
        json_patch.start()
        self.addCleanup(json_patch.stop)

    async def test_resend_timeout_is_pending_delivery_and_preserves_route(self):
        self.plugin.context.send_message.side_effect = NetworkError("WebSocket API call timeout")
        result = await self.api.generation_task()
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["status"], "possibly_sent")
        self.plugin.context.send_message.assert_awaited_once()
        self.assertEqual(result["task"]["channel_id"], "m")
        self.assertEqual(result["task"]["actual_model"], "model-2")
        self.assertEqual(result["task"]["duration"], 42)
        self.plugin.task_mgr.finish_success.assert_not_awaited()

    async def test_resend_storage_errors_do_not_fail_the_http_response(self):
        self.plugin.task_mgr.update_task.side_effect = OSError("disk full")
        self.plugin.data_mgr.update_generation_record_delivery.side_effect = OSError("disk full")
        result = await self.api.generation_task()
        self.assertTrue(result["success"])
        self.assertEqual(result["task"]["status"], "succeeded")
        self.plugin.context.send_message.assert_awaited_once()

    async def test_missing_platform_or_connection_failure_is_not_success(self):
        for error in (False, NetworkError("HTTP request failed"), ApiNotAvailable()):
            with self.subTest(error=error):
                self.plugin.context.send_message.return_value = error
                self.plugin.context.send_message.side_effect = error if isinstance(error, Exception) else None
                response, status = await self.api.generation_task()
                self.assertFalse(response["success"])
                self.assertEqual(status, 502)
        self.plugin.task_mgr.finish_success.assert_not_awaited()
        self.plugin.task_mgr.update_task.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

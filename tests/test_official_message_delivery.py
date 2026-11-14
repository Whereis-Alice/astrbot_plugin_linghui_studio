"""Verify that QQ native Markdown receipts cannot break delivered image tasks."""

import ast
import asyncio
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock

from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_linghui_features import _load_module
from test_message_delivery import delivery_harness


ROOT = Path(__file__).resolve().parents[1]
presentation = _load_module("message_presentation")


class OfficialResultDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = delivery_harness()()
        self.plugin.conf = {}
        self.plugin._decorate_requester = lambda event, chain: chain
        self.event = SimpleNamespace(
            get_platform_name=lambda: "qq_official",
            get_platform_id=lambda: "leaf",
            send=AsyncMock(),
        )
        self.chain = MessageChain([
            Image.fromBytes(b"gif-original"),
            Plain("✅ 生成成功 (42.59s) | 预设: 动图画 | 剩余: 额度豁免\n用户: 0 | 群组: 0"),
        ])

    async def test_official_sends_original_media_then_native_markdown_receipt(self):
        self.assertEqual(await self.plugin._send_generated_result(self.event, self.chain), "sent")
        media, receipt = [call.args[0] for call in self.event.send.await_args_list]
        self.assertEqual(media.chain, [self.chain.chain[0]])
        self.assertFalse(media.use_markdown_)
        self.assertTrue(receipt.use_markdown_)
        self.assertIn("### ✅ 生成成功", receipt.chain[-1].text)
        self.assertIn("**预设：** 动图画", receipt.chain[-1].text)
        self.assertEqual(len(self.chain.chain), 2)

    async def test_receipt_rejection_or_cancellation_keeps_media_success_without_retry(self):
        for error in (RuntimeError("caption denied"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.event.send = AsyncMock(side_effect=[None, error])
                self.assertEqual(await self.plugin._send_generated_result(self.event, self.chain), "sent")
                self.assertEqual(self.event.send.await_count, 2)
                self.assertEqual(sum(any(isinstance(node, Image) for node in call.args[0].chain)
                    for call in self.event.send.await_args_list), 1)

    async def test_media_rejection_does_not_send_a_success_receipt(self):
        self.event.send.side_effect = RuntimeError("upload denied")
        with self.assertRaisesRegex(RuntimeError, "upload denied"):
            await self.plugin._send_generated_result(self.event, self.chain)
        self.event.send.assert_awaited_once()

    async def test_dashboard_resend_uses_official_platform_and_same_split(self):
        self.plugin.context = SimpleNamespace(
            get_platform_inst=lambda _: SimpleNamespace(meta=lambda: SimpleNamespace(name="qq_official")),
            send_message=AsyncMock(return_value=True),
        )
        self.assertEqual(await self.plugin._send_generated_to_session("leaf:GroupMessage:room", self.chain), "sent")
        sent = self.plugin.context.send_message.await_args_list
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0].args[0], "leaf:GroupMessage:room")
        self.assertTrue(sent[1].args[1].use_markdown_)


class OfficialCommandPresentationTest(unittest.IsolatedAsyncioTestCase):
    async def test_official_help_uses_text_instead_of_unsupported_merged_forward(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_get_help_node")
        namespace = {"Plain": Plain, "QQ_OFFICIAL_PLATFORMS": presentation.QQ_OFFICIAL_PLATFORMS}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "main.py", "exec"), namespace)
        plugin = SimpleNamespace(conf={"help_text": "灵绘帮助"}, _command_example=lambda *args: "#预设列表")
        event = SimpleNamespace(get_platform_name=lambda: "qq_official", chain_result=MessageChain)
        result = namespace["_get_help_node"](plugin, event)
        self.assertIsInstance(result.chain[0], Plain)
        self.assertIn("灵绘帮助", result.chain[0].text)
        self.assertTrue(presentation.prepare_outgoing_messages(result, event=event)[0].use_markdown_)

    async def test_command_wrapper_formats_only_its_own_official_replies(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        methods = [node for node in source.body if isinstance(node, ast.FunctionDef)
            and node.name in {"_present_command_results", "_direct_command_only"}]
        namespace = {"wraps": wraps, "AstrMessageEvent": Any,
            "prepare_outgoing_messages": presentation.prepare_outgoing_messages}
        exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), "main.py", "exec"), namespace)

        async def command(plugin, event):
            yield MessageChain([Plain("❌ 没弄好：\n• orb：请求超时")])

        wrapped = namespace["_direct_command_only"](command)
        plugin = SimpleNamespace(_direct_commands_enabled=lambda: True)
        for platform in ("qq_official", "aiocqhttp"):
            event = SimpleNamespace(get_platform_name=lambda: platform)
            results = [result async for result in wrapped(plugin, event)]
            self.assertEqual(len(results), 1)
            self.assertEqual(bool(results[0].use_markdown_), platform == "qq_official")
        plugin._direct_commands_enabled = lambda: False
        self.assertEqual([result async for result in wrapped(plugin, event)], [])


if __name__ == "__main__":
    unittest.main()

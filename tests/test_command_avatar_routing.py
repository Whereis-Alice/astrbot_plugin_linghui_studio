"""Exercise real command methods with controlled generation boundaries."""
import ast
import asyncio
import pathlib
import re
import types
import unittest
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

from astrbot.core.message.components import At, Plain
from test_image_manager_mentions import load_image_manager
from test_linghui_features import _load_module

ROOT = pathlib.Path(__file__).resolve().parents[1]


def command_harness():
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
    names = {"_wake_prefix_candidates", "_wake_prefix_display", "_strip_command_marker", "_command_namespace",
        "_direct_commands_enabled", "_namespaced_command_text", "_command_args", "_command_example",
        "_resolve_known_command", "on_figurine_request"}
    methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for method in methods:
        method.decorator_list = []
    utils = _load_module("utils")
    policy = _load_module("channel_policy")
    namespace = {"AstrMessageEvent": Any, "Optional": Optional, "List": List, "Tuple": Tuple,
        "re": re, "Plain": Plain, "norm_id": str, "extract_channel_option": policy.extract_channel_option}
    for name in ("normalize_wake_prefixes", "pick_display_wake_prefix", "strip_command_marker", "is_custom_drawing_command"):
        namespace[name] = getattr(utils, name)
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Harness"]


class StopBeforeGeneration(Exception):
    pass


class CommandAvatarRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = command_harness()()
        self.plugin.conf = {"prefix": True, "extra_prefix": "画"}
        self.plugin.context = types.SimpleNamespace(get_config=lambda umo=None: {"wake_prefix": ["#"]})
        self.plugin._COMMAND_ROUTES = []
        self.plugin.img_mgr = load_image_manager()({})
        self.plugin._is_message_processed = lambda key: False
        self.plugin._check_command_image_cooldown = lambda uid, gid: (False, 0)
        self.plugin._check_quota = AsyncMock(return_value={"allowed": True})
        self.plugin._charge_quota = AsyncMock()
        self.plugin._get_bot_id = lambda event: "9000"
        self.plugin._remember_session_image_context = AsyncMock(side_effect=StopBeforeGeneration)
        self.plugin.img_mgr.get_avatar = AsyncMock(return_value=b"avatar")

    def event(self, text, chain):
        return types.SimpleNamespace(message_str=text, message_obj=types.SimpleNamespace(message=chain, message_id="test"),
            unified_msg_origin="qq:GroupMessage:100", is_at_or_wake_command=True,
            get_sender_id=lambda: "1001", get_group_id=lambda: "100",
            stop_event=Mock(), send=AsyncMock(), chain_result=lambda nodes: nodes)

    async def test_command_bot_reference_uses_avatar_after_wake_mention(self):
        event = self.event("bnn @9000 合影", [At(qq="9000"), Plain(text="#bnn "), At(qq="9000"), Plain(text="合影")])
        with self.assertRaises(StopBeforeGeneration):
            async for _ in self.plugin.on_figurine_request(event):
                pass
        self.plugin.img_mgr.get_avatar.assert_awaited_once_with("9000")
        self.plugin._remember_session_image_context.assert_awaited_once()

    async def test_failed_avatar_aborts_before_generation_and_charge(self):
        self.plugin.img_mgr.get_avatar.return_value = None
        event = self.event("bnn 合影", [Plain(text="bnn "), At(qq="9000"), Plain(text="合影")])
        results = [result async for result in self.plugin.on_figurine_request(event)]
        self.assertIn("QQ 头像获取失败：9000", results[0][0].text)
        self.assertIn("未扣除次数", results[0][0].text)
        self.plugin._charge_quota.assert_not_awaited()
        self.plugin._remember_session_image_context.assert_not_awaited()

    async def test_group_requires_core_wake_flag_but_private_wake_needs_no_slash(self):
        event = self.event("bnn 猫", [Plain(text="bnn "), At(qq="9000"), Plain(text="猫")])
        event.is_at_or_wake_command = False
        self.assertEqual([item async for item in self.plugin.on_figurine_request(event)], [])
        self.plugin.img_mgr.get_avatar.assert_not_awaited()
        event.is_at_or_wake_command = True  # AstrBot sets this for default private-chat waking.
        event.get_group_id = lambda: ""
        with self.assertRaises(StopBeforeGeneration):
            async for _ in self.plugin.on_figurine_request(event):
                pass
        self.plugin.img_mgr.get_avatar.assert_awaited_once_with("9000")

    async def test_session_prefix_and_namespace_are_used_by_drawing_entry(self):
        self.plugin.conf["command_namespace"] = "绘图"
        self.plugin.conf["enable_direct_commands"] = False
        self.plugin.context.get_config = lambda umo=None: {"wake_prefix": ["喂"] if umo else ["/"]}
        event = self.event("绘图 bnn 合影", [Plain(text="喂绘图 bnn "), At(qq="9000"), Plain(text="合影")])
        with self.assertRaises(StopBeforeGeneration):
            async for _ in self.plugin.on_figurine_request(event):
                pass
        self.plugin.img_mgr.get_avatar.assert_awaited_once_with("9000")
        self.assertEqual(self.plugin._command_example("bnn", event), "喂绘图 bnn")
        self.assertEqual(self.plugin._strip_command_marker("/bnn 猫", event), "/bnn 猫")

    def test_legacy_context_api_can_still_read_default_prefix(self):
        self.plugin.context.get_config = lambda: {"wake_prefix": ["!"]}
        self.assertEqual(self.plugin._strip_command_marker("!bnn 猫", self.event("", [])), "bnn 猫")


if __name__ == "__main__":
    unittest.main()

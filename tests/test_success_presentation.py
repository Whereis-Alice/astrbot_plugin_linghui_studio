"""Success receipts must show the completed route and concise quota details."""

import ast
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock

import test_message_delivery as delivery_tests
from test_linghui_features import _load_module


ROOT = Path(__file__).resolve().parents[1]
format_success = _load_module("success_presentation").format_generation_success


class SuccessPresentationTest(unittest.TestCase):
    def test_exempt_receipt_matches_requested_two_lines(self):
        receipt = format_success(
            "42.59s", "动图画",
            requester_prefix="[爱丽丝的巧克力蛋糕(2127074778)] ",
            quota_text="额度豁免\n用户: 0 | 群组: 0 | 全局总次数：不限（所有会话累计已用 213 次）",
        )
        self.assertEqual(receipt,
            "[爱丽丝的巧克力蛋糕(2127074778)] ✅ 生成成功 (42.59s) | 预设: 动图画 | 剩余: 额度豁免\n"
            "用户: 0 | 群组: 0 | 全局总次数：不限（所有会话累计已用 213 次）")

    def test_model_comes_from_completed_route_and_never_leaves_empty_separator(self):
        for metrics, requested, expected in (
            ({"model": "actual-route-model"}, "requested", "模型: actual-route-model"),
            ({}, "explicit-model", "模型: explicit-model"),
            ({"model": ""}, "", ""),
        ):
            with self.subTest(metrics=metrics, requested=requested):
                result = format_success("1s", "自定义", route_metrics=metrics,
                    requested_model=requested, show_model_info=True)
                self.assertFalse(result.endswith(" |"))
                self.assertFalse(result.endswith(" | "))
                if expected:
                    self.assertIn(expected, result)
                else:
                    self.assertNotIn("模型:", result)
                self.assertNotIn("requested", result)

    def test_model_disabled_and_short_rules_remain_clean(self):
        result = format_success("", "预设", route_metrics={"model": "private-model"},
            show_model_info=False, extra_rules="短规则", position="[1/2]", version=True)
        self.assertEqual(result, "✅ [1/2] 版本生成成功 | 预设: 预设 | 规则: 短规则")

    def test_quota_receipts_have_summary_and_detail_for_each_charge_source(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "LinghuiStudioPlugin")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_get_quota_str")
        namespace = {"Optional": delivery_tests.Optional, "norm_id": str}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
            str(ROOT / "main.py"), "exec"), namespace)
        plugin = types.SimpleNamespace(data_mgr=types.SimpleNamespace(
            get_user_count=lambda uid: 7, get_group_count=lambda gid: 12),
            _global_quota_text=lambda: "全局总次数：不限")
        for charge_source, expected in (("free", "额度豁免"), ("user", "个人额度 7"), ("group", "群组额度 12")):
            result = namespace["_get_quota_str"](plugin,
                {"source": charge_source, "exemption_reason": "管理员额度豁免"}, "u", "g")
            self.assertEqual(result, expected + "\n用户: 7 | 群组: 12 | 全局总次数：不限")
            self.assertNotIn("管理员", result)


class SuccessRouteIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delivery_tests.MessageDeliveryTest.setUp(self)
        self.plugin.conf.update(show_model_info=True, enable_preset_ref_images=False)
        self.plugin._snapshot_generation_route_metrics = lambda: {"model": "actual-route-model"}
        self.plugin._get_quota_str = lambda *args: "额度豁免\n用户: 0 | 群组: 0 | 全局总次数：不限"

    def sent_text(self):
        return "\n".join(node.text for call in self.event.send.call_args_list
            for node in call.args[0].chain if isinstance(node, delivery_tests.Plain))

    async def test_direct_command_model_toggle_uses_actual_model(self):
        self.assertEqual([result async for result in self.plugin.on_figurine_request(self.event)], [])
        self.assertIn("模型: actual-route-model | 剩余: 额度豁免\n用户:", self.sent_text())
        self.assertNotIn(" | \n", self.sent_text())

    async def test_background_uses_actual_model_instead_of_requested_override(self):
        result = await self.plugin._run_background_task(self.event, [], "cat", "自定义",
            {"allowed": True}, "1001", "100", 1, model_override="requested-model")
        self.assertEqual(result, (True, ""))
        self.assertIn("模型: actual-route-model", self.sent_text())
        self.assertNotIn("requested-model", self.sent_text())

    async def test_background_image_only_and_name_modes_stay_short(self):
        for mode in ("image_only", "name"):
            with self.subTest(mode=mode):
                self.event.send.reset_mock()
                self.plugin.conf.update(generation_execution_mode="background", background_result_text_mode=mode)
                result = await self.plugin._run_background_task(self.event, [], "cat", "自定义",
                    {"allowed": True}, "1001", "100", 1, hide_text=True, llm_tool_call=True)
                self.assertEqual(result, (True, ""))
                self.assertNotIn("模型:", self.sent_text())
                if mode == "name":
                    self.assertIn("✅ 图片完成", self.sent_text())
                else:
                    self.assertEqual(self.sent_text(), "")

    async def test_batch_success_entrypoints_honor_model_toggle(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "LinghuiStudioPlugin")
        names = {"_run_batch_text_to_image", "_run_batch_image_to_image", "_run_single_batch_task"}
        methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        namespace = dict(self.plugin._run_background_task.__globals__)
        exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
            str(ROOT / "main.py"), "exec"), namespace)
        for name in names:
            setattr(self.plugin, name, types.MethodType(namespace[name], self.plugin))
        self.plugin.api_mgr = types.SimpleNamespace(call_api=AsyncMock(return_value=delivery_tests.image_bytes()))
        self.plugin.img_mgr.proxy = None
        self.plugin._translate_error_to_chinese = str
        self.plugin._resolve_debug_error_message = lambda error, default: str(error)
        guard = types.SimpleNamespace(aborted=False, skipped=0, note_skip=AsyncMock(), note_failure=AsyncMock())
        self.plugin._make_batch_guard = lambda: guard
        self.plugin._finalize_batch_guard = AsyncMock(return_value="")
        for name in sorted(names):
            for show_model in (True, False):
                with self.subTest(entrypoint=name, show_model=show_model):
                    self.event.send.reset_mock()
                    self.plugin.conf["show_model_info"] = show_model
                    if name == "_run_batch_text_to_image":
                        result = await getattr(self.plugin, name)(self.event, "cat", "自定义", {}, "1001", "100", 1)
                    elif name == "_run_batch_image_to_image":
                        result = await getattr(self.plugin, name)(self.event, [], "cat", "自定义", {}, "1001", "100", 1)
                    else:
                        result = await getattr(self.plugin, name)(self.event, b"ref", "cat", "自定义", 1, 1, "1001", "100")
                    if isinstance(result, dict):
                        self.assertEqual(result["success"], 1, result)
                    else:
                        self.assertEqual(result, (True, ""))
                    if show_model:
                        self.assertIn("模型: actual-route-model", self.sent_text())
                    else:
                        self.assertNotIn("模型:", self.sent_text())


if __name__ == "__main__":
    unittest.main()

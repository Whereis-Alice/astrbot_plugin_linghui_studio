"""Budget regressions: cross-session totals, concurrent spending and commands."""

import ast
import asyncio
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

from test_linghui_features import _load_module


ROOT = pathlib.Path(__file__).resolve().parents[1]


def command_harness(manager, *, admin=True):
    """Run the actual handlers without starting AstrBot's providers or adapters."""
    source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == "LinghuiStudioPlugin")
    names = {
        "on_global_quota", "on_add_global_quota", "_global_quota_text",
        "_quota_denied_tool_message", "_check_quota", "_charge_quota", "_refund_quota",
    }
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for method in methods:
        method.decorator_list = [ast.Name(id="staticmethod", ctx=ast.Load())] if method.name == "_quota_denied_tool_message" else []
    namespace = {
        "AstrMessageEvent": object, "Dict": dict, "Any": object,
        "Plain": str, "QuotaDeniedError": _load_module("quota").QuotaDeniedError,
        "logger": types.SimpleNamespace(info=lambda *a: None, exception=lambda *a: None),
    }
    code = ast.Module(body=[ast.ClassDef(name="Handlers", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(ROOT / "main.py"), "exec"), namespace)
    plugin = namespace["Handlers"]()
    plugin.data_mgr = manager
    plugin.conf = manager.config
    plugin.is_admin = lambda event: admin
    plugin._command_args = lambda event, *names: event.args
    plugin._save_config = lambda keys: plugin.saved_keys.extend(keys)
    plugin.saved_keys = []
    plugin._effective_identity = lambda event, uid, gid: (uid, gid)
    plugin.access_policy = _load_module("access_control").AccessPolicy(manager.config)
    return plugin


async def run_command(plugin, method, args):
    event = types.SimpleNamespace(args=args, chain_result=lambda nodes: "".join(nodes))
    return [result async for result in getattr(plugin, method)(event)]


class GlobalQuotaTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "global_total_limit": 5, "enable_user_limit": True, "enable_group_limit": True,
            "group_access_mode": "all", "allow_private_messages": True,
        }
        self.manager = _load_module("data_manager").DataManager(pathlib.Path(self.temp.name), self.config)
        await self.manager.initialize()
        await self.manager.add_user_count("alice", 20)
        await self.manager.add_group_count("group-b", 20)

    async def test_all_groups_private_chats_and_exempt_users_share_one_cap(self):
        results = [
            await self.manager.consume_quota("alice", "group-a", 2),
            await self.manager.consume_quota("bob", "group-b", 1),
            await self.manager.consume_quota("alice", "", 1),
            await self.manager.consume_quota("admin", "group-c", 1, unlimited=True),
        ]
        self.assertEqual([r["source"] for r in results], ["user", "group", "user", "free"])
        denied = await self.manager.consume_quota("admin", "", 1, unlimited=True)
        self.assertFalse(denied["allowed"])
        self.assertIn("全局剩余 0", denied["msg"])
        self.assertEqual(self.manager.get_user_count("alice"), 17)
        self.assertEqual(self.manager.get_group_count("group-b"), 19)

    async def test_concurrent_sessions_cannot_spend_more_than_the_global_cap(self):
        results = await asyncio.gather(*(
            self.manager.consume_quota(f"user-{i}", f"group-{i}", 1, unlimited=True)
            for i in range(30)
        ))
        self.assertEqual(sum(r["allowed"] for r in results), 5)
        self.assertEqual(self.manager.get_global_quota()["used"], 5)

    async def test_concurrent_spending_also_rechecks_personal_balance(self):
        self.config["global_total_limit"] = 100
        await self.manager.set_user_count("alice", 2)
        results = await asyncio.gather(*(self.manager.consume_quota("alice", "", 1) for _ in range(20)))
        self.assertEqual(sum(r["allowed"] for r in results), 2)
        self.assertEqual(self.manager.get_user_count("alice"), 0)
        self.assertEqual(self.manager.get_global_quota()["used"], 2)

    async def test_batch_larger_than_remaining_budget_is_rejected_without_local_debit(self):
        await self.manager.consume_quota("alice", "", 3)
        result = await self.manager.consume_quota("bob", "group-b", 3)
        self.assertFalse(result["allowed"])
        self.assertIn("本次需要 3", result["msg"])
        self.assertIn("全局剩余 2", result["msg"])
        self.assertEqual(self.manager.get_group_count("group-b"), 20)
        self.assertEqual(self.manager.get_global_quota()["used"], 3)

    async def test_restart_restores_usage_and_limit_changes_do_not_reset_it(self):
        await self.manager.consume_quota("alice", "", 3)
        await self.manager.adjust_global_limit(2)
        reopened = type(self.manager)(pathlib.Path(self.temp.name), self.config)
        await reopened.initialize()
        self.assertFalse(reopened.check_quota("alice", "", 1)["allowed"])
        await reopened.adjust_global_limit(5, add=True)
        self.assertEqual(reopened.get_global_quota(), {"enabled": True, "limit": 7, "used": 3, "remaining": 4})

    async def test_refund_restores_total_and_local_credits_only_once(self):
        receipt = await self.manager.consume_quota("alice", "", 3)
        await asyncio.gather(*(self.manager.refund_quota(receipt, "alice", "", 3) for _ in range(5)))
        self.assertEqual(self.manager.get_global_quota()["used"], 0)
        self.assertEqual(self.manager.get_user_count("alice"), 20)

    async def test_partial_batch_and_exempt_user_refunds_restore_shared_budget(self):
        receipt = await self.manager.consume_quota("admin", "", 4, unlimited=True)
        await self.manager.refund_quota(receipt, "admin", "", 2)
        self.assertEqual(self.manager.get_global_quota()["remaining"], 3)
        self.assertEqual(receipt["charged"], 2)
        self.assertEqual(self.manager.get_user_count("admin"), 0)

    async def test_signin_and_local_credit_topups_do_not_refill_global_budget(self):
        await self.manager.consume_quota("alice", "", 5)
        await self.manager.process_checkin("alice")
        await self.manager.add_group_count("group-b", 50)
        self.assertFalse(self.manager.check_quota("alice", "group-b", 1)["allowed"])

    async def test_unlimited_mode_still_tracks_usage_and_zero_pauses_drawing(self):
        self.config.update(global_total_limit=-1, enable_user_limit=False, enable_group_limit=False)
        await self.manager.consume_quota("anyone", "", 8)
        await self.manager.adjust_global_limit(0)
        self.assertEqual(self.manager.get_global_quota()["used"], 8)
        self.assertFalse(self.manager.check_quota("anyone", "", 1)["allowed"])

    async def test_counter_write_failure_prevents_a_debit(self):
        with patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.manager.consume_quota("alice", "", 1)
        self.assertEqual(self.manager.get_global_quota()["used"], 0)
        self.assertEqual(self.manager.get_user_count("alice"), 20)

    async def test_corrupt_counter_fails_closed(self):
        self.manager.global_quota_file.write_text('{"used": -1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            await type(self.manager)(pathlib.Path(self.temp.name), self.config).initialize()

    async def test_non_admin_can_query_but_cannot_adjust(self):
        plugin = command_harness(self.manager, admin=False)
        self.assertIn("剩余 5", (await run_command(plugin, "on_global_quota", ""))[0])
        for method, args in [("on_global_quota", "100"), ("on_add_global_quota", "50")]:
            self.assertIn("只有 AstrBot 管理员", (await run_command(plugin, method, args))[0])
        self.assertEqual(self.config["global_total_limit"], 5)
        self.assertEqual(plugin.saved_keys, [])

    async def test_admin_can_set_add_and_disable_using_plain_commands(self):
        plugin = command_harness(self.manager)
        await self.manager.consume_quota("alice", "", 2)
        await run_command(plugin, "on_global_quota", "100")
        await run_command(plugin, "on_add_global_quota", "50")
        self.assertEqual(self.config["global_total_limit"], 150)
        self.assertEqual(self.manager.get_global_quota()["used"], 2)
        await run_command(plugin, "on_global_quota", "不限")
        self.assertEqual(self.config["global_total_limit"], -1)
        self.assertEqual(plugin.saved_keys, ["global_total_limit"] * 3)
        self.assertIn("请先", (await run_command(plugin, "on_add_global_quota", "10"))[0])

    async def test_invalid_commands_keep_the_existing_limit(self):
        plugin = command_harness(self.manager)
        for args in ["-2", "1.5", "abc", "50 extra"]:
            self.assertIn("用法", (await run_command(plugin, "on_global_quota", args))[0])
        for args in ["0", "-1", "1.5", ""]:
            self.assertIn("用法", (await run_command(plugin, "on_add_global_quota", args))[0])
        self.assertEqual(self.config["global_total_limit"], 5)

    async def test_access_and_exemption_do_not_bypass_the_cap_or_permission(self):
        plugin = command_harness(self.manager)
        self.config["global_total_limit"] = 0
        denied = await plugin._check_quota(object(), "admin", "group-a", 1)
        self.assertEqual(denied["access_level"], "global_quota_exhausted")
        self.assertIn("全局剩余 0", plugin._quota_denied_tool_message(denied))
        self.config["blocked_users"] = ["admin"]
        denied = await plugin._check_quota(object(), "admin", "group-a", 1)
        self.assertEqual(denied["access_level"], "blocked_user")

    async def test_late_charge_rejection_keeps_actionable_failure_message(self):
        plugin = command_harness(self.manager)
        receipt = await plugin._check_quota(object(), "alice", "", 1)
        await self.manager.consume_quota("other", "", 5, unlimited=True)
        with self.assertRaises(_load_module("quota").QuotaDeniedError):
            await plugin._charge_quota(receipt, "alice", "", 1)
        self.assertIn("全局剩余 0", plugin._quota_denied_tool_message(receipt))

    async def test_dashboard_saves_the_same_limit_and_returns_shared_usage(self):
        module = _load_module("dashboard_api", with_quart=True)
        saved = []
        plugin = types.SimpleNamespace(
            conf=self.config, data_mgr=self.manager,
            _save_config=lambda changed_keys=None: saved.append(changed_keys),
            _load_persona_scenes=lambda: None,
        )
        api = module.LinghuiDashboardApi(plugin)

        async def body():
            return {"usage": {"global_total_limit": 10}}

        api._json_body = body
        await self.manager.consume_quota("alice", "", 2)
        response = await api.save_config()
        self.assertTrue(response["success"])
        self.assertEqual(saved, [["global_total_limit"]])
        self.assertEqual(self.config["global_total_limit"], 10)
        response = await api.get_usage()
        self.assertEqual(response["global_quota"]["used"], 2)
        self.assertEqual(response["global_quota"]["remaining"], 8)

    async def test_dashboard_rejects_invalid_limit_before_changing_other_settings(self):
        module = _load_module("dashboard_api", with_quart=True)
        api = module.LinghuiDashboardApi(types.SimpleNamespace(conf=self.config, data_mgr=self.manager))
        for value in [-2, 1.5, True, "3"]:
            async def body():
                return {"settings": {"model": "changed"}, "usage": {"global_total_limit": value}}
            api._json_body = body
            response, status = await api.save_config()
            self.assertEqual(status, 400)
            self.assertNotIn("model", self.config)
            self.assertEqual(self.config["global_total_limit"], 5)


if __name__ == "__main__":
    unittest.main()

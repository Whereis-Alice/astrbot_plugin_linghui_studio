"""Cross-platform current-room authorization without numeric-ID assumptions."""

import ast
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any
from typing import Dict, List, Optional
import tempfile
import unittest
from unittest.mock import Mock

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from test_linghui_features import _load_module


ROOT = Path(__file__).resolve().parents[1]
access = _load_module("access_control")
session_key = _load_module("access_session").access_session_key


def event(*, platform="official-leaf", uid="user_OpenID", gid="group_OpenID", isolated=False,
          admin=True, args="", message_type=None):
    kind = message_type or (MessageType.GROUP_MESSAGE if gid else MessageType.FRIEND_MESSAGE)
    room = f"{uid}_{gid}" if isolated else gid or uid
    return SimpleNamespace(
        args=args, unified_msg_origin=f"{platform}:{kind.value}:{room}",
        get_platform_id=lambda: platform, get_message_type=lambda: kind,
        get_sender_id=lambda: uid, get_group_id=lambda: gid,
        is_admin=lambda: admin, chain_result=MessageChain,
    )


class SessionAccessPolicyTest(unittest.TestCase):
    def test_command_whitelist_survives_native_save_failure_and_can_restore_on_reload(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=="LinghuiStudioPlugin")
        methods = [n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in {
            "_save_config", "_build_dynamic_config_backup", "_write_dynamic_config_backup"}]
        dynamic_keys = ast.literal_eval(next(n.value for n in cls.body if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=="_DYNAMIC_CONFIG_KEYS" for t in n.targets)))
        self.assertIn("group_whitelist",dynamic_keys)
        persistence = _load_module("config_persistence")
        class Config(dict):
            def save_config(self):
                raise OSError("native save fixture failure")
        with tempfile.TemporaryDirectory() as directory:
            namespace={"Optional":Optional,"List":List,"Dict":Dict,"Any":Any,"datetime":__import__("datetime").datetime,
                "StarTools":SimpleNamespace(get_data_dir=lambda:Path(directory)),"logger":Mock(),
                "asyncio":__import__("asyncio"),"json":json,"merge_native_fallback_snapshots":persistence.merge_native_fallback_snapshots}
            tree=ast.Module(body=[ast.ClassDef(name="Harness",bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
            exec(compile(ast.fix_missing_locations(tree),"main.py","exec"),namespace)
            plugin=namespace["Harness"]()
            plugin._DYNAMIC_CONFIG_KEYS=dynamic_keys
            for attr,value in (("_DYNAMIC_CONFIG_META_KEY","__dynamic_overrides__"),
                    ("_DYNAMIC_CONFIG_NATIVE_SNAPSHOT_KEY","__native_values_at_fallback__"),
                    ("_DYNAMIC_CONFIG_VERSION_KEY","__dynamic_config_version__"),
                    ("_DYNAMIC_CONFIG_UPDATED_AT_KEY","__dynamic_updated_at__"),
                    ("_DYNAMIC_CONFIG_VERSION",6)):
                setattr(plugin,attr,value)
            key="leaf:GroupMessage:opaque_OpenID"
            plugin.conf=Config(group_whitelist=[key])
            plugin._purge_deprecated_config_keys=Mock()
            plugin._read_native_config_values=lambda _: {"group_whitelist":[]}
            self.assertTrue(plugin._save_config(["group_whitelist"]),namespace["logger"].error.call_args)
            saved=json.loads((Path(directory)/"dynamic_config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["group_whitelist"],[key])
            self.assertIn("group_whitelist",saved["__dynamic_overrides__"])
            restore=persistence.should_restore_fallback_value("group_whitelist",saved["group_whitelist"],
                dynamic_keys=dynamic_keys,legacy_restore_keys=set(),current_config={"group_whitelist":[]},
                has_override_meta=True,schema_defaults={"group_whitelist":[]},has_native_snapshot=True,
                native_value_at_fallback=saved["__native_values_at_fallback__"]["group_whitelist"])
            self.assertEqual(restore,(True,"fallback-value"))

    def test_all_platform_room_ids_and_isolated_members_use_same_key(self):
        for platform,room in (("qq", "12345"), ("official-leaf", "AB_C-D_OpenID"),
                              ("discord", "98765"), ("telegram", "-100123"),
                              ("slack", "C123-opaque"), ("matrix", "!abc:server")):
            with self.subTest(platform=platform):
                first = event(platform=platform, gid=room, isolated=True)
                other = event(platform=platform, gid=room, uid="different-member", isolated=True)
                key = session_key(first, room)
                self.assertEqual(key, session_key(other, room))
                policy = access.AccessPolicy({"group_whitelist": [key], "admins_unlimited": False})
                allowed = policy.evaluate("different-member", room, session_id=session_key(other, room))
                self.assertTrue(allowed.allowed)
                self.assertFalse(allowed.unlimited)
                self.assertFalse(policy.evaluate("different-member", room,
                    session_id=session_key(event(platform=platform+"-other",gid=room),room)).allowed)

    def test_private_grant_does_not_open_other_private_sessions(self):
        dm = event(gid="")
        key = session_key(dm)
        policy = access.AccessPolicy({"allow_private_messages": False, "group_whitelist": [key]})
        self.assertTrue(policy.evaluate("user_OpenID", "", session_id=key).allowed)
        for other in (event(gid="", uid="another"), event(gid="",platform="other-bot")):
            self.assertFalse(policy.evaluate(other.get_sender_id(), "", session_id=session_key(other)).allowed)
        self.assertFalse(policy.config["allow_private_messages"])

    def test_blacklists_user_whitelist_and_quotas_keep_priority(self):
        key = session_key(event(), "group_OpenID")
        for setting,level in (({"blocked_users": ["user_OpenID"]}, "blocked_user"),
                              ({"group_blacklist": ["group_OpenID"]}, "blocked_group"),
                              ({"allowed_users": ["old-qq-number"]}, "user_not_allowed")):
            policy = access.AccessPolicy({"group_whitelist": [key], **setting})
            self.assertEqual(policy.evaluate("user_OpenID", "group_OpenID", session_id=key).level, level)
        self.assertFalse(access.AccessPolicy({"group_whitelist": [key]}).evaluate(
            "user_OpenID", "group_OpenID", session_id=key).unlimited)
        self.assertTrue(access.AccessPolicy({"group_whitelist": ["legacy-group"]}).evaluate("uid","legacy-group").allowed)

    def test_missing_identity_cannot_make_an_empty_grant(self):
        self.assertEqual(session_key(SimpleNamespace()), "")
        self.assertFalse(access.AccessPolicy({"group_whitelist": [""]}).evaluate("u","g").allowed)


class SessionWhitelistCommandTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self, config=None):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=="LinghuiStudioPlugin")
        methods = [n for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
            and n.name in {"on_session_whitelist", "is_admin"}]
        for method in methods:
            method.decorator_list=[]
        namespace = {"AstrMessageEvent": Any,"Plain":Plain,"re":re,
            "access_session_key":session_key,"norm_id":str}
        exec(compile(ast.fix_missing_locations(ast.Module(body=methods,type_ignores=[])),"main.py","exec"),namespace)
        plugin = SimpleNamespace(conf=config if config is not None else {})
        plugin.access_policy=access.AccessPolicy(plugin.conf)
        plugin._command_args=lambda ev,*args:ev.args
        plugin._effective_identity=lambda ev:(ev.get_sender_id(),ev.get_group_id())
        plugin._save_config=Mock(return_value=True)
        plugin.context=SimpleNamespace(get_config=lambda:{"admins_id":["user_OpenID"]})
        plugin.is_admin=lambda ev:namespace["is_admin"](plugin,ev)
        plugin.run=lambda ev:namespace["on_session_whitelist"](plugin,ev)
        return plugin

    async def output(self, plugin, ev):
        return "\n".join(node.text for result in [r async for r in plugin.run(ev)] for node in result.chain)

    async def test_add_is_idempotent_and_persisted_policy_works_for_other_member(self):
        plugin=self.plugin({"group_whitelist":["old-numeric-id"]})
        text=await self.output(plugin,event(isolated=True))
        self.assertIn("已加入白名单",text)
        self.assertEqual(plugin.conf["group_whitelist"],["old-numeric-id","official-leaf:GroupMessage:group_OpenID"])
        plugin._save_config.assert_called_once_with(["group_whitelist"])
        await self.output(plugin,event(isolated=True))
        plugin._save_config.assert_called_once()
        restored=access.AccessPolicy(json.loads(json.dumps(plugin.conf)))
        self.assertTrue(restored.evaluate("other-member","group_OpenID",session_id="official-leaf:GroupMessage:group_OpenID").allowed)

    async def test_non_admin_cannot_self_grant_even_if_old_config_lists_them(self):
        plugin=self.plugin()
        text=await self.output(plugin,event(admin=False))
        self.assertIn("user_OpenID",text)
        self.assertIn("只有 AstrBot 管理员",text)
        self.assertNotIn("group_whitelist",plugin.conf)
        plugin._save_config.assert_not_called()

    async def test_core_admin_role_authorizes_even_when_default_profile_does_not(self):
        plugin=self.plugin()
        plugin.context=SimpleNamespace(get_config=lambda:{"admins_id":["old-qq-number"]})
        self.assertIn("已加入白名单",await self.output(plugin,event(admin=True)))

    async def test_private_add_and_view_do_not_change_global_private_switch(self):
        plugin=self.plugin({"allow_private_messages":False})
        self.assertIn("仅对这一个私聊",await self.output(plugin,event(gid="")))
        self.assertFalse(plugin.conf["allow_private_messages"])
        plugin._save_config.reset_mock()
        self.assertIn("已加入",await self.output(plugin,event(gid="",args="查看",admin=False)))
        plugin._save_config.assert_not_called()

    async def test_remove_scoped_entry_retains_legacy_entry_and_explains_access(self):
        key="official-leaf:GroupMessage:group_OpenID"
        plugin=self.plugin({"group_whitelist":["group_OpenID",key]})
        text=await self.output(plugin,event(args="移除"))
        self.assertEqual(plugin.conf["group_whitelist"],["group_OpenID"])
        self.assertIn("原始 ID",text)
        self.assertIn("仍被其他访问设置放行",text)

    async def test_save_failure_rolls_back_and_remaining_restrictions_are_reported(self):
        plugin=self.plugin({"group_whitelist":["old"],"blocked_users":["user_OpenID"]})
        self.assertIn("当前访问状态",await self.output(plugin,event()))
        plugin=self.plugin({"group_whitelist":["old"]})
        plugin._save_config.return_value=False
        self.assertIn("保存失败",await self.output(plugin,event()))
        self.assertEqual(plugin.conf["group_whitelist"],["old"])


if __name__=="__main__":
    unittest.main()

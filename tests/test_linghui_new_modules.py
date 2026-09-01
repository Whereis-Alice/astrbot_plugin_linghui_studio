"""针对本轮新增模块的单元测试：响应解析 / 原生协议 / 会话覆盖 / 批量失败策略。"""

import asyncio
import base64
import importlib
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
import zipfile

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


def _load(name):
    _ensure_test_package()
    return importlib.import_module(f"{PACKAGE}.{name}")


def _png_bytes(color=(200, 40, 90), size=(8, 8)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _large_png_bytes():
    """够长的 PNG，保证 base64 编码后超过裸 base64 判定阈值。"""
    import random

    random.seed(7)
    image = Image.new("RGB", (48, 48))
    image.putdata([(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(48 * 48)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class ResponseParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("response_parsing")

    def test_raw_png_body_is_detected_as_binary_image(self):
        blob = _png_bytes()
        payload, text, kind = self.mod.parse_response_body(blob)
        self.assertEqual(kind, "binary")
        self.assertEqual(payload, blob)
        self.assertTrue(text.startswith("<binary "))

    def test_bare_base64_body_is_decoded_without_json_envelope(self):
        blob = base64.b64encode(_large_png_bytes())
        payload, _text, kind = self.mod.parse_response_body(blob)
        self.assertEqual(kind, "base64")
        self.assertTrue(self.mod.looks_like_binary_image(payload))

    def test_bare_base64_can_be_disabled(self):
        blob = base64.b64encode(_large_png_bytes())
        payload, text, kind = self.mod.parse_response_body(blob, allow_bare_base64=False)
        self.assertEqual(kind, "text")
        self.assertIsNone(payload)
        self.assertTrue(text)

    def test_json_body_is_returned_as_mapping(self):
        blob = json.dumps({"data": [{"b64_json": "abc"}]}).encode("utf-8")
        payload, _text, kind = self.mod.parse_response_body(blob)
        self.assertEqual(kind, "json")
        self.assertIsInstance(payload, dict)

    def test_zip_archive_yields_the_first_embedded_image(self):
        image = _png_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "hello")
            archive.writestr("out/image.png", image)
        payload, _text, kind = self.mod.parse_response_body(buffer.getvalue())
        self.assertEqual(kind, "binary")
        self.assertEqual(payload, image)

    def test_heartbeat_prefixed_stream_still_exposes_the_json_payload(self):
        body = b": ping\n\n" + b"\n" + json.dumps({"data": [{"url": "https://x/y.png"}]}).encode("utf-8")
        payload, _text, kind = self.mod.parse_response_body(body)
        self.assertIn(kind, {"json", "mixed"})
        self.assertIsInstance(payload, dict)

    def test_sse_stream_payloads_are_iterated_in_order(self):
        blob = b"data: {\"a\": 1}\n\ndata: {\"a\": 2}\n\ndata: [DONE]\n\n"
        self.assertTrue(self.mod.looks_like_sse(blob.decode("utf-8")))
        chunks = list(self.mod.iter_sse_payloads(blob.decode("utf-8")))
        self.assertEqual(len(chunks), 2)


class ProtocolAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("protocol_adapters")

    def test_auto_protocol_builds_no_native_request(self):
        self.assertIsNone(
            self.mod.build_protocol_request(
                self.mod.PROTOCOL_AUTO, base_url="", key="k", model="", prompt="x"
            )
        )

    def test_auto_is_not_treated_as_a_native_protocol(self):
        self.assertFalse(self.mod.is_native_protocol(self.mod.PROTOCOL_AUTO))
        for name in self.mod.NATIVE_PROTOCOLS:
            self.assertTrue(self.mod.is_native_protocol(name))

    def test_unknown_protocol_names_fall_back_to_auto(self):
        self.assertEqual(self.mod.normalize_protocol("nope"), self.mod.PROTOCOL_AUTO)
        self.assertEqual(self.mod.normalize_protocol(None), self.mod.PROTOCOL_AUTO)
        self.assertEqual(self.mod.normalize_protocol(" Grok "), "grok")

    def test_every_native_protocol_builds_a_request_with_url_and_payload(self):
        for name in self.mod.NATIVE_PROTOCOLS:
            with self.subTest(protocol=name):
                built = self.mod.build_protocol_request(
                    name,
                    base_url="",
                    key="test-key",
                    model="",
                    prompt="a red fox",
                    aspect_ratio="1:1",
                    resolution="1K",
                )
                self.assertIsNotNone(built)
                self.assertEqual(built.protocol, name)
                self.assertTrue(built.url.startswith("http"))
                self.assertIsInstance(built.headers, dict)
                self.assertTrue(built.json_body or built.params)
                self.assertTrue(built.model)

    def test_reference_image_support_is_reported_per_protocol(self):
        flags = {name: self.mod.protocol_supports_reference_images(name) for name in self.mod.NATIVE_PROTOCOLS}
        self.assertEqual(set(flags), set(self.mod.NATIVE_PROTOCOLS))
        self.assertTrue(any(flags.values()))


class SessionOverrideStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Store = _load("session_overrides").SessionOverrideStore

    def test_model_and_channel_overrides_round_trip_through_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "session_overrides.json")
            store = self.Store(path, ttl_minutes=60)
            store.set_model("group:1", "gpt-image-1", label="测试群", scope="group")
            store.set_channel("group:1", "miku", label="测试群", scope="group")
            reopened = self.Store(path, ttl_minutes=60)
            self.assertEqual(reopened.get_model("group:1"), "gpt-image-1")
            self.assertEqual(reopened.get_channel("group:1"), "miku")

    def test_empty_value_clears_only_that_field(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.Store(str(pathlib.Path(directory) / "s.json"))
            store.set_model("private:9", "model-a")
            store.set_channel("private:9", "chan-a")
            store.set_model("private:9", "")
            self.assertEqual(store.get_model("private:9"), "")
            self.assertEqual(store.get_channel("private:9"), "chan-a")
            store.set_channel("private:9", "")
            self.assertEqual(store.get("private:9"), {})

    def test_expired_entries_are_pruned_on_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.Store(str(pathlib.Path(directory) / "s.json"), ttl_minutes=1)
            store.set_model("group:2", "model-b")
            entry = store.get("group:2")
            self.assertTrue(entry)
            store._sessions["group:2"]["expires_at"] = 1.0
            self.assertEqual(store.get("group:2"), {})

    def test_list_all_reports_session_ids_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.Store(str(pathlib.Path(directory) / "s.json"))
            store.set_model("group:a", "m1")
            store.set_model("group:b", "m2")
            store._sessions["group:a"]["updated_at"] = 10.0
            store._sessions["group:b"]["updated_at"] = 20.0
            listed = [item["session_id"] for item in store.list_all()]
            self.assertEqual(listed[:2], ["group:b", "group:a"])

    def test_clear_all_removes_every_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.Store(str(pathlib.Path(directory) / "s.json"))
            store.set_model("group:a", "m1")
            store.set_channel("group:b", "c1")
            self.assertEqual(store.clear_all(), 2)
            self.assertEqual(store.list_all(), [])


class BatchFailurePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("batch_policy")

    def test_chinese_aliases_normalise_to_canonical_policies(self):
        self.assertEqual(self.mod.normalize_policy("立即停止"), self.mod.POLICY_STOP)
        self.assertEqual(self.mod.normalize_policy("跳过继续"), self.mod.POLICY_SKIP)
        self.assertEqual(self.mod.normalize_policy("限量跳过"), self.mod.POLICY_SKIP_LIMIT)
        self.assertEqual(self.mod.normalize_policy("garbage"), self.mod.POLICY_SKIP)

    def test_stop_policy_aborts_after_the_first_failure(self):
        guard = self.mod.BatchFailureGuard(self.mod.POLICY_STOP)
        self.assertEqual(asyncio.run(guard.note_failure("boom")), "abort")
        self.assertTrue(guard.aborted)
        self.assertIn("boom", guard.abort_reason)

    def test_skip_policy_never_aborts(self):
        guard = self.mod.BatchFailureGuard(self.mod.POLICY_SKIP)

        async def run():
            return [await guard.note_failure("e") for _ in range(5)]

        self.assertEqual(set(asyncio.run(run())), {"continue"})
        self.assertFalse(guard.aborted)

    def test_skip_limit_policy_aborts_once_the_budget_is_used(self):
        guard = self.mod.BatchFailureGuard(self.mod.POLICY_SKIP_LIMIT, max_skips=2)

        async def run():
            return [await guard.note_failure("e") for _ in range(3)]

        results = asyncio.run(run())
        self.assertEqual(results[-1], "abort")
        self.assertTrue(guard.aborted)

    def test_snapshot_and_summary_expose_skip_counters(self):
        guard = self.mod.BatchFailureGuard(self.mod.POLICY_SKIP)
        asyncio.run(guard.note_failure("e"))
        asyncio.run(guard.note_skip())
        snapshot = guard.snapshot()
        self.assertEqual(snapshot["failures"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertIsInstance(guard.describe(), str)
        self.assertIsInstance(guard.summary_suffix(), str)


class WakePrefixHelpersTests(unittest.TestCase):
    """唤醒前缀工具：示例命令必须跟随 AstrBot 实际配置的 wake_prefix。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load("utils")

    def test_normalize_accepts_string_and_iterables(self):
        self.assertEqual(self.mod.normalize_wake_prefixes("#"), ["#"])
        self.assertEqual(self.mod.normalize_wake_prefixes(("#", "/")), ["#", "/"])
        self.assertEqual(sorted(self.mod.normalize_wake_prefixes({"#", "/"})), ["#", "/"])

    def test_normalize_sorts_longest_first_and_dedupes(self):
        self.assertEqual(self.mod.normalize_wake_prefixes(["/", "//", "/"]), ["//", "/"])
        self.assertEqual(self.mod.normalize_wake_prefixes(["#", "  ", "", None, "#"]), ["#"])

    def test_normalize_rejects_unsupported_types(self):
        self.assertEqual(self.mod.normalize_wake_prefixes(None), [])
        self.assertEqual(self.mod.normalize_wake_prefixes(42), [])
        self.assertEqual(self.mod.normalize_wake_prefixes({"wake": "#"}), [])

    def test_display_prefix_prefers_hash_then_slash(self):
        self.assertEqual(self.mod.pick_display_wake_prefix(["，", "#"]), "#")
        self.assertEqual(self.mod.pick_display_wake_prefix(["，", "/"]), "/")
        self.assertEqual(self.mod.pick_display_wake_prefix(["，"]), "，")
        self.assertEqual(self.mod.pick_display_wake_prefix(["机器人", "喂"]), "喂")
        self.assertEqual(self.mod.pick_display_wake_prefix([]), "")

    def test_strip_marker_removes_configured_prefix(self):
        self.assertEqual(self.mod.strip_command_marker("，画 猫", ["，"]), "画 猫")
        self.assertEqual(self.mod.strip_command_marker("  ，画 猫  ", ["，"]), "画 猫")
        self.assertEqual(self.mod.strip_command_marker("//画 猫", ["/", "//"]), "画 猫")

    def test_strip_marker_keeps_unknown_prefix_and_handles_defaults(self):
        self.assertEqual(self.mod.strip_command_marker("，画 猫"), "，画 猫")
        self.assertEqual(self.mod.strip_command_marker("#画 猫"), "画 猫")
        self.assertEqual(self.mod.strip_command_marker("!!画 猫"), "画 猫")
        self.assertEqual(self.mod.strip_command_marker("画 猫", ["，"]), "画 猫")
        self.assertEqual(self.mod.strip_command_marker(None), "")


if __name__ == "__main__":
    unittest.main()

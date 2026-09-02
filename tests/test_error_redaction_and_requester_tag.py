import importlib
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_error_tag_test_package"


def load_module(name):
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

    return importlib.import_module(f"{PACKAGE}.{name}")


class EndpointRedactionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = load_module("error_classify")
        cls.module = module

    def summary(self, text):
        return self.module.safe_error_summary(text)

    def test_images_api_error_keeps_status_and_body_without_host(self):
        result = self.summary(
            'Images API Error 400: {"message": "openai_error", "type": '
            '"bad_response_status_code"} | URL: https://newapi.example-provider.love/v1/images'
        )

        self.assertNotIn("example-provider", result)
        self.assertIn("400", result)
        self.assertIn("openai_error", result)
        self.assertIn("https://***/v1/images", result)

    def test_connection_errors_hide_hostname_but_keep_reason(self):
        result = self.summary(
            "Cannot connect to host newapi.example-provider.love:443 ssl:default"
        )

        self.assertNotIn("example-provider", result)
        self.assertIn("Cannot connect to host ***:443", result)

    def test_bare_host_and_base_url_forms_are_masked(self):
        self.assertNotIn(
            "example-provider",
            self.summary("base_url=newapi.example-provider.love model=gpt-image-2"),
        )
        self.assertNotIn(
            "example-provider",
            self.summary("endpoint: newapi.example-provider.love/v1/images/edits"),
        )

    def test_local_proxy_and_key_are_masked_together(self):
        result = self.summary(
            "proxy http://127.0.0.1:7890/v1/images failed with key sk-ABCDEFGH1234567890"
        )

        self.assertNotIn("127.0.0.1", result)
        self.assertNotIn("ABCDEFGH1234567890", result)
        self.assertIn("http://***/v1/images", result)

    def test_diagnostic_tokens_are_not_mistaken_for_hosts(self):
        result = self.summary(
            "[v4.27.2] api_manager.py:568 main.py:2536 "
            "aiohttp.client_exceptions.ClientConnectorError model=gpt-image-2 size=2048x2048"
        )

        self.assertIn("api_manager.py", result)
        self.assertIn("main.py:2536", result)
        self.assertIn("v4.27.2", result)
        self.assertIn("gpt-image-2", result)
        self.assertIn("2048x2048", result)
        self.assertIn("ClientConnectorError", result)

    def test_debug_detail_prefixes_a_classification_label(self):
        detail = self.module.debug_error_detail(
            "Images API Multipart Error 503: upstream unavailable "
            "| URL: https://newapi.example-provider.love/v1/images/edits"
        )

        self.assertTrue(detail.startswith("["))
        self.assertIn("503", detail)
        self.assertNotIn("example-provider", detail)


class RequesterTagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tag = load_module("requester_tag")

    def test_tag_mode_normalization_falls_back_to_name(self):
        self.assertEqual(self.tag.normalize_tag_mode("AT"), "at")
        self.assertEqual(self.tag.normalize_tag_mode(" off "), "off")
        self.assertEqual(self.tag.normalize_tag_mode(""), "name")
        self.assertEqual(self.tag.normalize_tag_mode(None), "name")
        self.assertEqual(self.tag.normalize_tag_mode("nonsense"), "name")

    def test_label_uses_nickname_and_qq_but_skips_private_chat(self):
        self.assertEqual(
            self.tag.build_requester_label("小明", "10001", "20002"), "小明(10001)"
        )
        self.assertEqual(self.tag.build_requester_label("小明", "10001", ""), "")
        self.assertEqual(self.tag.build_requester_label("", "10001", "20002"), "10001")

    def test_long_and_multiline_nicknames_are_clipped(self):
        label = self.tag.build_requester_label("换\n名\t狂 魔" + "字" * 30, "10001", "20002")

        self.assertTrue(label.endswith("(10001)"))
        self.assertLessEqual(len(label.split("(")[0]), self.tag.NICKNAME_LIMIT + 1)
        self.assertNotIn("\n", label)

    def test_prefix_text_respects_mode(self):
        self.assertEqual(
            self.tag.build_prefix_text("name", "小明", "10001", "20002"), "[小明(10001)] "
        )
        self.assertEqual(
            self.tag.build_prefix_text("at", "小明", "10001", "20002"), "[小明(10001)] "
        )
        self.assertEqual(self.tag.build_prefix_text("off", "小明", "10001", "20002"), "")
        self.assertEqual(self.tag.build_prefix_text("name", "小明", "10001", ""), "")

    def test_mention_only_for_at_mode_in_groups(self):
        self.assertTrue(self.tag.should_mention_requester("at", "10001", "20002"))
        self.assertFalse(self.tag.should_mention_requester("name", "10001", "20002"))
        self.assertFalse(self.tag.should_mention_requester("at", "10001", ""))
        self.assertFalse(self.tag.should_mention_requester("at", "", "20002"))

    def test_prefix_is_inserted_after_leading_newlines(self):
        self.assertEqual(
            self.tag.apply_prefix("\n✅ 生成成功", "[小明(10001)] "),
            "\n[小明(10001)] ✅ 生成成功",
        )
        self.assertEqual(self.tag.apply_prefix("\n✅ 生成成功", ""), "\n✅ 生成成功")


class DashboardWiringTest(unittest.TestCase):
    """The two new switches must be reachable from the Dashboard, not just the raw config."""

    @classmethod
    def setUpClass(cls):
        page = ROOT / "pages" / "linghui-studio"
        cls.html = (page / "index.html").read_text(encoding="utf-8")
        cls.script = (page / "app.js").read_text(encoding="utf-8")
        cls.api = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")

    def test_controls_exist_in_the_settings_tab(self):
        self.assertIn('id="debug-mode"', self.html)
        self.assertIn('id="requester-tag-mode"', self.html)
        for mode in ("off", "name", "at"):
            self.assertIn(f'<option value="{mode}">', self.html)

    def test_controls_are_loaded_and_saved(self):
        self.assertIn('byId("debug-mode").checked = bool(settings.debug_mode);', self.script)
        self.assertIn('byId("requester-tag-mode").value = settings.requester_tag_mode', self.script)
        self.assertIn('debug_mode: checked("debug-mode"),', self.script)
        self.assertIn('requester_tag_mode: value("requester-tag-mode"),', self.script)

    def test_api_reads_and_normalizes_both_keys(self):
        self.assertIn('"debug_mode": self._as_bool(self.plugin.conf.get("debug_mode", False)),', self.api)
        self.assertIn('"requester_tag_mode": normalize_tag_mode(', self.api)
        self.assertIn('self.plugin.conf["requester_tag_mode"] = normalize_tag_mode(settings["requester_tag_mode"])', self.api)
        self.assertIn('"debug_mode")', self.api)


if __name__ == "__main__":
    unittest.main()

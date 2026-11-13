"""User notices stay readable while task diagnostics retain provider failures."""

import ast
from pathlib import Path
from typing import Any
import unittest

from test_error_redaction_and_requester_tag import load_module


ROOT = Path(__file__).resolve().parents[1]
POLICY_MESSAGE = "非常抱歉，生成的图片可能违反了关于裸露、色情或情色内容的防护限制。如果你认为此判断有误，请重试或修改提示语。"
SAFETY_STOP_MARKER = "（内容审核拦截，未继续切换渠道）"
SAFETY_STOP_ADVICE = "内容审核已拦截，本次未继续切换渠道。请调整提示词或参考图后重试。"
STOPPED_SAMPLE = ('绘图渠道均失败（最终分类：内容审核）：'
                  'orb[内容审核]: HTTP 400: content_policy_violation' + SAFETY_STOP_MARKER)
SAMPLE = (
    '绘图渠道均失败（最终分类：上游服务异常）：'
    'orb[请求参数或格式错误]: Images API Multipart Error 400: '
    '{"error": {"message": "' + POLICY_MESSAGE + '", "code": "content_polic'
    ' | m[上游服务异常]: Images API Multipart Error 400: '
    '{"error": {"code": "GPT_IMAGE_UPSTREAM_ERROR", "message": "' + POLICY_MESSAGE + '", "type": "image_gene'
    ' | miku[上游服务异常]: HTTP 524: <!DOCTYPE html> <!--[if lt IE 7]> <html cl'
)


def helper_harness():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LinghuiStudioPlugin")
    names = {"_public_error_message", "_resolve_debug_error_message", "_mask_llm_error",
             "_translate_error_to_chinese", "_translate_error_reason", "_is_transient_generation_error"}
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    classify = load_module("error_classify")
    namespace = {
        "Any": Any,
        "safe_error_summary": classify.safe_error_summary,
        "classify_generation_error": classify.classify_generation_error,
        "format_generation_failure": load_module("error_presentation").format_generation_failure,
    }
    exec(compile(ast.fix_missing_locations(module), str(ROOT / "main.py"), "exec"), namespace)
    return namespace["Harness"]()


class GenerationErrorPresentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.classify = staticmethod(load_module("error_classify").classify_generation_error)
        cls.format = staticmethod(load_module("error_presentation").format_generation_failure)

    def test_chinese_content_policy_is_not_a_bad_parameter_error(self):
        result = self.classify('Images API Multipart Error 400: {"message":"' + POLICY_MESSAGE + '"}')
        self.assertEqual(result.category, "safety")
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.retryable)
        self.assertFalse(result.try_next_channel)

    def test_policy_reason_wins_over_upstream_wrapper(self):
        result = self.classify('Images API Multipart Error 400: {"code":"GPT_IMAGE_UPSTREAM_ERROR","message":"' + POLICY_MESSAGE + '"}')
        self.assertEqual(result.category, "safety")

    def test_truncated_content_policy_code_is_still_recognized(self):
        self.assertEqual(self.classify('HTTP 400: {"code":"content_polic').category, "safety")

    def test_524_is_timeout_even_with_upstream_wrapper(self):
        result = self.classify('HTTP 524: {"error":"GPT_IMAGE_UPSTREAM_ERROR"} <!DOCTYPE html>')
        self.assertEqual(result.category, "timeout")
        self.assertTrue(result.retryable)

    def test_user_example_lists_one_short_reason_per_channel(self):
        result = self.format(SAMPLE)
        self.assertEqual(result.splitlines()[:3], [
            "• orb：内容审核未通过（HTTP 400）",
            "• m：内容审核未通过（HTTP 400）",
            "• miku：请求超时（HTTP 524）",
        ])
        self.assertIn("调整提示词或参考图", result)
        self.assertNotIn("最终分类", result)
        self.assertNotIn("{", result)
        self.assertNotIn("html", result)
        self.assertLess(len(result), 220)

    def test_status_free_output_still_preserves_channel_line_breaks(self):
        result = self.format(SAMPLE, include_status=False)
        self.assertEqual(result.splitlines()[:3], ["• orb：内容审核未通过", "• m：内容审核未通过", "• miku：请求超时"])
        self.assertNotIn("HTTP", result)

    def test_moderation_stop_explains_why_other_channels_were_not_used(self):
        self.assertEqual(self.format(STOPPED_SAMPLE).splitlines(), [
            "• orb：内容审核未通过（HTTP 400）", SAFETY_STOP_ADVICE,
        ])

    def test_moderation_stop_preserves_prior_channel_failures(self):
        error = ('绘图渠道均失败（最终分类：内容审核）：'
                 'miku[请求超时]: HTTP 524: timed out '
                 '| orb[内容审核]: HTTP 400: content_policy_violation' + SAFETY_STOP_MARKER)
        self.assertEqual(self.format(error).splitlines(), [
            "• miku：请求超时（HTTP 524）",
            "• orb：内容审核未通过（HTTP 400）", SAFETY_STOP_ADVICE,
        ])

    def test_stop_metadata_does_not_reclassify_a_channel_payload(self):
        error = ('绘图渠道均失败（最终分类：请求超时）：'
                 'orb[请求超时]: HTTP 524: timed out' + SAFETY_STOP_MARKER)
        self.assertEqual(self.format(error).splitlines()[0], "• orb：请求超时（HTTP 524）")

    def test_old_moderation_diagnostics_do_not_claim_fallback_stopped(self):
        result = self.format(STOPPED_SAMPLE.removesuffix(SAFETY_STOP_MARKER))
        self.assertEqual(result.splitlines()[-1], "请调整提示词或参考图后重试。")
        self.assertNotIn("未继续切换渠道", result)

    def test_timeout_without_stop_metadata_keeps_retry_advice(self):
        for error in ('HTTP 524: timed out',
                      '绘图渠道均失败（最终分类：请求超时）：orb[请求超时]: HTTP 524: timed out'):
            with self.subTest(error=error):
                result = self.format(error)
                self.assertIn("请求超时", result)
                self.assertEqual(result.splitlines()[-1], "请稍后重试；持续失败可查看任务详情。")
                self.assertNotIn("内容审核", result)

    def test_url_diagnostics_are_not_mistaken_for_extra_channels(self):
        error = ('绘图渠道均失败（最终分类：上游服务异常）：'
                 'a[上游服务异常]: HTTP 503: unavailable | URL: https://private.example.com/v1/images '
                 '| b[鉴权失败]: HTTP 401: invalid_api_key sk-EXAMPLE12345678')
        result = self.format(error)
        self.assertEqual(len([line for line in result.splitlines() if line.startswith("•")]), 2)
        for token in ("https", "private", "EXAMPLE", "invalid_api_key"):
            self.assertNotIn(token, result)

    def test_single_html_failure_never_reaches_chat(self):
        result = self.format('HTTP 524: <!DOCTYPE html><html><body>private data</body></html>')
        self.assertEqual(result.splitlines()[0], "请求超时（HTTP 524）")
        self.assertNotIn("private", result)
        self.assertNotIn("<", result)

    def test_unknown_traceback_has_a_safe_default(self):
        self.assertEqual(self.format("Traceback TypeError: secret detail", "失败了"), "失败了")

    def test_local_validation_prose_is_preserved(self):
        error = "预设 [动图画] 绑定的渠道不存在，请检查 WebUI 的渠道 ID。"
        self.assertEqual(self.format(error), error)

    def test_gif_static_response_has_specific_advice(self):
        result = self.format("渠道未返回有效的 GIF 动画，请确认模型支持 GIF 且渠道接受 output_format 参数。")
        self.assertIn("渠道未返回有效的 GIF 动画", result)
        self.assertIn("更换支持 GIF 的渠道", result)
        self.assertNotIn("output_format", result)

    def test_astrbot_api_keys_are_redacted_in_diagnostics(self):
        result = load_module("error_classify").safe_error_summary("failed abk_1234567890abcdef")
        self.assertEqual(result, "failed abk_***")


class MainFailureNoticeTest(unittest.TestCase):
    def setUp(self):
        self.plugin = helper_harness()
        self.plugin._should_show_debug_errors = lambda: True

    def test_direct_and_batch_helpers_do_not_append_raw_payloads(self):
        for name in ("_public_error_message", "_resolve_debug_error_message", "_translate_error_to_chinese"):
            with self.subTest(name=name):
                result = getattr(self.plugin, name)(SAMPLE, "默认失败")
                self.assertEqual(result.count("•"), 3)
                self.assertNotIn("{", result)
                self.assertNotIn("html", result)
                self.assertIn("\n• miku", result)

    def test_llm_failure_is_useful_without_raw_provider_data(self):
        result = self.plugin._mask_llm_error(SAMPLE)
        self.assertIn("内容审核未通过", result)
        self.assertIn("请求超时", result)
        self.assertNotIn("HTTP", result)
        self.assertNotIn("{", result)

    def test_a_policy_failure_with_timeout_word_is_not_transient(self):
        self.assertFalse(self.plugin._is_transient_generation_error('HTTP 400: content_policy_violation; upstream timeout'))
        self.assertTrue(self.plugin._is_transient_generation_error('HTTP 524: timed out'))

    def test_normal_mode_does_not_collapse_channel_line_breaks(self):
        self.plugin._should_show_debug_errors = lambda: False
        result = self.plugin._resolve_debug_error_message(SAMPLE)
        self.assertEqual(result.count("•"), 3)
        self.assertNotIn("HTTP", result)

    def test_moderation_stop_notice_uses_real_line_breaks(self):
        for debug in (True, False):
            with self.subTest(debug=debug):
                self.plugin._should_show_debug_errors = lambda: debug
                result = self.plugin._resolve_debug_error_message(STOPPED_SAMPLE)
                self.assertEqual(result.splitlines()[-1], SAFETY_STOP_ADVICE)
                self.assertEqual(len(result.splitlines()), 2)
                self.assertNotIn("\\", result)


if __name__ == "__main__":
    unittest.main()

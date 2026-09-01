import unittest

from utils import (
    MENTION_AVATAR_PRIVACY_INSTRUCTION,
    MENTION_AVATAR_PRIVACY_NEGATIVE_PROMPT,
    append_final_instruction,
    append_negative_prompt,
    combine_negative_prompts,
    has_mention_privacy_instruction,
    is_custom_drawing_command,
    normalize_api_root,
    normalize_model_list,
)


class UrlNormalizationTest(unittest.TestCase):
    def test_version_suffix_is_removed(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/v1beta"),
            "https://api.example.com",
        )

    def test_openai_endpoint_is_reduced_to_prefixed_root(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/openai/v1/chat/completions"),
            "https://api.example.com/openai",
        )

    def test_gemini_endpoint_is_reduced_to_prefixed_root(self):
        self.assertEqual(
            normalize_api_root(
                "https://api.example.com/google/v1beta/models/gemini-2.5-flash-image:generateContent?key=old"
            ),
            "https://api.example.com/google",
        )

    def test_image_endpoint_and_query_are_removed(self):
        self.assertEqual(
            normalize_api_root("https://api.example.com/api/v1/images/generations?foo=bar"),
            "https://api.example.com/api",
        )


class ModelListNormalizationTest(unittest.TestCase):
    def test_old_and_new_model_entries_are_supported(self):
        self.assertEqual(
            normalize_model_list([
                "model-a",
                {"id": "model-b"},
                {"model": "model-c"},
                {"name": "model-d"},
                {},
                None,
                "model-a",
            ]),
            ["model-a", "model-b", "model-c", "model-d"],
        )


class CustomDrawingCommandTest(unittest.TestCase):
    def test_bnn_remains_available_when_the_configured_command_changes(self):
        self.assertTrue(is_custom_drawing_command("bnn", "画"))
        self.assertTrue(is_custom_drawing_command("bnn(2)".split("(")[0], "生成"))
        self.assertTrue(is_custom_drawing_command("生成", "生成"))
        self.assertFalse(is_custom_drawing_command("手办化", "生成"))


class NegativePromptTest(unittest.TestCase):
    def test_negative_prompt_is_appended_once_as_a_portable_clause(self):
        result = append_negative_prompt("cinematic portrait", "blurry, watermark")
        self.assertEqual(result, "cinematic portrait\n\nNegative prompt: blurry, watermark")
        self.assertEqual(append_negative_prompt(result, "blurry, watermark"), result)
        self.assertEqual(append_negative_prompt("cinematic portrait", ""), "cinematic portrait")

    def test_negative_prompt_values_are_combined_without_duplicates(self):
        combined = combine_negative_prompts(
            "blurry, watermark",
            "low quality",
            "blurry, watermark",
        )
        self.assertIn("blurry, watermark", combined)
        self.assertIn("low quality", combined)
        self.assertEqual(combined.count("blurry, watermark"), 1)

    def test_legacy_privacy_constant_points_to_final_instruction(self):
        self.assertEqual(
            MENTION_AVATAR_PRIVACY_NEGATIVE_PROMPT,
            MENTION_AVATAR_PRIVACY_INSTRUCTION,
        )

    def test_mention_privacy_is_a_direct_final_instruction(self):
        result = append_final_instruction(
            "portrait\n\nNegative prompt: blurry",
            MENTION_AVATAR_PRIVACY_INSTRUCTION,
        )
        self.assertTrue(result.endswith(MENTION_AVATAR_PRIVACY_INSTRUCTION))
        self.assertIn("图片不带艾特对象的名字", result)
        self.assertEqual(
            append_final_instruction(result, MENTION_AVATAR_PRIVACY_INSTRUCTION),
            result,
        )

    def test_explicit_chinese_mention_privacy_instruction_is_detected(self):
        self.assertTrue(has_mention_privacy_instruction("图片不带艾特名字和 ID"))
        self.assertTrue(has_mention_privacy_instruction("不要显示群友昵称和 QQ 号"))

    def test_partial_or_unrelated_instruction_keeps_automatic_guard(self):
        self.assertFalse(has_mention_privacy_instruction("不要显示水印"))
        self.assertFalse(has_mention_privacy_instruction("使用艾特对象的头像和昵称"))
        self.assertFalse(has_mention_privacy_instruction("不要显示昵称"))

    def test_explicit_english_mention_privacy_instruction_is_detected(self):
        self.assertTrue(has_mention_privacy_instruction("Do not show display names or QQ IDs"))


if __name__ == "__main__":
    unittest.main()

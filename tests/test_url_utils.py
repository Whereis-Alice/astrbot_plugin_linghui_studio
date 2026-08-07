import unittest

from utils import is_custom_drawing_command, normalize_api_root, normalize_model_list


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


if __name__ == "__main__":
    unittest.main()

import importlib
import io
import pathlib
import sys
import types
import unittest

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_test_package"


def load_api_manager():
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

    return importlib.import_module(f"{PACKAGE}.api_manager").ApiManager


def load_channel_router():
    load_api_manager()
    return importlib.import_module(f"{PACKAGE}.channel_router").DrawingChannelRouter


class ApiEndpointBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ApiManager = load_api_manager()

    def setUp(self):
        self.manager = self.ApiManager({})

    def test_chat_mode_replaces_version_and_endpoint(self):
        self.assertEqual(
            self.manager._normalize_generic_chat_url(
                "https://api.example.com/openai/v1beta/chat/completions"
            ),
            "https://api.example.com/openai/v1/chat/completions",
        )

    def test_image_mode_replaces_version_and_endpoint(self):
        self.assertEqual(
            self.manager._convert_to_images_api_url(
                "https://api.example.com/v1/images/generations"
            ),
            "https://api.example.com/v1/images/generations",
        )

    def test_bare_images_collection_url_is_not_treated_as_a_complete_endpoint(self):
        self.assertTrue(
            self.manager._looks_like_openai_images_collection_endpoint(
                "https://api.example.com/openai/v1/images"
            )
        )
        self.assertFalse(
            self.manager._looks_like_openai_images_collection_endpoint(
                "https://api.example.com/openai/v1/images/edits"
            )
        )

    def test_gpt_image_uses_multipart_for_reference_images_by_default(self):
        self.assertEqual(self.manager._get_image_edit_transport("gpt-image-2"), "multipart")
        self.assertEqual(
            self.ApiManager({"image_edit_transport": "json"})._get_image_edit_transport("gpt-image-2"),
            "json",
        )

    def test_gpt_image_edit_clamps_an_unsupported_high_resolution_size(self):
        requested = {"resolution": "2K", "aspect_ratio": "1:1", "size": "2048x2048"}
        normalized = self.manager._normalize_gpt_image_edit_generation_params(
            requested,
            "gpt-image-2",
            True,
        )
        self.assertEqual(normalized, {"resolution": "1K", "aspect_ratio": "1:1", "size": "1024x1024"})
        self.assertEqual(requested["size"], "2048x2048")

    def test_gpt_image_text_request_keeps_the_requested_resolution(self):
        requested = {"resolution": "2K", "aspect_ratio": "1:1", "size": "2048x2048"}
        normalized = self.manager._normalize_gpt_image_edit_generation_params(
            requested,
            "gpt-image-2",
            False,
        )
        self.assertEqual(normalized, requested)

    def test_gpt_image_edit_keeps_multiple_reference_images(self):
        images, field_name = self.manager._edit_upload_images(
            [b"first", b"second"], "gpt-image-2"
        )
        self.assertEqual(images, [b"first", b"second"])
        self.assertEqual(field_name, "image[]")

    def test_legacy_image_edit_keeps_a_single_reference_image(self):
        images, field_name = self.manager._edit_upload_images(
            [b"first", b"second"], "dall-e-2"
        )
        self.assertEqual(images, [b"first"])
        self.assertEqual(field_name, "image")

    def test_gemini_mode_always_uses_v1beta(self):
        self.assertEqual(
            self.manager._build_gemini_api_url(
                "https://generativelanguage.googleapis.com/v1/models/old:generateContent",
                "models/gemini-2.5-flash-image",
            ),
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent",
        )


class ImageEditTransportTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ApiManager = load_api_manager()

    async def test_gpt_image_reference_request_directly_uses_multipart(self):
        manager = self.ApiManager({"image_edit_transport": "auto"})
        captured = {}

        async def fake_multipart(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return b"image-result"

        manager._call_images_api_multipart = fake_multipart
        result = await manager.call_images_api(
            [b"reference-image"],
            "portrait",
            "gpt-image-2",
            "test-key",
            "https://api.example.com",
        )

        self.assertEqual(result, b"image-result")
        self.assertEqual(captured["args"][2], "gpt-image-2")
        self.assertFalse(captured["kwargs"]["exact_endpoint"])

    async def test_bare_custom_images_url_is_normalized_at_runtime(self):
        manager = self.ApiManager({
            "interface_mode": "custom_endpoint",
            "base_url": "https://api.example.com/v1/images",
            "api_keys": "test-key",
        })
        captured = {}

        async def fake_images_api(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return b"image-result"

        manager.call_images_api = fake_images_api
        result = await manager._call_api_once([], "portrait", "gpt-image-2")

        self.assertEqual(result, b"image-result")
        self.assertEqual(captured["args"][4], "https://api.example.com/v1/images")
        self.assertFalse(captured["kwargs"]["exact_endpoint"])


class ReferenceImageEchoGuardTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ApiManager = load_api_manager()

    @staticmethod
    def _image_bytes(color, image_format="PNG"):
        output = io.BytesIO()
        Image.new("RGB", (96, 96), color).save(output, image_format)
        return output.getvalue()

    async def test_exact_reference_echo_is_rejected(self):
        reference = self._image_bytes((20, 80, 180))
        manager = self.ApiManager({})

        async def echo_reference(*args, **kwargs):
            return reference

        manager._call_api_once = echo_reference
        result = await manager.call_api([reference], "restyle this avatar", "model")

        self.assertIsInstance(result, str)
        self.assertIn("输入参考图相同", result)

    async def test_same_pixels_with_different_encoding_are_rejected(self):
        reference = self._image_bytes((40, 160, 90), "PNG")
        reencoded = self._image_bytes((40, 160, 90), "BMP")
        self.assertNotEqual(reference, reencoded)
        manager = self.ApiManager({})

        async def echo_reencoded(*args, **kwargs):
            return reencoded

        manager._call_api_once = echo_reencoded
        result = await manager.call_api([reference], "restyle this avatar", "model")

        self.assertIsInstance(result, str)
        self.assertIn("输入参考图相同", result)

    async def test_changed_image_is_accepted(self):
        reference = self._image_bytes((20, 80, 180))
        generated = self._image_bytes((220, 90, 40))
        manager = self.ApiManager({})

        async def return_generated(*args, **kwargs):
            return generated

        manager._call_api_once = return_generated
        result = await manager.call_api([reference], "restyle this avatar", "model")

        self.assertEqual(result, generated)


class ReferenceEchoFallbackIntegrationTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ApiManager = load_api_manager()
        cls.DrawingChannelRouter = load_channel_router()

    @staticmethod
    def _image_bytes(color):
        output = io.BytesIO()
        Image.new("RGB", (96, 96), color).save(output, "PNG")
        return output.getvalue()

    async def test_echo_from_primary_channel_falls_back_to_next_channel(self):
        reference = self._image_bytes((20, 80, 180))
        generated = self._image_bytes((220, 90, 40))
        primary = self.ApiManager({})
        backup = self.ApiManager({})

        async def echo_reference(*args, **kwargs):
            return reference

        async def return_generated(*args, **kwargs):
            return generated

        primary._call_api_once = echo_reference
        backup._call_api_once = return_generated
        router = self.DrawingChannelRouter({
            "drawing_channels": [
                {"id": "primary", "base_url": "primary", "api_keys": "one"},
                {"id": "backup", "base_url": "backup", "api_keys": "two"},
            ]
        })
        clients = {"primary": primary, "backup": backup}

        async def client_for(channel):
            return clients[channel["id"]]

        router._client_for = client_for
        result = await router.call_api([reference], "restyle this avatar", "model")

        self.assertEqual(result, generated)
        self.assertEqual(router.get_last_metrics()["channel_id"], "backup")
        self.assertEqual(router.get_last_metrics()["fallback_count"], 1)

    async def test_luxury_mode_skips_echo_and_keeps_waiting_for_a_real_result(self):
        reference = self._image_bytes((20, 80, 180))
        generated = self._image_bytes((220, 90, 40))
        manager = self.ApiManager({"enable_luxury_mode": True, "luxury_request_count": 2})
        call_count = 0

        async def return_in_order(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return reference
            return generated

        manager._call_api_once = return_in_order
        result = await manager.call_api([reference], "restyle this avatar", "model")

        self.assertEqual(result, generated)

if __name__ == "__main__":
    unittest.main()

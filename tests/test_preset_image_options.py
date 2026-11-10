"""A preset's image format affects only its own request on a shared channel."""

import asyncio
import copy
import importlib
import io
import types
import unittest

from PIL import Image

from test_api_manager_urls import load_api_manager, load_channel_router, PACKAGE
from test_api_transport_fallback import png_bytes
from test_gif_transport import gif_bytes


class PresetOptionsValidationTest(unittest.TestCase):
    def setUp(self):
        load_api_manager()
        self.options = importlib.import_module(PACKAGE + ".preset_options")

    def test_builtin_gif_default_and_explicit_auto(self):
        self.assertEqual(self.options.resolve_preset_generation_options({}, "GIF表情")["image_output_format"], "gif")
        config = {"preset_generation_options": [{"preset": "GIF表情", "image_output_format": "auto"}]}
        self.assertEqual(self.options.resolve_preset_generation_options(config, "GIF表情"), {})
        self.assertEqual(self.options.resolve_preset_generation_options({}, "照片"), {})

    def test_validation_rejects_duplicates_unknown_names_and_invalid_frame_fields(self):
        normalize = self.options.normalize_preset_generation_options
        for rows in (
            {}, [{"preset": "missing"}], [{"preset": "动图"}, {"preset": "动图"}],
            [{"preset": "动图", "image_output_format": "svg"}],
            [{"preset": "动图", "image_frame_count": True}],
            [{"preset": "动图", "image_frame_count": 17}],
            [{"preset": "动图", "image_frame_duration_ms": 167}],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize(rows, {"动图"})

    def test_options_only_allow_image_fields(self):
        rows = self.options.normalize_preset_generation_options(
            [{"preset": "动图", "image_output_format": "gif", "image_frame_count": 12,
              "image_frame_duration_ms": 170, "api_keys": "untrusted", "model": "ignored"}], {"动图"})
        self.assertEqual(rows[0]["__template_key"], "preset_generation_option")
        self.assertNotIn("api_keys", rows[0])
        self.assertNotIn("model", rows[0])


class SharedChannelPresetTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.animation = gif_bytes()
        self.still = png_bytes("blue")
        self.requests = []
        self.router = load_channel_router()({
            "model": "gpt-image-2.5-sunburst", "active_drawing_channel": "miku",
            "drawing_channels": [{"id": "miku", "model": "gpt-image-2.5-sunburst",
                "base_url": "https://relay.example.com", "api_keys": "test-key", "interface_mode": "auto",
                "image_output_format": "auto"}],
            "preset_channel_bindings": [{"preset": "动图画", "channel_id": "miku"}, {"preset": "照片", "channel_id": "miku"}],
            "preset_generation_options": [{"preset": "动图画", "image_output_format": "gif",
                "image_frame_count": 12, "image_frame_duration_ms": 170}],
        })
        self.addAsyncCleanup(self.router.close)
        owner = self

        class Response:
            status = 200
            charset = "utf-8"
            def __init__(self, animated):
                self.animated = animated
            async def read(self):
                await asyncio.sleep(0)
                return owner.animation if self.animated else owner.still
            async def __aenter__(self):
                await asyncio.sleep(0)
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            def post(self, url, **kwargs):
                if "json" in kwargs:
                    fields = kwargs["json"]
                else:
                    fields = {info["name"]: value for info, _, value in kwargs["data"]._fields if info["name"] != "image[]"}
                owner.requests.append((url, fields))
                return Response(fields.get("output_format") == "gif")

        self.session = Session()
        original = self.router._client_for
        async def client_for(channel):
            client = await original(channel)
            async def get_session():
                return self.session
            client._get_session = get_session
            return client
        self.router._client_for = client_for

    async def test_gif_and_still_share_client_without_concurrent_parameter_leak(self):
        before = copy.deepcopy(self.router.config)
        gif, still = await asyncio.gather(
            self.router.call_api([], "gentle loop", "gpt-image-2.5-sunburst", preset_name="动图画"),
            self.router.call_api([], "a blue square", "gpt-image-2.5-sunburst", preset_name="照片"),
        )
        self.assertEqual(gif, self.animation)
        self.assertEqual(still, self.still)
        self.assertEqual(len(self.router._clients), 1)
        self.assertEqual(self.router.config, before)
        animated = next(fields for _, fields in self.requests if fields.get("output_format") == "gif")
        normal = next(fields for _, fields in self.requests if fields.get("output_format") != "gif")
        self.assertEqual((animated["frame_count"], animated["frame_duration_ms"], animated["size"]), (12, 170, "auto"))
        self.assertNotIn("frame_count", normal)
        self.assertNotIn("output_format", normal)
        self.assertIsNone(self.router._clients["miku"]._request_image_options.get())

    async def test_reference_edit_gets_preset_options_for_25_model(self):
        result = await self.router.call_api([png_bytes("green")], "gentle loop", "gpt-image-2.5-sunburst", preset_name="动图画")
        self.assertEqual(result, self.animation)
        url, fields = self.requests[0]
        self.assertTrue(url.endswith("/images/edits"))
        self.assertEqual(fields["output_format"], "gif")
        self.assertEqual(fields["frame_count"], "12")
        self.assertEqual(fields["frame_duration_ms"], "170")

    async def test_auto_follows_channel_and_explicit_png_overrides_channel_gif(self):
        self.router.config["drawing_channels"][0].update(image_output_format="gif", image_frame_count=5)
        self.router.config["preset_generation_options"].append({"preset": "照片", "image_output_format": "png"})
        gif = await self.router.call_api([], "loop", "gpt-image-2.5-sunburst", preset_name="未指定格式")
        still = await self.router.call_api([], "square", "gpt-image-2.5-sunburst", preset_name="照片")
        self.assertEqual(gif, self.animation)
        self.assertEqual(still, self.still)
        self.assertEqual(self.requests[0][1]["frame_count"], 5)
        self.assertEqual(self.requests[1][1]["output_format"], "png")
        self.assertNotIn("frame_count", self.requests[1][1])

    async def test_static_result_is_rejected_for_gif_preset(self):
        self.animation = self.still
        result = await self.router.call_api([], "loop", "gpt-image-2.5-sunburst", preset_name="动图画")
        self.assertIsInstance(result, str)
        self.assertIn("GIF", result)

    async def test_invalid_preset_options_do_not_call_upstream(self):
        self.router.config["preset_generation_options"][0]["image_frame_duration_ms"] = 167
        result = await self.router.call_api([], "loop", "gpt-image-2.5-sunburst", preset_name="动图画")
        self.assertIsInstance(result, str)
        self.assertEqual(self.requests, [])

    async def test_cancelled_call_restores_request_context(self):
        manager = load_api_manager()({"image_output_format": "auto"})
        started = asyncio.Event()
        async def wait_forever(*args, **kwargs):
            self.assertEqual(manager._image_option("image_output_format"), "gif")
            started.set()
            await asyncio.Event().wait()
        manager._call_api_once = wait_forever
        task = asyncio.create_task(manager.call_api([], "loop", "test", image_options={"image_output_format": "gif"}))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(manager._request_image_options.get())
        self.assertEqual(manager.config["image_output_format"], "auto")


if __name__ == "__main__":
    unittest.main()

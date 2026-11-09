"""Exercise animated Images requests and retain the complete provider output."""

import base64
import importlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from test_api_manager_urls import load_api_manager
from test_api_transport_fallback import Session, error, form_values, png_bytes
from test_image_manager_mentions import load_image_manager


def gif_bytes(size=(32, 32), noisy=False):
    output = io.BytesIO()
    if noisy:
        rng = random.Random(84)
        frames = [Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3)) for _ in range(2)]
    else:
        frames = [Image.new("RGB", size, color) for color in ("red", "blue")]
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return output.getvalue()


class GifTransportTest(unittest.IsolatedAsyncioTestCase):
    def manager(self, responses, **config):
        config = {
            "base_url": "https://relay.example.com",
            "api_keys": "test-key",
            "interface_mode": "auto",
            "image_output_format": "gif",
            "image_frame_count": 9,
            "image_frame_duration_ms": 200,
            "image_complexity": "medium",
            "image_resolution": "4K",
            "use_stream": True,
            **config,
        }
        manager = load_api_manager()(config)
        session = Session(responses)

        async def get_session():
            return session

        manager._get_session = get_session
        return manager, session

    def assert_gif_options(self, options):
        self.assertEqual(options["output_format"], "gif")
        self.assertEqual(options["frame_count"], 9)
        self.assertEqual(options["frame_duration_ms"], 200)
        self.assertEqual(options["complexity"], "medium")
        self.assertEqual(options["size"], "auto")
        for forbidden in ("stream", "partial_images", "output_compression", "input_fidelity"):
            self.assertNotIn(forbidden, options)

    async def test_json_generation_decodes_complete_gif_for_both_26_models(self):
        animation = gif_bytes()
        encoded = base64.b64encode(animation).decode()
        body = json.dumps({"data": [{"b64_json": encoded}]}).encode()
        for model in ("gpt-image-2.6-flare", "gpt-image-2.6-sunburst"):
            with self.subTest(model=model):
                manager, session = self.manager([(200, body)])
                result = await manager.call_api([], "a looping animation", model)
                self.assertEqual(result, animation)
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(session.calls[0][0].endswith("/images/generations"))
                self.assert_gif_options(session.calls[0][1]["json"])
                self.assertTrue(manager.extract_image_url({"data": [{"b64_json": encoded}]}).startswith("data:image/gif;base64,"))
                with Image.open(io.BytesIO(result)) as decoded:
                    self.assertEqual(decoded.n_frames, 2)
                    self.assertEqual(decoded.info["duration"], 200)
                    self.assertEqual(decoded.info["loop"], 0)

    async def test_multipart_edit_carries_frame_fields_once_and_preserves_references(self):
        animation = gif_bytes()
        references = [png_bytes("green"), png_bytes("yellow")]
        manager, session = self.manager([(200, animation)])
        result = await manager.call_api(references, "make a gentle loop", "gpt-image-2.6-flare")
        self.assertEqual(result, animation)
        self.assertTrue(session.calls[0][0].endswith("/images/edits"))
        kwargs = session.calls[0][1]
        self.assertEqual(form_values(kwargs, "image[]"), references)
        fields = {options["name"]: value for options, _, value in kwargs["data"]._fields if options["name"] != "image[]"}
        fields["frame_count"] = int(fields["frame_count"])
        fields["frame_duration_ms"] = int(fields["frame_duration_ms"])
        self.assert_gif_options(fields)
        self.assertEqual(form_values(kwargs, "size"), ["auto"])

    async def test_explicit_json_edit_has_same_gif_options(self):
        animation = gif_bytes()
        manager, session = self.manager([(200, animation)], image_edit_transport="json")
        result = await manager.call_api([png_bytes("green")], "animate", "gpt-image-2.6-sunburst")
        self.assertEqual(result, animation)
        self.assert_gif_options(session.calls[0][1]["json"])
        self.assertTrue(session.calls[0][1]["json"]["image"].startswith("data:image/png;base64,"))

    async def test_gif_does_not_retry_with_static_size_or_drop_options_for_chat(self):
        manager, session = self.manager([error(400, "openai_error: size unsupported")])
        result = await manager.call_api([], "animate", "gpt-image-2.6-flare")
        self.assertIn("400", result)
        self.assertEqual(len(session.calls), 1)
        self.assert_gif_options(session.calls[0][1]["json"])

    async def test_gif_first_frame_can_match_a_still_reference(self):
        animation = gif_bytes()
        manager, _ = self.manager([])

        async def produce_animation(*args, **kwargs):
            return animation

        manager._call_api_once = produce_animation
        result = await manager.call_api([png_bytes("red")], "make it move", "gpt-image-2.6-flare")
        self.assertEqual(result, animation)

    async def test_byte_identical_animation_echo_is_still_rejected(self):
        animation = gif_bytes()
        manager, _ = self.manager([])
        result = await manager._validate_reference_result(animation, [animation])
        self.assertIn(manager.REFERENCE_ECHO_ERROR, result)

    async def test_auto_uses_images_even_for_custom_gif_model_names(self):
        animation = gif_bytes()
        manager, session = self.manager([(200, animation)])
        result = await manager.call_api([], "animate", "custom-animation-model")
        self.assertEqual(result, animation)
        self.assertTrue(session.calls[0][0].endswith("/images/generations"))
        self.assert_gif_options(session.calls[0][1]["json"])

    async def test_explicit_incompatible_transport_rejects_gif_before_request(self):
        for mode in ("openai_chat", "gemini_official", "custom_endpoint"):
            with self.subTest(mode=mode):
                manager, session = self.manager([], interface_mode=mode)
                result = await manager.call_api([], "animate", "custom-animation-model")
                self.assertIn("OpenAI Images", result)
                self.assertEqual(session.calls, [])

    async def test_gateway_returning_static_image_for_gif_is_not_success(self):
        manager, session = self.manager([(200, png_bytes())])
        result = await manager.call_api([], "animate", "gpt-image-2.6-flare")
        self.assertIn("未返回有效的 GIF", result)
        self.assertEqual(len(session.calls), 1)

    async def test_cached_original_and_qq_message_keep_all_animation_bytes(self):
        from astrbot.api.message_components import Image as MessageImage
        from astrbot.core.message.message_event_result import MessageChain

        load_api_manager()
        data_module = importlib.import_module("linghui_test_package.data_manager")
        delivery_module = importlib.import_module("linghui_test_package.image_delivery")
        animation = gif_bytes()
        with tempfile.TemporaryDirectory() as directory:
            manager = data_module.DataManager(Path(directory), {})
            await manager.initialize()
            record = await manager.save_generation_record(animation, prompt="a loop", user_id="gif-test", model="gpt-image-2.6-flare")
            self.assertTrue(record["filename"].endswith(".gif"))
            self.assertEqual(manager.get_generation_image_path(record).read_bytes(), animation)
        chain = MessageChain(chain=[MessageImage.fromBytes(animation)])
        prepared = await delivery_module.prepare_generated_chain(chain, {"qq_image_delivery_mode": "auto"}, "aiocqhttp")
        node = prepared.chain[0]
        encoded = node.url or node.file
        self.assertEqual(base64.b64decode(encoded.removeprefix("base64://")), animation)


class GifOptionsTest(unittest.TestCase):
    def test_old_model_defaults_have_no_new_optional_fields(self):
        manager = load_api_manager()({})
        self.assertEqual(manager._images_request_options("gpt-image-2"), {"quality": "auto"})
        self.assertEqual(manager._images_request_options("legacy-relay"), {})

    def test_explicit_formats_work_for_relay_model_names_without_a_whitelist(self):
        manager = load_api_manager()({"image_output_format": "gif"})
        options = manager._images_request_options("my-animation-model")
        self.assertEqual(options, {"output_format": "gif", "frame_count": 9, "frame_duration_ms": 200, "size": "auto"})

    def test_static_formats_never_send_frame_parameters(self):
        for output_format in ("auto", "png", "jpeg", "webp"):
            with self.subTest(output_format=output_format):
                manager = load_api_manager()({"image_output_format": output_format, "image_frame_count": "ignored"})
                options = manager._images_request_options("gpt-image-2.6-flare")
                self.assertNotIn("frame_count", options)
                self.assertNotIn("frame_duration_ms", options)
                self.assertNotIn("size", options)
                self.assertNotIn("complexity", options)
                if output_format == "auto":
                    self.assertNotIn("output_format", options)
                else:
                    self.assertEqual(options["output_format"], output_format)

    def test_invalid_gif_options_fail_before_upstream_request(self):
        for name, value in (
            ("image_frame_count", 1), ("image_frame_count", 17),
            ("image_frame_count", True), ("image_frame_count", 2.5),
            ("image_frame_duration_ms", 10), ("image_frame_duration_ms", 2010),
            ("image_frame_duration_ms", 25), ("image_complexity", "max"),
            ("image_output_format", "svg"),
        ):
            with self.subTest(name=name, value=value):
                manager = load_api_manager()({"image_output_format": "gif", name: value})
                with self.assertRaises(ValueError):
                    manager._images_request_options("gpt-image-2.6-flare")

    def test_frame_boundaries_and_numeric_strings(self):
        for count, duration in ((2, 20), (16, 2000), ("9", "200")):
            options = load_api_manager()({"image_output_format": "gif", "image_frame_count": count, "image_frame_duration_ms": duration})._images_request_options("gpt-image-2.6-flare")
            self.assertEqual(options["frame_count"], int(count))
            self.assertEqual(options["frame_duration_ms"], int(duration))

    def test_large_animation_is_not_flattened_by_legacy_optimizer(self):
        animation = gif_bytes((800, 600), noisy=True)
        self.assertGreater(len(animation), 700_000)
        result = load_image_manager()({})._optimize_output_image_sync(animation)
        self.assertIs(result, animation)
        with Image.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.n_frames, 2)


if __name__ == "__main__":
    unittest.main()

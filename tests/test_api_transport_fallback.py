"""Exercise bounded compatibility retries with real request construction."""

import copy
import io
import json
import unittest

from PIL import Image

from test_api_manager_urls import load_api_manager


def png_bytes(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(output, "PNG")
    return output.getvalue()


def error(status, message):
    return status, json.dumps({"error": {"message": message}}).encode()


class Response:
    charset = "utf-8"

    def __init__(self, session, status, body):
        self.session = session
        self.status = status
        self.body = body

    async def read(self):
        return self.body

    async def __aenter__(self):
        self.session.active += 1
        return self

    async def __aexit__(self, *exc):
        self.session.active -= 1


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.active = 0

    def post(self, url, **kwargs):
        # A retry must not hold an earlier HTTP response open.
        if self.active:
            raise AssertionError("recursive retry inside an open HTTP response")
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected extra upstream request")
        return Response(self, *self.responses.pop(0))


def form_values(kwargs, name):
    return [value for options, _headers, value in kwargs["data"]._fields if options["name"] == name]


class TransportFallbackTest(unittest.IsolatedAsyncioTestCase):
    def manager(self, responses, **overrides):
        config = {
            "base_url": "https://relay.example.com",
            "api_keys": ["test-key-one", "test-key-two"],
            "interface_mode": "auto",
            "image_edit_transport": "auto",
            "image_resolution": "1K",
            "image_aspect_ratio": "1:1",
        }
        config.update(overrides)
        manager = load_api_manager()(config)
        session = Session(responses)

        async def get_session():
            return session

        manager._get_session = get_session
        return manager, session

    async def test_each_interface_only_once_with_same_key_and_unchanged_config(self):
        for preferred, expected in (
            ("auto", ["images/generations", "chat/completions"]),
            ("openai_image", ["images/generations", "chat/completions"]),
            ("openai_chat", ["chat/completions", "images/generations"]),
        ):
            with self.subTest(preferred=preferred):
                manager, session = self.manager(
                    [error(405, "not support this api"), error(405, "not support this api")],
                    interface_mode=preferred,
                )
                original = copy.deepcopy(manager.config)
                result = await manager._call_api_once([], "a blue circle", "gpt-image-2")
                self.assertIn("405", result)
                self.assertEqual([url.rsplit("/v1/", 1)[-1] for url, _ in session.calls], expected)
                self.assertEqual(manager.unified_idx, 1)
                self.assertEqual(manager.config, original)
                for _, kwargs in session.calls:
                    self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key-one")
                    self.assertEqual(kwargs["json"]["n"], 1)

    async def test_chat_405_falls_back_to_correct_images_endpoint(self):
        for references in ([], [png_bytes("red"), png_bytes("green")]):
            with self.subTest(edit=bool(references)):
                generated = png_bytes()
                manager, session = self.manager(
                    [error(405, "Method Not Allowed"), (200, generated)],
                    interface_mode="openai_chat",
                )
                result = await manager.call_api(references, "a blue circle", "gpt-image-2")
                self.assertEqual(result, generated)
                self.assertEqual(len(session.calls), 2)
                self.assertTrue(session.calls[1][0].endswith("/images/edits" if references else "/images/generations"))
                content = session.calls[0][1]["json"]["messages"][0]["content"]
                self.assertEqual(len(content), 1 + len(references))
                if references:
                    self.assertEqual(form_values(session.calls[1][1], "image[]"), references)
                    self.assertEqual(form_values(session.calls[1][1], "n"), ["1"])

    async def test_multipart_and_json_do_not_ping_pong(self):
        manager, session = self.manager([
            error(415, "expected application/json"),
            error(415, "expected multipart/form-data"),
        ])
        result = await manager.call_images_api(
            [png_bytes("red")], "a blue circle", "gpt-image-2", "test-key", "https://relay.example.com",
            generation_params={"size": "1024x1024", "resolution": "1K", "aspect_ratio": "1:1"},
        )
        self.assertIn("415", result)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("data", session.calls[0][1])
        self.assertIn("json", session.calls[1][1])

    async def test_explicit_upload_format_does_not_switch(self):
        for upload_format in ("multipart", "json"):
            with self.subTest(upload_format=upload_format):
                manager, session = self.manager([error(415, "unsupported media type")], image_edit_transport=upload_format)
                await manager.call_images_api(
                    [png_bytes("red")], "a blue circle", "gpt-image-2", "test-key", "https://relay.example.com",
                )
                self.assertEqual(len(session.calls), 1)
                self.assertIn("data" if upload_format == "multipart" else "json", session.calls[0][1])

    async def test_custom_complete_endpoint_does_not_switch_or_downgrade(self):
        for path in ("/v1/images/edits?version=2", "/v1/chat/completions?version=2"):
            with self.subTest(path=path):
                manager, session = self.manager(
                    [error(400, "openai_error")], interface_mode="custom_endpoint",
                    base_url="https://relay.example.com" + path,
                )
                await manager._call_api_once([png_bytes("red")], "a blue circle", "gpt-image-2", resolution="2K")
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(session.calls[0][0], manager.config["base_url"])

    async def test_non_compatibility_errors_never_switch_protocol_or_size(self):
        cases = [
            (401, "invalid API key multipart/form-data"),
            (403, "forbidden"),
            (429, "rate limit"),
            (400, "safety filter blocked size"),
            (400, "insufficient balance openai_error"),
            (400, "tiered expr invalid operation: <nil> * float64"),
            (400, "invalid prompt"),
            (400, "reference image blocked by safety policy openai_error"),
            (400, "reference image invalid_api_key"),
            (503, "openai_error"),
            (504, "gateway timeout"),
        ]
        for status, message in cases:
            with self.subTest(status=status, message=message):
                manager, session = self.manager([error(status, message)])
                result = await manager._call_api_once([png_bytes("red")], "a blue circle", "gpt-image-2", resolution="2K")
                self.assertIsInstance(result, str)
                self.assertEqual(len(session.calls), 1)
                self.assertFalse(manager._is_transport_compatibility_error(
                    f"API Error {status}: {message} | URL: https://relay.example.com/v1/images/edits"
                ))

    async def test_model_not_found_404_does_not_switch_protocol(self):
        manager, session = self.manager([error(404, "model_not_found: model not found")])
        result = await manager._call_api_once([], "a blue circle", "gpt-image-2")
        self.assertIn("404", result)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(manager._is_transport_compatibility_error(result))

    async def test_http_200_with_no_image_does_not_issue_another_generation(self):
        for body in (
            {"error": {"message": "use images api"}},
            {"choices": [{"message": {"content": "use images api"}}]},
        ):
            with self.subTest(body=body):
                manager, session = self.manager([(200, json.dumps(body).encode())], interface_mode="openai_chat")
                result = await manager._call_api_once([], "a blue circle", "gpt-image-2")
                self.assertIsInstance(result, str)
                self.assertEqual(len(session.calls), 1)

    async def test_edit_and_generation_preserve_size_then_retry_only_once(self):
        for references, upload_format in (([], "auto"), ([png_bytes("red")], "multipart"), ([png_bytes("red")], "json")):
            with self.subTest(edit=bool(references), upload_format=upload_format):
                generated = png_bytes()
                manager, session = self.manager([error(400, "unsupported size"), (200, generated)], image_edit_transport=upload_format)
                params = {"resolution": "2K", "aspect_ratio": "16:9", "size": "2048x1152"}
                result = await manager.call_images_api(
                    references, "a blue circle", "gpt-image-2", "test-key", "https://relay.example.com",
                    generation_params=params,
                )
                self.assertEqual(result, generated)
                self.assertEqual(len(session.calls), 2)
                sizes = [kwargs["json"]["size"] if "json" in kwargs else form_values(kwargs, "size")[0] for _, kwargs in session.calls]
                self.assertEqual(sizes, ["2048x1152", "1024x1024"])
                self.assertEqual(params["size"], "2048x1152")

    async def test_already_compatible_size_is_not_retried(self):
        manager, session = self.manager([error(400, "unsupported size")])
        result = await manager._call_api_once([], "a blue circle", "gpt-image-2")
        self.assertIn("400", result)
        self.assertEqual(len(session.calls), 1)

    async def test_opaque_error_has_bounded_size_format_and_protocol_attempts(self):
        manager, session = self.manager([error(400, "openai_error")] * 4)
        await manager._call_api_once([png_bytes("red")], "a blue circle", "gpt-image-2", resolution="2K")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(["multipart" if "data" in kwargs else "json" for _, kwargs in session.calls], ["multipart", "multipart", "json", "json"])
        self.assertTrue(session.calls[-1][0].endswith("/chat/completions"))
        self.assertEqual(manager.unified_idx, 1)

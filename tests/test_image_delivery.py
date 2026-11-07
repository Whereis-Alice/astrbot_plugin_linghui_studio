"""Verify QQ copies, retained original bytes and bounded real SDK deliveries."""

import asyncio
import base64
import io
import types
import unittest
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Image as MessageImage
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain
from PIL import Image
from test_generation_full_image import image_bytes
from test_linghui_features import _load_module
from test_message_delivery import delivery_harness


class DeliveryCopyTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_module("image_delivery")

    def test_large_opaque_png_keeps_dimensions_and_reduces_upload(self):
        original = image_bytes(size=(1000, 600), compress_level=0)
        self.assertGreater(len(original), 1024 * 1024)
        result = self.module.prepare_delivery_copy(
            original, {"qq_image_delivery_max_mb": 1}
        )
        self.assertEqual(result.reason, "reencoded")
        self.assertLess(len(result.data), len(original) // 10)
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (1000, 600)))
        with Image.open(io.BytesIO(original)) as image:
            self.assertEqual(image.format, "PNG")

    def test_small_images_are_byte_identical(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            original = image_bytes(fmt)
            self.assertIs(
                self.module.prepare_delivery_copy(original, {}).data, original
            )

    def test_explicit_edge_limit_only_shrinks_proportionally(self):
        original = image_bytes(size=(1200, 600))
        result = self.module.prepare_delivery_copy(
            original, {"qq_image_delivery_max_edge": 512}
        )
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.size, (512, 256))
        self.assertEqual(result.reason, "resized")

    def test_alpha_survives_even_when_above_the_byte_target(self):
        original = image_bytes(size=(1000, 600), mode="RGBA", compress_level=0)
        result = self.module.prepare_delivery_copy(
            original, {"qq_image_delivery_max_mb": 1}
        )
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.getpixel((0, 0))[3], 80)

    def test_animation_is_never_flattened_or_resized(self):
        output = io.BytesIO()
        Image.new("RGB", (600, 10), "red").save(
            output,
            "GIF",
            save_all=True,
            append_images=[Image.new("RGB", (600, 10), "blue")],
            duration=100,
            loop=0,
        )
        original = output.getvalue()
        result = self.module.prepare_delivery_copy(
            original, {"qq_image_delivery_max_edge": 512}
        )
        self.assertIs(result.data, original)
        self.assertEqual(result.reason, "animated")

    def test_exif_rotation_is_applied_before_delivery_resize(self):
        exif = Image.Exif()
        exif[274] = 6
        original = image_bytes("JPEG", size=(1200, 600), exif=exif)
        result = self.module.prepare_delivery_copy(
            original, {"qq_image_delivery_max_edge": 512}
        )
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.size, (256, 512))
            self.assertNotIn(274, image.getexif())

    def test_invalid_image_and_decoder_error_use_original(self):
        for data in (b"invalid image", image_bytes()[:40]):
            self.assertIs(self.module.prepare_delivery_copy(data, {}).data, data)
        original = image_bytes(size=(1200, 600))
        with patch.object(
            self.module.ImageOps, "exif_transpose", side_effect=OSError("decoder")
        ):
            self.assertIs(
                self.module.prepare_delivery_copy(
                    original, {"qq_image_delivery_max_edge": 512}
                ).data,
                original,
            )

    def test_pixel_limit_precedes_decode(self):
        original = image_bytes()
        with patch.object(self.module, "MAX_DECODE_PIXELS", 1):
            result = self.module.prepare_delivery_copy(original, {})
        self.assertEqual(result.reason, "pixel_limit")
        self.assertIs(result.data, original)


class ChainDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_module("image_delivery")
        self.original = image_bytes(size=(1000, 600), compress_level=0)
        self.chain = MessageChain(
            [MessageImage.fromBytes(self.original), Plain("caption")]
        )

    async def test_sdk_chain_and_node_are_copied_and_original_is_retained(self):
        original_file = self.chain.chain[0].file
        prepared = await self.module.prepare_generated_chain(
            self.chain, {"qq_image_delivery_max_mb": 1}, "aiocqhttp"
        )
        self.assertIsNot(prepared, self.chain)
        self.assertIsNot(prepared.chain[0], self.chain.chain[0])
        self.assertIs(prepared.chain[1], self.chain.chain[1])
        self.assertEqual(self.chain.chain[0].file, original_file)
        # Real SDK MediaResolver handles the inline delivery copy.
        payload = await prepared.chain[0].convert_to_base64()
        self.assertTrue(base64.b64decode(payload).startswith(b"\xff\xd8"))

    async def test_original_mode_and_other_platforms_do_not_schedule_processing(self):
        for config, platform in [
            ({"qq_image_delivery_mode": "original"}, "aiocqhttp"),
            ({}, "webchat"),
            ({}, "qq_official"),
        ]:
            with patch.object(
                self.module.asyncio,
                "to_thread",
                side_effect=AssertionError("processing"),
            ):
                self.assertIs(
                    await self.module.prepare_generated_chain(
                        self.chain, config, platform
                    ),
                    self.chain,
                )

    async def test_remote_urls_and_malformed_base64_are_not_fetched(self):
        chain = MessageChain(
            [
                MessageImage.fromURL("https://example.invalid/private"),
                MessageImage.fromBase64("bad!!"),
            ]
        )
        with patch.object(
            MessageImage,
            "convert_to_base64",
            side_effect=AssertionError("network fetch"),
        ):
            prepared = await self.module.prepare_generated_chain(chain, {}, "aiocqhttp")
        self.assertEqual(
            [node.file for node in prepared.chain], [node.file for node in chain.chain]
        )

    async def test_two_results_for_same_bot_are_uploaded_serially(self):
        plugin = delivery_harness()()
        plugin.conf = {"qq_image_delivery_mode": "original"}
        active = maximum = 0

        async def send(chain):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

        event = types.SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq-1"
        )
        results = await asyncio.gather(
            *(
                plugin._deliver_generated_chain(self.chain, send, event=event)
                for _ in range(3)
            )
        )
        self.assertEqual(results, ["sent"] * 3)
        self.assertEqual(maximum, 1)

    async def test_separate_bots_do_not_share_upload_gate(self):
        plugin = delivery_harness()()
        plugin.conf = {"qq_image_delivery_mode": "original"}
        arrived = asyncio.Event()
        active = 0

        async def send(chain):
            nonlocal active
            active += 1
            if active == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), 1)

        events = [
            types.SimpleNamespace(
                get_platform_name=lambda: "aiocqhttp",
                get_platform_id=lambda i=i: f"qq-{i}",
            )
            for i in (1, 2)
        ]
        self.assertEqual(
            await asyncio.gather(
                *(
                    plugin._deliver_generated_chain(self.chain, send, event=e)
                    for e in events
                )
            ),
            ["sent", "sent"],
        )

    async def test_dashboard_resolves_adapter_type_from_instance_id(self):
        plugin = delivery_harness()()
        plugin.conf = {"qq_image_delivery_max_mb": 1}
        platform = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(name="aiocqhttp")
        )
        plugin.context = types.SimpleNamespace(
            get_platform_inst=lambda instance: (
                platform if instance == "custom-bot-id" else None
            ),
            send_message=AsyncMock(return_value=True),
        )
        self.assertEqual(
            await plugin._send_generated_to_session(
                "custom-bot-id:GroupMessage:1", self.chain
            ),
            "sent",
        )
        sent = plugin.context.send_message.call_args.args[1]
        self.assertTrue(
            base64.b64decode(await sent.chain[0].convert_to_base64()).startswith(
                b"\xff\xd8"
            )
        )
        self.assertEqual(
            base64.b64decode(await self.chain.chain[0].convert_to_base64()),
            self.original,
        )


if __name__ == "__main__":
    unittest.main()

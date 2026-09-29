import asyncio
import base64
import importlib
import io
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_gallery_test_package"


def load_module(name):
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    if "astrbot" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        astrbot.logger = Mock()
        sys.modules["astrbot"] = astrbot
    if "quart" not in sys.modules:
        quart = types.ModuleType("quart")
        quart.jsonify = lambda payload: payload
        quart.request = types.SimpleNamespace(args={})

        async def send_file(*args, **kwargs):
            return args, kwargs

        quart.send_file = send_file
        sys.modules["quart"] = quart
    return importlib.import_module(f"{PACKAGE}.{name}")


def image_bytes(image_format="PNG", size=(80, 40), mode="RGB", **save_options):
    output = io.BytesIO()
    color = (20, 160, 220, 80) if mode == "RGBA" else (20, 160, 220)
    Image.new(mode, size, color).save(output, image_format, **save_options)
    return output.getvalue()


def decode_payload(payload):
    return base64.b64decode(payload["image"].split(",", 1)[1])


class GalleryImageEncodingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module("gallery_preview")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = pathlib.Path(self.directory.name) / "cached-image.bin"

    def encode(self, data):
        self.path.write_bytes(data)
        return self.module.build_gallery_image(self.path)

    def test_supported_formats_preserve_real_original_bytes_and_mime(self):
        for image_format, mime in (
            ("PNG", "image/png"), ("JPEG", "image/jpeg"),
            ("WEBP", "image/webp"), ("GIF", "image/gif"), ("BMP", "image/bmp"),
        ):
            with self.subTest(image_format=image_format):
                original = image_bytes(image_format)
                result = self.encode(original)
                self.assertEqual(decode_payload(result), original)
                self.assertTrue(result["image"].startswith(f"data:{mime};base64,"))
                self.assertFalse(result["preview_only"])
                self.assertEqual((result["width"], result["height"]), (80, 40))
                self.assertEqual((result["original_width"], result["original_height"]), (80, 40))

    def test_animated_gif_remains_animated_without_conversion(self):
        output = io.BytesIO()
        first = Image.new("RGB", (20, 10), "red")
        first.save(output, "GIF", save_all=True, append_images=[Image.new("RGB", (20, 10), "blue")], duration=100, loop=0)
        result = self.encode(output.getvalue())
        self.assertEqual(decode_payload(result), output.getvalue())
        with Image.open(io.BytesIO(decode_payload(result))) as decoded:
            self.assertEqual(decoded.n_frames, 2)

    def test_tiff_converts_to_displayable_jpeg(self):
        result = self.encode(image_bytes("TIFF"))
        self.assertTrue(result["preview_only"])
        with Image.open(io.BytesIO(decode_payload(result))) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (80, 40))

    def test_converted_transparent_image_retains_alpha(self):
        result = self.encode(image_bytes("TIFF", mode="RGBA"))
        self.assertTrue(result["image"].startswith("data:image/png;base64,"))
        self.assertTrue(result["preview_only"])
        with Image.open(io.BytesIO(decode_payload(result))) as decoded:
            self.assertEqual(decoded.mode, "RGBA")
            self.assertEqual(decoded.getpixel((0, 0))[3], 80)

    def test_exif_rotation_is_corrected_and_reported_as_preview(self):
        exif = Image.Exif()
        exif[274] = 6
        result = self.encode(image_bytes("JPEG", exif=exif))
        self.assertTrue(result["preview_only"])
        self.assertEqual((result["width"], result["height"]), (40, 80))
        self.assertEqual((result["original_width"], result["original_height"]), (80, 40))
        with Image.open(io.BytesIO(decode_payload(result))) as decoded:
            self.assertEqual(decoded.size, (40, 80))
            self.assertNotIn(274, decoded.getexif())

    def test_original_over_inline_limit_is_resized_and_reencoded(self):
        original = image_bytes("BMP", size=(200, 100))
        with patch.object(self.module, "MAX_INLINE_BYTES", 2048), patch.object(self.module, "MAX_PREVIEW_EDGE", 64):
            result = self.encode(original)
        self.assertGreater(len(original), 2048)
        self.assertTrue(result["preview_only"])
        self.assertLessEqual(len(decode_payload(result)), 2048)
        self.assertEqual((result["width"], result["height"]), (64, 32))
        self.assertEqual((result["original_width"], result["original_height"]), (200, 100))

    def test_large_transparent_png_is_bounded_without_flattening(self):
        original = image_bytes("PNG", size=(200, 100), mode="RGBA", compress_level=0)
        with patch.object(self.module, "MAX_INLINE_BYTES", 2048), patch.object(self.module, "MAX_PREVIEW_EDGE", 64):
            result = self.encode(original)
        self.assertTrue(result["preview_only"])
        self.assertLessEqual(len(decode_payload(result)), 2048)
        with Image.open(io.BytesIO(decode_payload(result))) as decoded:
            self.assertEqual(decoded.size, (64, 32))
            self.assertEqual(decoded.getpixel((0, 0))[3], 80)

    def test_real_twelve_mib_threshold_uses_converted_preview(self):
        original = image_bytes("BMP", size=(2300, 2000))
        self.assertGreater(len(original), 12 * 1024 * 1024)
        result = self.encode(original)
        self.assertTrue(result["preview_only"])
        self.assertLessEqual(len(decode_payload(result)), 12 * 1024 * 1024)
        self.assertEqual((result["original_width"], result["original_height"]), (2300, 2000))

    def test_conversion_respects_real_4096_edge_without_changing_original(self):
        original = image_bytes("TIFF", size=(5000, 50))
        result = self.encode(original)
        self.assertTrue(result["preview_only"])
        self.assertEqual(max(result["width"], result["height"]), 4096)
        self.assertEqual(result["original_width"], 5000)
        self.assertEqual(self.path.read_bytes(), original)

    def test_real_input_limit_is_checked_before_file_read(self):
        with self.path.open("wb") as output:
            output.truncate(self.module.MAX_INPUT_BYTES + 1)
        with patch.object(pathlib.Path, "open", side_effect=AssertionError("must not read oversized file")):
            with self.assertRaises(self.module.GalleryPreviewError) as caught:
                self.module.build_gallery_image(self.path)
        self.assertEqual(caught.exception.status, 413)
        self.assertIn("下载原图", str(caught.exception))

    def test_read_is_bounded_even_if_file_grows_after_stat(self):
        fake_path = Mock()
        fake_path.stat.return_value.st_size = 1
        source = io.BytesIO(b"x" * 20)
        fake_path.open.return_value.__enter__ = Mock(return_value=source)
        fake_path.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(self.module, "MAX_INPUT_BYTES", 10):
            with self.assertRaises(self.module.GalleryPreviewError) as caught:
                self.module.build_gallery_image(fake_path)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(source.tell(), 11)

    def test_pixel_limit_precedes_decode(self):
        self.path.write_bytes(image_bytes())
        image = Mock(size=(8000, 5000))
        opened = Mock()
        opened.__enter__ = Mock(return_value=image)
        opened.__exit__ = Mock(return_value=False)
        with patch.object(self.module.Image, "open", return_value=opened):
            with self.assertRaises(self.module.GalleryPreviewError) as caught:
                self.module.build_gallery_image(self.path)
        self.assertEqual(caught.exception.status, 413)
        image.load.assert_not_called()
        image.getexif.assert_not_called()

    def test_encoding_failure_reports_download_without_private_path(self):
        with patch.object(self.module, "MAX_INLINE_BYTES", 1):
            with self.assertRaises(self.module.GalleryPreviewError) as caught:
                self.encode(image_bytes("TIFF"))
        self.assertEqual(caught.exception.status, 413)
        self.assertIn("下载原图", str(caught.exception))
        self.assertNotIn(self.directory.name, str(caught.exception))

    def test_empty_and_corrupt_bytes_are_user_safe_errors(self):
        for content in (b"", b"not an image", image_bytes()[:40]):
            with self.subTest(content_length=len(content)):
                with self.assertRaises(self.module.GalleryPreviewError) as caught:
                    self.encode(content)
                self.assertNotIn(self.directory.name, str(caught.exception))


class GenerationFullImageApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = load_module("data_manager").DataManager(pathlib.Path(self.directory.name), {})
        self.original = image_bytes("PNG")
        self.source = image_bytes("JPEG", size=(32, 64))
        self.record = await self.manager.save_generation_record(
            self.original, prompt="gallery fixture", user_id="test-user", reference_images=[self.source],
        )
        self.api = self.module.LinghuiDashboardApi(types.SimpleNamespace(data_mgr=self.manager))
        self.args = {"id": self.record["id"]}
        self.request_patch = patch.object(self.module, "request", types.SimpleNamespace(args=self.args))
        self.json_patch = patch.object(self.module, "jsonify", side_effect=lambda payload: payload)
        self.request_patch.start()
        self.json_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.addCleanup(self.json_patch.stop)

    async def get_image(self, **params):
        self.args.clear()
        self.args.update({"id": self.record["id"], **params})
        response, status, headers = await self.api.generation_full_image()
        self.assertEqual(headers["Cache-Control"], "no-store")
        return response, status

    async def test_default_and_zero_index_return_exact_result(self):
        for params in ({}, {"source_index": "0"}):
            response, status = await self.get_image(**params)
            self.assertEqual(status, 200)
            self.assertTrue(response["success"])
            self.assertEqual(response["source_index"], 0)
            self.assertEqual(response["id"], self.record["id"])
            self.assertEqual(decode_payload(response), self.original)

    async def test_source_returns_actual_input_not_result(self):
        response, status = await self.get_image(source_index="1")
        self.assertEqual(status, 200)
        self.assertEqual(response["source_index"], 1)
        self.assertEqual(decode_payload(response), self.source)
        self.assertEqual((response["width"], response["height"]), (32, 64))

    async def test_source_index_is_strict_and_never_clamped(self):
        for index in ("", "-1", "1000", "1.5", "abc", " 1", "+1", "01", None):
            with self.subTest(index=index):
                response, status = await self.get_image(source_index=index)
                self.assertEqual(status, 400)
                self.assertFalse(response["success"])
        _, status = await self.get_image(source_index="999")
        self.assertEqual(status, 404)

    async def test_invalid_and_missing_record_ids(self):
        for record_id in ("", "../private", "a" * 81):
            with self.subTest(record_id=record_id):
                _, status = await self.get_image(id=record_id)
                self.assertEqual(status, 400)
        _, status = await self.get_image(id="nonexistent")
        self.assertEqual(status, 404)

    async def test_missing_output_and_source_caches_are_safe_404(self):
        self.manager.get_generation_image_path(self.record).unlink()
        self.manager.get_generation_source_path(self.record, 1).unlink()
        for index in ("0", "1"):
            response, status = await self.get_image(source_index=index)
            self.assertEqual(status, 404)
            self.assertNotIn(self.directory.name, str(response))

    async def test_record_path_escape_is_not_exposed(self):
        self.manager.generation_history[0]["filename"] = "../outside.png"
        response, status = await self.get_image()
        self.assertEqual(status, 404)
        self.assertNotIn("outside", str(response))

    async def test_encoding_errors_are_forwarded_with_no_store(self):
        error = self.module.GalleryPreviewError("图片超过预览限制，请下载原图查看。", 413)
        with patch.object(self.module, "build_gallery_image", side_effect=error):
            response, status = await self.get_image()
        self.assertEqual(status, 413)
        self.assertIn("下载原图", response["message"])

    async def test_file_disappearing_during_load_does_not_expose_path(self):
        with patch.object(self.module, "build_gallery_image", side_effect=FileNotFoundError("private/server/path")):
            response, status = await self.get_image()
        self.assertEqual(status, 404)
        self.assertNotIn("private", str(response))

    async def test_blocking_work_is_off_loop_and_at_most_two_at_once(self):
        loop_thread = threading.get_ident()
        active = 0
        maximum = 0
        worker_ids = set()
        lock = threading.Lock()
        release = threading.Event()

        def build(_path):
            nonlocal active, maximum
            with lock:
                worker_ids.add(threading.get_ident())
                active += 1
                maximum = max(active, maximum)
            release.wait(3)
            with lock:
                active -= 1
            return {"image": "data:image/png;base64,eA==", "width": 1, "height": 1,
                    "original_width": 1, "original_height": 1, "preview_only": False}

        with patch.object(self.module, "build_gallery_image", side_effect=build):
            tasks = [asyncio.create_task(self.api.generation_full_image()) for _ in range(4)]
            try:
                for _ in range(100):
                    if maximum == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(maximum, 2)
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)
        self.assertNotIn(loop_thread, worker_ids)
        self.assertTrue(all(result[1] == 200 for result in results))

    async def test_cancelled_request_keeps_its_decoder_slot_until_completion(self):
        started = 0
        lock = threading.Lock()
        release = threading.Event()

        def build(_path):
            nonlocal started
            with lock:
                started += 1
            release.wait(3)
            return {"image": "data:image/png;base64,eA=="}

        with patch.object(self.module, "build_gallery_image", side_effect=build):
            tasks = [asyncio.create_task(self.api.generation_full_image()) for _ in range(2)]
            try:
                for _ in range(100):
                    if started == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(started, 2)
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                tasks.append(asyncio.create_task(self.api.generation_full_image()))
                await asyncio.sleep(0.05)
                self.assertEqual(started, 2)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(started, 3)


if __name__ == "__main__":
    unittest.main()

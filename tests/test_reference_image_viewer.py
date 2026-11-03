import pathlib
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_generation_full_image import decode_payload, image_bytes, load_module


class ReferenceImageApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_module("dashboard_api")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = load_module("data_manager").DataManager(pathlib.Path(self.directory.name), {})
        self.original = image_bytes("PNG", size=(80, 160), mode="RGBA")
        self.preset_image = image_bytes("JPEG", size=(160, 80))
        await self.manager.save_preset_ref_image("_persona_", self.original)
        await self.manager.save_preset_ref_image("测试预设", self.preset_image)
        self.api = self.module.LinghuiDashboardApi(types.SimpleNamespace(data_mgr=self.manager))
        self.args = {"preset": "_persona_", "index": "0"}
        request_patch = patch.object(self.module, "request", types.SimpleNamespace(args=self.args))
        json_patch = patch.object(self.module, "jsonify", side_effect=lambda payload: payload)
        request_patch.start()
        json_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(json_patch.stop)

    async def test_persona_and_preset_views_return_their_exact_original(self):
        for preset, original, size in (("_persona_", self.original, (80, 160)), ("测试预设", self.preset_image, (160, 80))):
            self.args["preset"] = preset
            response, status, headers = await self.api.reference_full_image()
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(decode_payload(response), original)
            self.assertEqual((response["width"], response["height"]), size)

    async def test_download_sends_original_attachment_and_keeps_file_unchanged(self):
        path = pathlib.Path(self.manager.get_preset_ref_image_paths("_persona_")[0])
        file_response = types.SimpleNamespace(headers={})
        with patch.object(self.module, "send_file", new=AsyncMock(return_value=file_response)) as send:
            response, status, headers = await self.api.reference_download()
        self.assertIs(response, file_response)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(send.call_args.args, (path,))
        self.assertTrue(send.call_args.kwargs["as_attachment"])
        self.assertEqual(send.call_args.kwargs["attachment_filename"], "linghui_persona_1.png")
        self.assertEqual(path.read_bytes(), self.original)

    async def test_converted_view_does_not_change_tiff_download(self):
        original = image_bytes("TIFF")
        filename = await self.manager.save_preset_ref_image("_persona_", original)
        self.args["index"] = "1"
        response, status, _ = await self.api.reference_full_image()
        self.assertEqual(status, 200)
        self.assertTrue(response["preview_only"])
        with patch.object(self.module, "send_file", new=AsyncMock()) as send:
            await self.api.reference_download()
        path = send.call_args.args[0]
        self.assertEqual(path.name, filename)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(send.call_args.kwargs["attachment_filename"], "linghui_persona_2.tiff")

    async def test_invalid_indices_and_missing_references_are_not_clamped(self):
        for endpoint in (self.api.reference_full_image, self.api.reference_download):
            for index in ("", "-1", "1000", "1.5", "abc", " 0", "+0", "00", None):
                self.args["index"] = index
                response, status, _ = await endpoint()
                self.assertEqual(status, 400)
                self.assertFalse(response["success"])
            self.args["index"] = "9"
            _, status, _ = await endpoint()
            self.assertEqual(status, 404)
            self.args.update(preset="not-registered", index="0")
            _, status, _ = await endpoint()
            self.assertEqual(status, 404)
            self.args.update(preset="_persona_", index="0")

    async def test_registered_path_escape_is_never_viewed_or_downloaded(self):
        outside = pathlib.Path(self.directory.name) / "private.png"
        outside.write_bytes(self.original)
        self.manager.preset_ref_images["_persona_"] = ["../private.png"]
        for endpoint in (self.api.reference_full_image, self.api.reference_download):
            response, status, _ = await endpoint()
            self.assertEqual(status, 404)
            self.assertNotIn("private", str(response))

    async def test_missing_file_during_view_or_download_returns_safe_error(self):
        for function, endpoint in (("build_gallery_image", self.api.reference_full_image), ("send_file", self.api.reference_download)):
            with patch.object(self.module, function, side_effect=FileNotFoundError("private/path.png")):
                response, status, headers = await endpoint()
            self.assertEqual(status, 404)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn("private", str(response))

    async def test_oversized_view_keeps_original_download_available(self):
        error = self.module.GalleryPreviewError("图片超过预览限制，请下载原图查看。", 413)
        with patch.object(self.module, "build_gallery_image", side_effect=error):
            response, status, _ = await self.api.reference_full_image()
        self.assertEqual(status, 413)
        self.assertIn("下载原图", response["message"])
        with patch.object(self.module, "send_file", new=AsyncMock()):
            _, status, _ = await self.api.reference_download()
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()

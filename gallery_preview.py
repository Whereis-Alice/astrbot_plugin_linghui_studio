"""Bounded, on-demand gallery images for the authenticated Dashboard bridge."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_INLINE_BYTES = 12 * 1024 * 1024
MAX_PREVIEW_EDGE = 4096
_BROWSER_MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}


class GalleryPreviewError(ValueError):
    """An intentionally user-safe failure with no filesystem details."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _encode_preview(image: Image.Image) -> tuple[bytes, str, int, int]:
    """Keep transparency, correct orientation, and bound converted payloads."""
    image = ImageOps.exif_transpose(image)
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    image = image.convert("RGBA" if has_alpha else "RGB")
    image.thumbnail((MAX_PREVIEW_EDGE, MAX_PREVIEW_EDGE), Image.Resampling.LANCZOS)
    image_format = "PNG" if has_alpha else "JPEG"
    mime = "image/png" if has_alpha else "image/jpeg"
    while True:
        output = io.BytesIO()
        options = {"compress_level": 6} if has_alpha else {"quality": 88}
        image.save(output, format=image_format, **options)
        encoded = output.getvalue()
        if len(encoded) <= MAX_INLINE_BYTES:
            return encoded, mime, image.width, image.height
        edge = max(image.size)
        if edge <= 256:
            raise GalleryPreviewError("图片无法在大图预览限制内显示，请下载原图查看。", 413)
        next_edge = max(256, int(edge * 0.75))
        image.thumbnail((next_edge, next_edge), Image.Resampling.LANCZOS)


def build_gallery_image(path: Path) -> dict[str, Any]:
    """Read one safe cache path; callers must run this blocking work off-loop.

    Input bytes and decoded pixels are hard limits. Only conversion output is
    resized: ordinary browser-supported files retain their exact original bytes.
    """
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise GalleryPreviewError("图片超过 32 MiB 大图预览限制，请下载原图查看。", 413)
    with path.open("rb") as source_file:
        original = source_file.read(MAX_INPUT_BYTES + 1)
    if len(original) > MAX_INPUT_BYTES:
        raise GalleryPreviewError("图片超过 32 MiB 大图预览限制，请下载原图查看。", 413)
    if not original:
        raise GalleryPreviewError("原图缓存为空，请重新生成或下载其他图片。")

    try:
        with Image.open(io.BytesIO(original)) as source:
            original_width, original_height = source.size
            if original_width * original_height > MAX_IMAGE_PIXELS:
                raise GalleryPreviewError("图片超过 3200 万像素大图预览限制，请下载原图查看。", 413)
            mime = _BROWSER_MIMES.get(source.format or "")
            orientation = source.getexif().get(274, 1)
            preview_only = mime is None or orientation not in (None, 1) or len(original) > MAX_INLINE_BYTES
            # Validate the first frame before returning bytes, without decoding
            # every frame of an animation. The pixel guard runs before load().
            source.load()
            if preview_only:
                encoded, mime, width, height = _encode_preview(source)
            else:
                encoded = original
                width, height = source.size
    except GalleryPreviewError:
        raise
    except Image.DecompressionBombError as exc:
        raise GalleryPreviewError("图片超过安全解码限制，请下载原图查看。", 413) from exc
    except Exception as exc:
        raise GalleryPreviewError("该图片无法生成大图预览，请下载原图查看。") from exc

    return {
        "image": f"data:{mime};base64,{base64.b64encode(encoded).decode('ascii')}",
        "width": width,
        "height": height,
        "original_width": original_width,
        "original_height": original_height,
        "preview_only": preview_only,
    }

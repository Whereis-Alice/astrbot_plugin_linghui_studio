"""Prepare bounded QQ delivery copies without changing generation originals."""

import asyncio
import base64
import copy
import io
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrbot.api.message_components import Image as MessageImage
from PIL import Image, ImageOps

MAX_DECODE_PIXELS = 40_000_000
MAX_INPUT_BYTES = 64 * 1024 * 1024


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """Parse a bounded option, including invalid legacy configuration values."""
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def qq_delivery_enabled(config: Mapping[str, Any], platform_name: str) -> bool:
    """Enable delivery copies only for the actual OneBot adapter type."""
    return (
        platform_name == "aiocqhttp"
        and config.get("qq_image_delivery_mode", "auto") == "auto"
    )


@dataclass(frozen=True)
class DeliveryCopy:
    data: bytes
    reason: str
    width: int = 0
    height: int = 0


def prepare_delivery_copy(data: bytes, config: Mapping[str, Any]) -> DeliveryCopy:
    """Reduce oversized static images; preserve alpha, animation and originals.

    The byte target is best effort. JPEG quality drops by at most five points;
    dimensions only shrink when above the explicitly configured maximum edge.
    Decode/encode failures leave delivery using the original bytes.
    """
    if not data or len(data) > MAX_INPUT_BYTES:
        return DeliveryCopy(data, "input_limit")
    target = (
        bounded_int(config.get("qq_image_delivery_max_mb", 2), 2, 1, 20) * 1024 * 1024
    )
    edge = bounded_int(config.get("qq_image_delivery_max_edge", 4096), 4096, 512, 8192)
    quality = bounded_int(config.get("qq_image_delivery_jpeg_quality", 95), 95, 80, 100)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width, height = source.size
                if width * height > MAX_DECODE_PIXELS:
                    return DeliveryCopy(data, "pixel_limit", width, height)
                if getattr(source, "is_animated", False):
                    return DeliveryCopy(data, "animated", width, height)
                if len(data) <= target and max(width, height) <= edge:
                    return DeliveryCopy(data, "within_target", width, height)
                source.load()
                picture = ImageOps.exif_transpose(source)
                resized = max(picture.size) > edge
                if resized:
                    picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                alpha = "A" in picture.getbands() or "transparency" in picture.info
                output = io.BytesIO()
                if alpha:
                    # Never flatten a transparent image onto an arbitrary background.
                    picture.convert("RGBA").save(output, "PNG", compress_level=6)
                    candidate = output.getvalue()
                else:
                    rgb = picture.convert("RGB")
                    # ICC describes RGB bytes only; a CMYK profile cannot be reused.
                    profile = (
                        picture.info.get("icc_profile")
                        if picture.mode == "RGB"
                        else None
                    )
                    options = {"icc_profile": profile} if profile else {}
                    candidate = data
                    for current_quality in range(quality, max(80, quality - 5) - 1, -2):
                        output.seek(0)
                        output.truncate()
                        rgb.save(
                            output,
                            "JPEG",
                            quality=current_quality,
                            subsampling=0,
                            **options,
                        )
                        candidate = output.getvalue()
                        if len(candidate) <= target:
                            break
                # Avoid a larger replacement. An explicit edge limit still applies.
                if not resized and len(candidate) >= len(data):
                    return DeliveryCopy(data, "original_smaller", width, height)
                return DeliveryCopy(
                    candidate, "resized" if resized else "reencoded", *picture.size
                )
    except Exception:  # noqa: BLE001 - optional encoding must not break original delivery
        return DeliveryCopy(data, "processing_failed")


def _prepare_inline_chain(
    chain: Any, config: Mapping[str, Any]
) -> tuple[Any, list[dict[str, Any]]]:
    nodes = list(chain.chain)
    stats = []
    for index, node in enumerate(nodes):
        if not isinstance(node, MessageImage):
            continue
        source = node.url or node.file or ""
        # Generated results use inline bytes. Never fetch arbitrary message URLs.
        if not source.startswith("base64://"):
            continue
        if len(source) > (MAX_INPUT_BYTES + 2) // 3 * 4 + 9:
            stats.append({"reason": "input_limit"})
            continue
        try:
            data = base64.b64decode(source[9:], validate=True)
        except (ValueError, TypeError):
            stats.append({"reason": "invalid_base64"})
            continue
        result = prepare_delivery_copy(data, config)
        stats.append(
            {
                "original_bytes": len(data),
                "send_bytes": len(result.data),
                "reason": result.reason,
                "width": result.width,
                "height": result.height,
            }
        )
        if result.data is not data:
            replacement = copy.deepcopy(node)
            replacement.file = "base64://" + base64.b64encode(result.data).decode(
                "ascii"
            )
            replacement.url = ""
            replacement.path = ""
            nodes[index] = replacement
    prepared = copy.copy(chain)
    prepared.chain = nodes
    return prepared, stats


async def prepare_generated_chain(
    chain: Any, config: Mapping[str, Any], platform_name: str
) -> Any:
    """Prepare a private message copy off the event loop and log transfer size."""
    if not qq_delivery_enabled(config, platform_name):
        return chain
    from astrbot.api import logger

    started = time.perf_counter()
    try:
        prepared, stats = await asyncio.to_thread(
            _prepare_inline_chain, chain, dict(config)
        )
    except Exception:  # noqa: BLE001 - fall back when message copying or encoding fails
        logger.warning("Linghui QQ delivery preparation failed; using original message")
        return chain
    for item in stats:
        logger.info(
            "Linghui QQ delivery copy: original_bytes=%s send_bytes=%s size=%sx%s reason=%s prepare_ms=%.1f",
            item.get("original_bytes", 0),
            item.get("send_bytes", 0),
            item.get("width", 0),
            item.get("height", 0),
            item["reason"],
            (time.perf_counter() - started) * 1000,
        )
    return prepared

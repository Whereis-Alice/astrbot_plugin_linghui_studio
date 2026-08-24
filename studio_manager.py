"""Server-side reference image slots for the Dashboard image workbench."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image as PILImage
from PIL import ImageOps
from astrbot import logger


STUDIO_SLOTS = {
    "identity": "身份",
    "outfit": "服装",
    "pose": "姿势",
    "scene": "场景",
    "background": "底图",
    "style": "风格",
    "detail": "细节",
}


class StudioAssetManager:
    """Store validated reference images without exposing local paths."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.assets_dir = self.data_dir / "studio_assets"
        self.index_file = self.data_dir / "studio_assets.json"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.assets: Dict[str, List[Dict[str, Any]]] = {slot: [] for slot in STUDIO_SLOTS}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.index_file.is_file():
            return
        try:
            raw = await asyncio.to_thread(self.index_file.read_text, encoding="utf-8")
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                return
            for slot in STUDIO_SLOTS:
                items = loaded.get(slot, [])
                if not isinstance(items, list):
                    continue
                self.assets[slot] = [
                    self._normalize_item(item)
                    for item in items[:32]
                    if isinstance(item, dict) and self._normalize_item(item)
                ]
        except Exception as exc:
            logger.warning("Linghui could not load studio assets: %s", exc)

    @staticmethod
    def _safe_label(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:120]

    @staticmethod
    def _safe_id(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:80]

    def _normalize_item(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        asset_id = self._safe_id(raw.get("id"))
        filename = Path(str(raw.get("filename", "") or "")).name
        if not asset_id or not filename or not (self.assets_dir / filename).is_file():
            return None
        return {
            "id": asset_id,
            "filename": filename,
            "label": self._safe_label(raw.get("label")),
            "created_at": str(raw.get("created_at", "") or "")[:80],
            "width": max(0, int(raw.get("width", 0) or 0)),
            "height": max(0, int(raw.get("height", 0) or 0)),
            "image_format": str(raw.get("image_format", "") or "")[:20],
            "size_bytes": max(0, int(raw.get("size_bytes", 0) or 0)),
            "source_record_id": self._safe_id(raw.get("source_record_id")),
        }

    async def _save_locked(self) -> None:
        payload = json.dumps(self.assets, ensure_ascii=False, indent=2)
        temp = self.index_file.with_suffix(".json.tmp")

        def write() -> None:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.index_file)

        await asyncio.to_thread(write)

    @staticmethod
    def _inspect(data: bytes) -> tuple[str, str, int, int]:
        if not data or len(data) > 10 * 1024 * 1024:
            raise ValueError("图片为空或超过 10 MB。")
        try:
            with PILImage.open(io.BytesIO(data)) as source:
                image = ImageOps.exif_transpose(source)
                image.verify()
                image_format = str(source.format or "PNG").upper()
                width, height = source.size
        except Exception as exc:
            raise ValueError(f"无法识别图片：{exc}") from exc
        suffixes = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
        return suffixes.get(image_format, "png"), image_format, int(width), int(height)

    @staticmethod
    def validate_slot(slot: Any) -> str:
        value = str(slot or "").strip().lower()
        if value not in STUDIO_SLOTS:
            raise ValueError("无效的工作台参考图槽位。")
        return value

    async def add_image(
        self,
        slot: str,
        data: bytes,
        *,
        label: str = "",
        source_record_id: str = "",
    ) -> Dict[str, Any]:
        slot = self.validate_slot(slot)
        suffix, image_format, width, height = await asyncio.to_thread(self._inspect, data)
        asset_id = uuid.uuid4().hex
        filename = f"{slot}_{asset_id}.{suffix}"
        path = self.assets_dir / filename
        item = {
            "id": asset_id,
            "filename": filename,
            "label": self._safe_label(label) or STUDIO_SLOTS[slot],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "width": width,
            "height": height,
            "image_format": image_format,
            "size_bytes": len(data),
            "source_record_id": self._safe_id(source_record_id),
        }
        async with self._lock:
            if len(self.assets[slot]) >= 32:
                raise ValueError("每个工作台槽位最多保存 32 张参考图。")
            await asyncio.to_thread(path.write_bytes, data)
            self.assets[slot].append(item)
            await self._save_locked()
        return dict(item)

    async def remove_image(self, slot: str, asset_id: str) -> bool:
        slot = self.validate_slot(slot)
        asset_id = self._safe_id(asset_id)
        async with self._lock:
            for index, item in enumerate(self.assets[slot]):
                if item.get("id") != asset_id:
                    continue
                path = self.assets_dir / Path(str(item.get("filename", ""))).name
                try:
                    await asyncio.to_thread(path.unlink)
                except FileNotFoundError:
                    pass
                self.assets[slot].pop(index)
                await self._save_locked()
                return True
        return False

    async def reorder(self, slot: str, ordered_ids: List[str]) -> List[Dict[str, Any]]:
        slot = self.validate_slot(slot)
        normalized = [self._safe_id(item) for item in ordered_ids if self._safe_id(item)]
        async with self._lock:
            by_id = {item.get("id"): item for item in self.assets[slot]}
            if set(normalized) != set(by_id):
                raise ValueError("排序列表与当前槽位图片不一致，请刷新后重试。")
            self.assets[slot] = [by_id[item] for item in normalized]
            await self._save_locked()
            return [dict(item) for item in self.assets[slot]]

    async def clear_slot(self, slot: str) -> int:
        slot = self.validate_slot(slot)
        async with self._lock:
            items = list(self.assets[slot])
            self.assets[slot] = []
            for item in items:
                try:
                    await asyncio.to_thread((self.assets_dir / Path(item["filename"]).name).unlink)
                except FileNotFoundError:
                    pass
            await self._save_locked()
            return len(items)

    def public_summary(self) -> Dict[str, Any]:
        return {
            "slots": [
                {
                    "id": slot,
                    "label": label,
                    "count": len(self.assets.get(slot, [])),
                    "items": [
                        {key: value for key, value in item.items() if key != "filename"}
                        for item in self.assets.get(slot, [])
                    ],
                }
                for slot, label in STUDIO_SLOTS.items()
            ],
            "order": list(STUDIO_SLOTS),
        }

    def get_asset_path(self, slot: str, asset_id: str) -> Optional[Path]:
        try:
            slot = self.validate_slot(slot)
        except ValueError:
            return None
        asset_id = self._safe_id(asset_id)
        item = next((item for item in self.assets.get(slot, []) if item.get("id") == asset_id), None)
        if item is None:
            return None
        path = (self.assets_dir / Path(str(item.get("filename", ""))).name).resolve()
        try:
            root = self.assets_dir.resolve()
        except OSError:
            return None
        return path if root in path.parents and path.is_file() else None

    async def load_selected_images(self, selections: List[Dict[str, Any]]) -> List[bytes]:
        images: List[bytes] = []
        for selection in selections[:32]:
            if not isinstance(selection, dict):
                continue
            path = self.get_asset_path(selection.get("slot", ""), selection.get("id", ""))
            if path is None:
                continue
            try:
                images.append(await asyncio.to_thread(path.read_bytes))
            except OSError:
                continue
        return [item for item in images if item]

    async def close(self) -> None:
        # Reserved for future thumbnail/background workers.
        return None

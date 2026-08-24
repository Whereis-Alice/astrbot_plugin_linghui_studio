"""Persistent generation task registry for Linghui Studio."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from astrbot import logger

from .error_classify import classify_generation_error, safe_error_summary
from .utils import norm_id


ACTIVE_TASK_STATES = {"queued", "running", "generated", "sending"}
FINAL_TASK_STATES = {"succeeded", "failed", "cancelled", "expired", "send_failed", "possibly_sent"}
ALL_TASK_STATES = ACTIVE_TASK_STATES | FINAL_TASK_STATES


class GenerationTaskManager:
    """Track, deduplicate, cancel, retry, and inspect image requests.

    Request images are stored in a task-scoped directory so an administrator
    can force a rerun after a provider failure without exposing API keys to the
    browser. Generated outputs continue to live in DataManager's protected
    generation history rather than being duplicated here.
    """

    FILE_VERSION = 1

    def __init__(self, data_dir: Path, config: Any):
        self.data_dir = Path(data_dir)
        self.config = config
        self.tasks_file = self.data_dir / "generation_tasks.json"
        self.request_cache_dir = self.data_dir / "generation_task_requests"
        self.request_cache_dir.mkdir(parents=True, exist_ok=True)
        self.tasks: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._runtime_tasks: Dict[str, asyncio.Task] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _parse_time(value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_text(value: Any, limit: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]

    def _max_tasks(self) -> int:
        try:
            return min(max(50, int(self.config.get("task_history_limit", 500))), 5_000)
        except (TypeError, ValueError):
            return 500

    def _dedup_seconds(self) -> int:
        try:
            return min(max(0, int(self.config.get("task_dedup_seconds", 180))), 86_400)
        except (TypeError, ValueError):
            return 180

    def _request_retention_days(self) -> int:
        try:
            return min(max(1, int(self.config.get("task_request_retention_days", 7))), 90)
        except (TypeError, ValueError):
            return 7

    async def initialize(self) -> None:
        if self.tasks_file.is_file():
            try:
                raw = await asyncio.to_thread(self.tasks_file.read_text, encoding="utf-8")
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    loaded = loaded.get("tasks", [])
                if isinstance(loaded, list):
                    self.tasks = [self._normalize_task(item) for item in loaded if isinstance(item, dict)]
            except Exception as exc:
                logger.error("Linghui could not load generation tasks: %s", exc)
                self.tasks = []

        changed = False
        expired_at = self._now()
        for task in self.tasks:
            if task.get("status") in ACTIVE_TASK_STATES:
                task["status"] = "expired"
                task["finished_at"] = expired_at
                task["updated_at"] = expired_at
                task["error_category"] = "restart"
                task["error"] = "插件重启前任务未正常结束，已标记为过期。"
                changed = True

        if await self._prune_locked():
            changed = True
        if changed:
            await self._save_locked()

    def _normalize_task(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        status = str(raw.get("status", "failed") or "failed").strip().lower()
        if status not in ALL_TASK_STATES:
            status = "failed"
        attempts = raw.get("attempt_chain", [])
        if not isinstance(attempts, list):
            attempts = []
        return {
            "id": self._safe_text(raw.get("id"), 80) or uuid.uuid4().hex,
            "fingerprint": self._safe_text(raw.get("fingerprint"), 128),
            "request_id": self._safe_text(raw.get("request_id"), 160),
            "status": status,
            "task_type": self._safe_text(raw.get("task_type"), 100),
            "session_id": self._safe_text(raw.get("session_id"), 500),
            "user_id": norm_id(raw.get("user_id")),
            "group_id": norm_id(raw.get("group_id")),
            "user_name": self._safe_text(raw.get("user_name"), 160),
            "group_name": self._safe_text(raw.get("group_name"), 160),
            "prompt": str(raw.get("prompt", "") or "").strip()[:12_000],
            "preset": self._safe_text(raw.get("preset"), 160),
            "requested_model": self._safe_text(raw.get("requested_model"), 200),
            "actual_model": self._safe_text(raw.get("actual_model"), 200),
            "channel_id": self._safe_text(raw.get("channel_id"), 80),
            "channel_name": self._safe_text(raw.get("channel_name"), 160),
            "created_at": self._safe_text(raw.get("created_at"), 80) or self._now(),
            "updated_at": self._safe_text(raw.get("updated_at"), 80) or self._now(),
            "started_at": self._safe_text(raw.get("started_at"), 80),
            "finished_at": self._safe_text(raw.get("finished_at"), 80),
            "duration": max(0.0, float(raw.get("duration", 0.0) or 0.0)),
            "error": safe_error_summary(raw.get("error", ""), 600),
            "error_category": self._safe_text(raw.get("error_category"), 80),
            "attempt_chain": attempts[:64],
            "result_record_id": self._safe_text(raw.get("result_record_id"), 80),
            "delivery_status": self._safe_text(raw.get("delivery_status"), 40) or "pending",
            "input_count": max(0, int(raw.get("input_count", 0) or 0)),
            "force": bool(raw.get("force", False)),
            "rerun_of": self._safe_text(raw.get("rerun_of"), 80),
            "progress": self._normalize_progress(raw.get("progress", {})),
            "request": raw.get("request", {}) if isinstance(raw.get("request"), dict) else {},
        }

    @staticmethod
    def _normalize_progress(raw: Any) -> Dict[str, int]:
        raw = raw if isinstance(raw, dict) else {}
        result: Dict[str, int] = {}
        for key in ("current", "total", "success", "fail"):
            try:
                result[key] = max(0, int(raw.get(key, 0) or 0))
            except (TypeError, ValueError):
                result[key] = 0
        return result

    async def _save_locked(self) -> None:
        payload = json.dumps(
            {"version": self.FILE_VERSION, "tasks": self.tasks},
            ensure_ascii=False,
            indent=2,
        )
        temp = self.tasks_file.with_suffix(".json.tmp")

        def write() -> None:
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.tasks_file)

        await asyncio.to_thread(write)

    @staticmethod
    def build_fingerprint(
        *,
        request_id: str = "",
        session_id: str = "",
        user_id: str = "",
        task_type: str = "",
        prompt: str = "",
        model: str = "",
        images: Iterable[bytes] = (),
        nonce: str = "",
    ) -> str:
        digest = hashlib.sha256()
        for value in (request_id, session_id, user_id, task_type, prompt, model, nonce):
            digest.update(str(value or "").encode("utf-8", "ignore"))
            digest.update(b"\0")
        for image in images:
            if isinstance(image, bytes) and image:
                digest.update(hashlib.sha256(image).digest())
            digest.update(b"\0")
        return digest.hexdigest()

    async def begin_task(
        self,
        *,
        request_id: str = "",
        session_id: str = "",
        user_id: str = "",
        group_id: str = "",
        user_name: str = "",
        group_name: str = "",
        task_type: str = "",
        prompt: str = "",
        preset: str = "",
        requested_model: str = "",
        images: Optional[List[bytes]] = None,
        request: Optional[Dict[str, Any]] = None,
        force: bool = False,
        nonce: str = "",
        rerun_of: str = "",
    ) -> Tuple[Dict[str, Any], bool]:
        image_list = [item for item in (images or []) if isinstance(item, bytes) and item]
        fingerprint = self.build_fingerprint(
            request_id=request_id,
            session_id=session_id,
            user_id=user_id,
            task_type=task_type,
            prompt=prompt,
            model=requested_model,
            images=image_list,
            nonce=nonce,
        )
        now = datetime.now()
        dedup_cutoff = now - timedelta(seconds=self._dedup_seconds())

        async with self._lock:
            if not force and self._dedup_seconds() > 0:
                for existing in self.tasks:
                    if existing.get("fingerprint") != fingerprint:
                        continue
                    created_at = self._parse_time(existing.get("created_at"))
                    if created_at is None or created_at < dedup_cutoff:
                        continue
                    if existing.get("status") in ACTIVE_TASK_STATES | {"succeeded", "possibly_sent"}:
                        return dict(existing), True

            task_id = uuid.uuid4().hex
            timestamp = self._now()
            task = self._normalize_task({
                "id": task_id,
                "fingerprint": fingerprint,
                "request_id": request_id,
                "status": "running",
                "task_type": task_type,
                "session_id": session_id,
                "user_id": user_id,
                "group_id": group_id,
                "user_name": user_name,
                "group_name": group_name,
                "prompt": prompt,
                "preset": preset,
                "requested_model": requested_model,
                "created_at": timestamp,
                "updated_at": timestamp,
                "started_at": timestamp,
                "input_count": len(image_list),
                "force": force,
                "rerun_of": rerun_of,
                "request": request or {},
            })
            self.tasks.insert(0, task)
            await self._write_request_images_locked(task_id, image_list)
            await self._prune_locked()
            await self._save_locked()
            return dict(task), False

    async def _write_request_images_locked(self, task_id: str, images: List[bytes]) -> None:
        if not images:
            return
        target = self.request_cache_dir / task_id
        target.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(images, start=1):
            await asyncio.to_thread((target / f"source_{index:03d}.img").write_bytes, image)

    async def get_request_images(self, task_id: str) -> List[bytes]:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(task_id or ""))[:80]
        if not safe_id:
            return []
        directory = self.request_cache_dir / safe_id
        try:
            paths = sorted(path for path in directory.glob("source_*.img") if path.is_file())
        except OSError:
            return []
        images: List[bytes] = []
        for path in paths[:32]:
            try:
                images.append(await asyncio.to_thread(path.read_bytes))
            except OSError:
                continue
        return [item for item in images if item]

    async def attach_runtime_task(self, task_id: str, runtime_task: asyncio.Task) -> None:
        async with self._lock:
            if task_id and runtime_task is not None:
                self._runtime_tasks[task_id] = runtime_task

    async def detach_runtime_task(self, task_id: str, runtime_task: Optional[asyncio.Task] = None) -> None:
        async with self._lock:
            current = self._runtime_tasks.get(task_id)
            if current is not None and (runtime_task is None or current is runtime_task):
                self._runtime_tasks.pop(task_id, None)

    def _find_locked(self, task_id: str) -> Optional[Dict[str, Any]]:
        return next((item for item in self.tasks if item.get("id") == task_id), None)

    async def update_task(self, task_id: str, **changes: Any) -> Optional[Dict[str, Any]]:
        async with self._lock:
            task = self._find_locked(task_id)
            if task is None:
                return None
            for key, value in changes.items():
                if key == "progress":
                    task[key] = self._normalize_progress(value)
                elif key == "attempt_chain":
                    task[key] = list(value or [])[:64]
                elif key in {"error", "prompt"}:
                    task[key] = str(value or "")[: (600 if key == "error" else 12_000)]
                elif key in task:
                    task[key] = value
            task["updated_at"] = self._now()
            await self._save_locked()
            return dict(task)

    async def finish_success(
        self,
        task_id: str,
        *,
        metrics: Optional[Dict[str, Any]] = None,
        result_record_id: str = "",
        delivery_status: str = "sent",
    ) -> Optional[Dict[str, Any]]:
        metrics = metrics if isinstance(metrics, dict) else {}
        started_at: Optional[datetime] = None
        async with self._lock:
            task = self._find_locked(task_id)
            if task is None:
                return None
            started_at = self._parse_time(task.get("started_at"))
            now = datetime.now()
            task.update({
                "status": "possibly_sent" if delivery_status == "possibly_sent" else "succeeded",
                "finished_at": self._now(),
                "updated_at": self._now(),
                "duration": max(0.0, (now - started_at).total_seconds()) if started_at else 0.0,
                "actual_model": self._safe_text(metrics.get("model"), 200),
                "channel_id": self._safe_text(metrics.get("channel_id"), 80),
                "channel_name": self._safe_text(metrics.get("channel_name"), 160),
                "attempt_chain": list(metrics.get("attempt_chain", []) or [])[:64],
                "result_record_id": self._safe_text(result_record_id, 80),
                "delivery_status": delivery_status,
                "error": "",
                "error_category": "",
            })
            await self._save_locked()
            return dict(task)

    async def mark_generated(
        self,
        task_id: str,
        *,
        metrics: Optional[Dict[str, Any]] = None,
        result_record_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        metrics = metrics if isinstance(metrics, dict) else {}
        return await self.update_task(
            task_id,
            status="generated",
            actual_model=self._safe_text(metrics.get("model"), 200),
            channel_id=self._safe_text(metrics.get("channel_id"), 80),
            channel_name=self._safe_text(metrics.get("channel_name"), 160),
            attempt_chain=list(metrics.get("attempt_chain", []) or [])[:64],
            result_record_id=self._safe_text(result_record_id, 80),
            delivery_status="pending",
        )

    async def finish_failure(
        self,
        task_id: str,
        error: Any,
        *,
        metrics: Optional[Dict[str, Any]] = None,
        status: str = "failed",
        delivery_status: str = "failed",
    ) -> Optional[Dict[str, Any]]:
        classification = classify_generation_error(error)
        metrics = metrics if isinstance(metrics, dict) else {}
        async with self._lock:
            task = self._find_locked(task_id)
            if task is None:
                return None
            started_at = self._parse_time(task.get("started_at"))
            now = datetime.now()
            safe_status = status if status in FINAL_TASK_STATES else "failed"
            task.update({
                "status": safe_status,
                "finished_at": self._now(),
                "updated_at": self._now(),
                "duration": max(0.0, (now - started_at).total_seconds()) if started_at else 0.0,
                "actual_model": self._safe_text(metrics.get("model"), 200),
                "channel_id": self._safe_text(metrics.get("channel_id"), 80),
                "channel_name": self._safe_text(metrics.get("channel_name"), 160),
                "attempt_chain": list(metrics.get("attempt_chain", []) or [])[:64],
                "delivery_status": delivery_status,
                "error": safe_error_summary(error, 600),
                "error_category": classification.category,
            })
            await self._save_locked()
            return dict(task)

    async def update_progress(
        self,
        task_id: str,
        *,
        current: Optional[int] = None,
        total: Optional[int] = None,
        success: Optional[int] = None,
        fail: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            task = self._find_locked(task_id)
            if task is None:
                return None
            progress = self._normalize_progress(task.get("progress", {}))
            for key, value in (("current", current), ("total", total), ("success", success), ("fail", fail)):
                if value is not None:
                    progress[key] = max(0, int(value))
            task["progress"] = progress
            task["updated_at"] = self._now()
            await self._save_locked()
            return dict(task)

    async def cancel_task(self, task_id: str) -> Tuple[bool, str]:
        runtime_task: Optional[asyncio.Task]
        async with self._lock:
            task = self._find_locked(task_id)
            if task is None:
                return False, "未找到任务。"
            if task.get("status") not in ACTIVE_TASK_STATES:
                return False, "该任务已经结束，无法取消。"
            runtime_task = self._runtime_tasks.get(task_id)
            task["status"] = "cancelled"
            task["finished_at"] = self._now()
            task["updated_at"] = self._now()
            task["delivery_status"] = "cancelled"
            task["error_category"] = "cancelled"
            task["error"] = "管理员取消了该任务。"
            await self._save_locked()
        if runtime_task is not None and not runtime_task.done():
            runtime_task.cancel()
        return True, "任务已取消。"

    async def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            task = self._find_locked(str(task_id or "").strip())
            return dict(task) if task is not None else None

    async def list_tasks(
        self,
        *,
        limit: int = 40,
        offset: int = 0,
        status: str = "all",
    ) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
        try:
            limit = min(max(1, int(limit)), 100)
        except (TypeError, ValueError):
            limit = 40
        try:
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            offset = 0
        status = str(status or "all").strip().lower()
        async with self._lock:
            summary: Dict[str, int] = {"total": len(self.tasks), "active": 0}
            for task in self.tasks:
                key = str(task.get("status", "failed"))
                summary[key] = summary.get(key, 0) + 1
                if key in ACTIVE_TASK_STATES:
                    summary["active"] += 1
            filtered = self.tasks if status == "all" else [item for item in self.tasks if item.get("status") == status]
            return [dict(item) for item in filtered[offset: offset + limit]], len(filtered), summary

    async def _prune_locked(self) -> bool:
        changed = False
        max_tasks = self._max_tasks()
        if len(self.tasks) > max_tasks:
            active = [task for task in self.tasks if task.get("status") in ACTIVE_TASK_STATES]
            final = [task for task in self.tasks if task.get("status") not in ACTIVE_TASK_STATES]
            removed = final[max(0, max_tasks - len(active)):]
            kept_final = final[: max(0, max_tasks - len(active))]
            self.tasks = active + kept_final
            for task in removed:
                await self._remove_request_cache_locked(str(task.get("id", "")))
            changed = bool(removed)

        cutoff = datetime.now() - timedelta(days=self._request_retention_days())
        known_ids = {str(task.get("id", "")) for task in self.tasks}
        try:
            directories = [path for path in self.request_cache_dir.iterdir() if path.is_dir()]
        except OSError:
            directories = []
        for directory in directories:
            task = next((item for item in self.tasks if item.get("id") == directory.name), None)
            finished = self._parse_time(task.get("finished_at")) if task else None
            try:
                modified = datetime.fromtimestamp(directory.stat().st_mtime)
            except OSError:
                modified = datetime.min
            if directory.name not in known_ids or ((finished or modified) < cutoff and not task.get("status") in ACTIVE_TASK_STATES):
                await asyncio.to_thread(shutil.rmtree, directory, True)
                changed = True
        return changed

    async def _remove_request_cache_locked(self, task_id: str) -> None:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(task_id or ""))[:80]
        if not safe_id:
            return
        await asyncio.to_thread(shutil.rmtree, self.request_cache_dir / safe_id, True)

    async def cleanup(self) -> Dict[str, int]:
        async with self._lock:
            before = len(self.tasks)
            changed = await self._prune_locked()
            if changed:
                await self._save_locked()
            return {"before": before, "after": len(self.tasks), "removed": before - len(self.tasks)}

    async def close(self) -> None:
        async with self._lock:
            runtime_tasks = list(self._runtime_tasks.values())
            self._runtime_tasks.clear()
        for task in runtime_tasks:
            if not task.done():
                task.cancel()

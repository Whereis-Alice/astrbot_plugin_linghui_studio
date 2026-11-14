"""Recover evidenced channel outcomes from retained task/output histories once.

This does not infer requests from quota totals. A route contains one outcome per
consecutive channel visit, even when several models or keys were attempted.
Incomplete cancelled visits and missing latency measurements stay unknown.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from .error_classify import safe_error_summary


def _text(value: Any, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit]


def _duration(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _epoch(row: dict[str, Any], source: str) -> float | None:
    keys = ("finished_at", "updated_at", "started_at", "created_at") if source == "task" else ("created_at",)
    for key in keys:
        value = row.get(key)
        if isinstance(value, bool) or not value:
            continue
        try:
            # Older output records used local naive ISO timestamps. They are
            # interpreted in the same server timezone in which they were saved.
            parsed = (float(value) if isinstance(value, (int, float)) else
                      datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return None


def _route(row: Any, source: str) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    identity = _text(row.get("id"))
    timestamp = _epoch(row, source)
    attempts = row.get("attempt_chain")
    if not identity or timestamp is None or not isinstance(attempts, list) or not attempts:
        return None
    checked = []
    for raw in attempts:
        # Invalid entries can conceal a channel boundary or an unfinished
        # retry. Reject that route rather than joining unrelated visits.
        if not isinstance(raw, dict) or not isinstance(raw.get("success"), bool):
            return None
        channel_id = _text(raw.get("channel_id"))
        if not channel_id:
            return None
        checked.append({
            "channel_id": channel_id, "success": raw["success"],
            "duration": _duration(raw.get("duration")),
            "error_category": _text(raw.get("error_category")),
            "error": safe_error_summary(raw.get("error", ""), 240),
            "model": _text(raw.get("model"), 200),
            "key_attempt": _text(raw.get("key_attempt")),
        })
    if source == "record" and not checked[-1]["success"]:
        # An output row proves an image exists, but a contradictory old chain
        # cannot identify its actual winning provider reliably.
        return None
    groups: list[list[dict[str, Any]]] = []
    for attempt in checked:
        if not groups or groups[-1][-1]["channel_id"] != attempt["channel_id"]:
            groups.append([])
        groups[-1].append(attempt)
    status = str(row.get("status", "") or "").lower()
    if source == "task" and status in {"cancelled", "expired", "queued", "running"} and not groups[-1][-1]["success"]:
        # Earlier channels finished before the router advanced. The last
        # failed model/key may still have been followed by an interrupted retry.
        groups.pop()
    if not groups:
        return None
    fallback_count = _duration(row.get("fallback_count"))
    return {
        "id": identity, "timestamp": timestamp, "source": source,
        "task_id": _text(row.get("task_id")),
        "result_record_id": _text(row.get("result_record_id")),
        "groups": groups,
        "fallback": fallback_count is not None and fallback_count > 0,
        "signature": tuple((a["channel_id"], a["success"], a["duration"], a["model"], a["key_attempt"])
                           for a in checked),
    }


def _unique_routes(rows: Any, source: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows if isinstance(rows, list) else []:
        route = _route(row, source)
        if route is not None:
            old = unique.get(route["id"])
            if old is None or (route["timestamp"], len(route["signature"])) > (old["timestamp"], len(old["signature"])):
                unique[route["id"]] = route
    return list(unique.values())


def _empty_state() -> dict[str, Any]:
    return {
        "successes": 0, "failures": 0, "fallback_successes": 0,
        "consecutive_failures": 0, "cooldown_until": 0.0,
        "last_error_category": "", "last_error": "",
        "last_success_at": 0.0, "last_failure_at": 0.0, "durations": [],
    }


def reconstruct_channel_health(tasks: Any, generation_history: Any) -> dict[str, Any]:
    """Return bounded health state reconstructed only from retained evidence.

    Task chains take precedence for the same route. Output IDs and explicit
    task links suppress duplicate successes, while distinct batch output IDs
    remain separate routes even if they share one aggregate task. Historical
    errors never reactivate cooldown. Missing measurements do not become zero.
    """
    task_routes = _unique_routes(tasks, "task")
    records = _unique_routes(generation_history, "record")
    by_id = {r["id"]: r for r in records}
    by_task: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["task_id"]:
            by_task.setdefault(record["task_id"], []).append(record)
    paired_records: set[str] = set()
    routes = []
    for task in sorted(task_routes, key=lambda r: (r["timestamp"], r["id"])):
        linked = by_task.get(task["id"], [])
        counterpart = by_id.get(task["result_record_id"])
        if counterpart is None:
            matching = [r for r in linked if r["signature"] == task["signature"]]
            if matching:
                counterpart = min(matching, key=lambda r: (abs(r["timestamp"] - task["timestamp"]), r["id"]))
            elif len(linked) == 1:
                counterpart = linked[0]
            elif len(linked) > 1:
                # An aggregate task without an exact output link cannot be
                # counted in addition to its individually evidenced outputs.
                continue
        if counterpart is not None:
            if counterpart["id"] in paired_records:
                continue
            paired_records.add(counterpart["id"])
            task["fallback"] = task["fallback"] or counterpart["fallback"]
        routes.append(task)
    routes.extend(record for record in records if record["id"] not in paired_records)
    routes.sort(key=lambda r: (r["timestamp"], r["source"], r["id"]))
    channels: dict[str, dict[str, Any]] = {}
    for route in routes:
        previous_channels: set[str] = set()
        for group in route["groups"]:
            last = group[-1]
            channel_id = last["channel_id"]
            state = channels.setdefault(channel_id, _empty_state())
            if last["success"]:
                state["successes"] += 1
                if previous_channels - {channel_id} or route["fallback"]:
                    state["fallback_successes"] += 1
                state["consecutive_failures"] = 0
                state["last_success_at"] = route["timestamp"]
                if all(attempt["duration"] is not None for attempt in group):
                    total = sum(attempt["duration"] for attempt in group)
                    if math.isfinite(total):
                        state["durations"] = (state["durations"] + [total])[-200:]
            else:
                state["failures"] += 1
                state["consecutive_failures"] += 1
                state["last_failure_at"] = route["timestamp"]
                state["last_error_category"] = last["error_category"]
                state["last_error"] = last["error"]
            previous_channels.add(channel_id)
    return {
        "channels": channels, "recovered_routes": len(routes),
        "recovered_since": min((route["timestamp"] for route in routes), default=0.0),
    }

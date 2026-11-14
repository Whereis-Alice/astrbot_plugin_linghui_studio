"""Real channel aggregates survive hot reload without replay or fabricated samples."""

import asyncio
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_linghui_features import _load_module


class ChannelHealthStoreTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_module("channel_health")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "channel_health.json"
        self.wall = 1000.0
        self.monotonic = 25.0

    def store(self):
        return self.module.ChannelHealthStore(self.path,
            wall_clock=lambda: self.wall, monotonic_clock=lambda: self.monotonic)

    def test_cooldown_uses_elapsed_wall_time_across_process_clock_reset(self):
        first = self.store()
        state = first.state("orb")
        state.update(successes=3, failures=2, consecutive_failures=2, cooldown_until=85.0,
                     last_failure_at=995.0, durations=[2.0, 4.0, 8.0])
        first.changed()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["channels"]["orb"]["cooldown_until"], 1060.0)
        self.wall = 1020.0
        self.monotonic = 5.0
        second = self.store()
        self.assertEqual(second.channels["orb"]["cooldown_until"], 45.0)
        self.assertEqual(second.channels["orb"]["successes"], 3)
        self.assertEqual(second.channels["orb"]["durations"], [2.0, 4.0, 8.0])
        self.wall = 1061.0
        third = self.store()
        self.assertEqual(third.channels["orb"]["cooldown_until"], 0.0)

    def test_seed_keeps_unknown_latency_unknown_and_does_not_start_fresh_cooldown(self):
        store = self.store()
        self.assertTrue(store.seed_history({"channels": {
            "orb": {"successes": 2, "fallback_successes": 1, "durations": [7.0], "last_success_at": 950},
            "miku": {"failures": 1, "consecutive_failures": 1, "last_failure_at": 960,
                     "last_error_category": "timeout", "last_error": "HTTP 524 timeout", "cooldown_until": 99999},
        }, "recovered_routes": 3, "recovered_since": 900}))
        restored = self.store()
        self.assertFalse(restored.needs_history_seed)
        self.assertEqual(restored.channels["orb"]["successes"], 2)
        self.assertEqual(restored.channels["orb"]["fallback_successes"], 1)
        self.assertEqual(restored.channels["orb"]["durations"], [7.0])
        self.assertEqual(restored.channels["miku"]["failures"], 1)
        self.assertEqual(restored.channels["miku"]["cooldown_until"], 0.0)
        self.assertEqual(restored.statistics_since, 900.0)
        self.assertEqual(restored.history_records, 3)
        self.assertFalse(restored.seed_history({"channels": {"orb": {"successes": 20}}, "recovered_routes": 1}))
        self.assertEqual(restored.channels["orb"]["successes"], 2)

    def test_atomic_write_failure_preserves_disk_memory_and_retries_without_recounting(self):
        store = self.store()
        store.state("orb")["successes"] = 1
        store.changed()
        before = self.path.read_bytes()
        store.channels["orb"]["successes"] += 1
        with patch.object(self.module.os, "replace", side_effect=OSError("disk unavailable")):
            store.changed()
            self.assertFalse(store.metadata()["persistence_ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(store.channels["orb"]["successes"], 2)
        self.assertTrue(store.metadata()["persistence_ok"])
        self.assertFalse(store.dirty)
        self.assertEqual(self.store().channels["orb"]["successes"], 2)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_partially_invalid_rows_keep_good_statistics_and_bound_samples(self):
        self.path.write_text(json.dumps({"version": 1, "channels": {
            "orb": {"successes": 5, "failures": 2, "fallback_successes": 12,
                    "consecutive_failures": 99, "durations": [float("nan"), -1, 2, float("inf")]},
            "broken": "invalid", "": {},
            "miku": {"successes": "4", "durations": list(range(220))},
        }}))
        store = self.store()
        self.assertEqual(set(store.channels), {"orb", "miku"})
        self.assertEqual(store.channels["orb"]["successes"], 5)
        self.assertEqual(store.channels["orb"]["fallback_successes"], 5)
        self.assertEqual(store.channels["orb"]["consecutive_failures"], 2)
        self.assertEqual(store.channels["orb"]["durations"], [2.0])
        self.assertEqual(store.channels["miku"]["durations"], list(range(20, 220)))
        self.assertTrue(store.load_warning)
        self.assertFalse(store.needs_history_seed)

    def test_unreadable_file_is_not_silently_overwritten_by_a_dashboard_read(self):
        original = b'{"version":1,"channels":{"orb":'
        self.path.write_bytes(original)
        store = self.store()
        self.assertTrue(store.needs_history_seed)
        self.assertTrue(store.metadata()["load_warning"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(store.seed_history({"channels": {"orb": {"successes": 1}}, "recovered_since": 980}))
        self.assertEqual(self.store().channels["orb"]["successes"], 1)

    def test_missing_file_empty_seed_is_persistent_and_does_not_create_fake_samples(self):
        store = self.store()
        self.assertTrue(store.seed_history({"channels": {}, "recovered_routes": 0}))
        restored = self.store()
        self.assertFalse(restored.needs_history_seed)
        self.assertEqual(restored.channels, {})
        self.assertEqual(restored.history_records, 0)
        self.assertTrue(restored.metadata()["persistent"])

    def test_wholly_invalid_channel_mapping_can_recover_history(self):
        self.path.write_text(json.dumps({"version": 1, "history_seeded": True, "channels": {"orb": "broken"}}))
        store = self.store()
        self.assertTrue(store.needs_history_seed)
        store.seed_history({"channels": {"orb": {"successes": 3}}, "recovered_routes": 3})
        self.assertEqual(self.store().channels["orb"]["successes"], 3)


class ChannelHealthRouterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_module("channel_router")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data_dir = Path(self.directory.name)
        self.conf = {"channel_failure_threshold": 1, "channel_cooldown_seconds": 90,
            "drawing_channels": [{"id": "miku", "api_keys": "fake-key", "model": "model"},
                                 {"id": "orb", "api_keys": "fake-key", "model": "model"}]}

    def router(self):
        router = self.module.DrawingChannelRouter(self.conf)
        router.bind_daily_quota(self.data_dir)
        self.addAsyncCleanup(router.close)
        return router

    def snapshot(self, router):
        return {row["id"]: row for row in router.get_health_snapshot()["channels"]}

    async def test_real_fallback_counts_and_latency_survive_refresh_and_router_recreation(self):
        first = self.router()
        async def client(channel):
            async def generate(*args, **kwargs):
                await asyncio.sleep(0)
                return "HTTP 524 timeout" if channel["id"] == "miku" else b"image"
            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})
        first._client_for = client
        self.assertEqual(await first.call_api([], "cat", ""), b"image")
        before = self.snapshot(first)
        self.assertEqual((before["miku"]["failures"], before["orb"]["successes"], before["orb"]["fallback_successes"]), (1, 1, 1))
        self.assertTrue(before["miku"]["cooling_down"])
        await first.refresh()
        self.assertEqual(self.snapshot(first)["orb"]["successes"], 1)
        second = self.router()
        after = self.snapshot(second)
        self.assertEqual((after["miku"]["failures"], after["orb"]["successes"], after["orb"]["fallback_successes"]), (1, 1, 1))
        self.assertTrue(after["miku"]["cooling_down"])
        self.assertEqual(after["orb"]["p50_duration"], before["orb"]["p50_duration"])
        self.assertTrue(second.get_health_snapshot()["persistence_ok"])

    async def test_concurrent_completed_requests_are_saved_without_lost_counters(self):
        router = self.router()
        async def client(channel):
            async def generate(*args, **kwargs):
                await asyncio.sleep(0)
                return b"image"
            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})
        router._client_for = client
        results = await asyncio.gather(*[router.call_api([], "cat", "", preferred_channel_id="orb", allow_fallback=False)
                                        for _ in range(20)])
        self.assertEqual(results, [b"image"] * 20)
        restored = self.router()
        self.assertEqual(self.snapshot(restored)["orb"]["successes"], 20)
        self.assertEqual(len(restored._health["orb"]["durations"]), 20)

    async def test_selected_channel_models_and_keys_are_one_channel_outcome(self):
        self.conf["drawing_channels"][0].update(models=["a", "b"], api_keys="key-one\nkey-two")
        router = self.router()
        async def client(channel):
            async def generate(images, prompt, model, *args, **kwargs):
                return "HTTP 404 model_not_found" if model == "a" else b"image"
            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})
        router._client_for = client
        self.assertEqual(await router.call_api([], "cat", "", preferred_channel_id="miku", allow_fallback=False), b"image")
        restored = self.router()
        row = self.snapshot(restored)["miku"]
        self.assertEqual((row["successes"], row["failures"], row["fallback_successes"]), (1, 0, 0))

    async def test_success_latency_retains_latest_200_samples(self):
        router = self.router()
        for duration in range(220):
            router._record_success("orb", duration, False)
        restored = self.router()
        self.assertEqual(restored._health["orb"]["durations"], list(range(20, 220)))
        self.assertEqual(self.snapshot(restored)["orb"]["successes"], 220)

    async def test_removed_channel_statistics_do_not_resurrect_dashboard_cards(self):
        router = self.router()
        router._health_store.seed_history({"channels": {
            "old-gif": {"successes": 7, "durations": [8.0]},
            "orb": {"successes": 3, "durations": [5.0]},
        }, "recovered_routes": 10, "recovered_since": 1000})
        self.assertEqual(set(self.snapshot(router)), {"miku", "orb"})
        restored = self.router()
        self.assertEqual(restored._health["old-gif"]["successes"], 7)
        self.conf["drawing_channels"].append({"id": "old-gif", "api_keys": "fake-key", "model": "model"})
        self.assertEqual(self.snapshot(restored)["old-gif"]["successes"], 7)
        self.assertEqual(self.snapshot(restored)["orb"]["duration_sample_count"], 1)

    async def test_failed_health_write_does_not_turn_generated_image_into_failure(self):
        router = self.router()
        async def client(channel):
            return types.SimpleNamespace(call_api=lambda *args, **kwargs: asyncio.sleep(0, result=b"image"),
                                         get_last_metrics=lambda: {})
        router._client_for = client
        health_module = _load_module("channel_health")
        with patch.object(health_module.os, "replace", side_effect=OSError("disk temporarily unavailable")):
            # Quota has its own required persistence contract; only health save is optional.
            with patch.object(router.daily_quota, "_save"):
                self.assertEqual(await router.call_api([], "cat", ""), b"image")
            self.assertFalse(router.get_health_snapshot()["persistence_ok"])
            self.assertEqual(self.snapshot(router)["miku"]["successes"], 1)
        self.assertTrue(router.get_health_snapshot()["persistence_ok"])
        restored = self.router()
        self.assertEqual(self.snapshot(restored)["miku"]["successes"], 1)

    async def test_evidenced_history_is_seeded_once_even_after_history_retention_changes(self):
        router = self.router()
        chain = [{"channel_id": "miku", "success": False, "duration": 4.0,
                  "error_category": "timeout", "error": "HTTP 524 timeout"},
                 {"channel_id": "orb", "success": True, "duration": 12.0}]
        task = {"id": "task", "status": "succeeded", "finished_at": "2026-11-14T00:00:00+00:00",
                "attempt_chain": chain, "result_record_id": "output"}
        record = {"id": "output", "task_id": "task", "created_at": "2026-11-14T00:00:00+00:00",
                  "attempt_chain": chain, "fallback_count": 1}
        self.assertTrue(router.restore_health_history([task], [record]))
        first = self.snapshot(router)
        self.assertEqual((first["miku"]["failures"], first["orb"]["successes"], first["orb"]["fallback_successes"]), (1, 1, 1))
        self.assertEqual(first["orb"]["p50_duration"], 12.0)
        self.assertFalse(first["miku"]["cooling_down"])
        restored = self.router()
        self.assertFalse(restored.restore_health_history([task], [record]))
        self.assertFalse(restored.restore_health_history([], []))
        self.assertEqual(self.snapshot(restored), first)
        self.assertEqual(restored.get_health_snapshot()["history_records"], 1)


if __name__ == "__main__":
    unittest.main()

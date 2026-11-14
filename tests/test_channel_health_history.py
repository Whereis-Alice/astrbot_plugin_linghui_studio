"""Restore only evidenced channel visits without replay, duplicate outputs or fake timings."""

import copy
from datetime import datetime, timedelta, timezone
import math
import unittest

from test_linghui_features import _load_module


_MISSING = object()


def attempt(channel="orb", success=True, duration=1.0, **extra):
    row = {"channel_id": channel, "success": success, **extra}
    if duration is not _MISSING:
        row["duration"] = duration
    return row


def task(identity, chain, *, timestamp="2026-11-14T00:00:00+00:00", **extra):
    return {"id": identity, "finished_at": timestamp, "status": "failed", "attempt_chain": chain, **extra}


def record(identity, chain, *, timestamp="2026-11-14T00:00:00+00:00", **extra):
    return {"id": identity, "created_at": timestamp, "attempt_chain": chain, **extra}


class ChannelHealthHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reconstruct = staticmethod(_load_module("channel_health_history").reconstruct_channel_health)

    def test_models_and_key_retries_form_one_successful_channel_visit(self):
        result = self.reconstruct([task("one", [
            attempt(success=False, duration=2, model="first", key_attempt=1),
            attempt(success=False, duration=3, model="first", key_attempt=2),
            attempt(duration=7, model="best", key_attempt=1),
        ])], [])
        orb = result["channels"]["orb"]
        self.assertEqual((orb["successes"], orb["failures"], orb["fallback_successes"]), (1, 0, 0))
        self.assertEqual(orb["durations"], [12])
        self.assertEqual(result["recovered_routes"], 1)

    def test_failed_channel_visit_then_backup_success_has_same_live_aggregation(self):
        result = self.reconstruct([task("one", [
            attempt("m", False, 1, error="first failure"),
            attempt("m", False, 2, error_category="timeout", error="HTTP 524 at https://private.example.com/?key=secret-value-1 sk-veryprivate1234"),
            attempt("miku", True, 3),
        ])], [])
        primary, backup = result["channels"]["m"], result["channels"]["miku"]
        self.assertEqual((primary["successes"], primary["failures"], primary["consecutive_failures"]), (0, 1, 1))
        self.assertEqual(primary["durations"], [])
        self.assertEqual(primary["cooldown_until"], 0)
        self.assertEqual(primary["last_error_category"], "timeout")
        for secret in ("private.example.com", "secret-value-1", "veryprivate1234"):
            self.assertNotIn(secret, primary["last_error"])
        self.assertEqual((backup["successes"], backup["fallback_successes"], backup["durations"]), (1, 1, [3]))

    def test_explicit_fallback_metadata_preserves_quota_skipped_primary(self):
        result = self.reconstruct([], [record("one", [attempt()], fallback_count=1)])
        self.assertEqual(result["channels"]["orb"]["fallback_successes"], 1)

    def test_send_failure_and_expired_after_generation_are_upstream_successes(self):
        rows = [task("one", [attempt()], status="send_failed"),
                task("two", [attempt()], status="expired"),
                task("three", [attempt()], status="possibly_sent")]
        result = self.reconstruct(rows, [])
        self.assertEqual(result["channels"]["orb"]["successes"], 3)
        self.assertEqual(result["channels"]["orb"]["failures"], 0)

    def test_task_chain_wins_matching_output_record_and_retains_explicit_fallback(self):
        tasks = [task("one", [attempt(duration=9)], result_record_id="image")]
        histories = [record("image", [attempt(duration=1)], task_id="one", fallback_count=1),
                     record("image", [attempt(duration=1)], task_id="one", fallback_count=1)]
        result = self.reconstruct(tasks, histories)
        self.assertEqual(result["recovered_routes"], 1)
        self.assertEqual(result["channels"]["orb"]["durations"], [9])
        self.assertEqual(result["channels"]["orb"]["fallback_successes"], 1)

    def test_task_link_deduplicates_output_without_result_record_id(self):
        result = self.reconstruct([task("one", [attempt(duration=5)])],
                                  [record("image", [attempt(duration=2)], task_id="one")])
        self.assertEqual(result["recovered_routes"], 1)
        self.assertEqual(result["channels"]["orb"]["durations"], [5])

    def test_distinct_outputs_sharing_aggregate_task_are_not_collapsed(self):
        histories = [record("a", [attempt(duration=1)], task_id="batch"),
                     record("b", [attempt(duration=2)], task_id="batch")]
        result = self.reconstruct([task("batch", [], status="succeeded")], histories)
        self.assertEqual(result["recovered_routes"], 2)
        self.assertEqual(result["channels"]["orb"]["successes"], 2)
        self.assertEqual(result["channels"]["orb"]["durations"], [1, 2])

    def test_exactly_matching_output_is_deduplicated_without_hiding_batch_siblings(self):
        histories = [record("a", [attempt(duration=1)], task_id="batch"),
                     record("b", [attempt(duration=2)], task_id="batch")]
        result = self.reconstruct([task("batch", [attempt(duration=2)])], histories)
        self.assertEqual(result["recovered_routes"], 2)
        self.assertEqual(sorted(result["channels"]["orb"]["durations"]), [1, 2])

    def test_ambiguous_aggregate_chain_does_not_add_a_third_output(self):
        histories = [record("a", [attempt(duration=1)], task_id="batch"),
                     record("b", [attempt(duration=2)], task_id="batch")]
        result = self.reconstruct([task("batch", [attempt(duration=99)])], histories)
        self.assertEqual(result["recovered_routes"], 2)
        self.assertEqual(result["channels"]["orb"]["successes"], 2)

    def test_missing_or_invalid_attempt_duration_does_not_create_zero_latency(self):
        values = [_MISSING, None, "bad", -1, float("nan"), float("inf"), True]
        rows = [task(str(index), [attempt(duration=value)]) for index, value in enumerate(values)]
        rows += [task("partial", [attempt(success=False, duration=_MISSING), attempt(duration=8)]),
                 task("real-zero", [attempt(duration=0)])]
        result = self.reconstruct(rows, [])
        self.assertEqual(result["channels"]["orb"]["successes"], len(values) + 2)
        self.assertEqual(result["channels"]["orb"]["durations"], [0])
        self.assertEqual(result["channels"]["orb"]["failures"], 0)

    def test_invalid_timestamps_and_malformed_attempts_are_not_invented(self):
        rows = [task("missing", [attempt()], timestamp=""),
                task("invalid", [attempt()], timestamp="nonsense"),
                task("nan", [attempt()], timestamp=math.nan),
                task("boolean", [attempt()], timestamp=True),
                task("bad-attempt", [{"channel_id": "orb", "success": "true"}]),
                task("bad-channel", [attempt("")]),
                task("missing-id", [attempt()], id=""),
                task("negative", [attempt()], timestamp=-1)]
        self.assertEqual(self.reconstruct(rows, []), {"channels": {}, "recovered_routes": 0, "recovered_since": 0})

    def test_timestamp_offsets_sort_recovery_and_consecutive_failures(self):
        earlier = task("success", [attempt(duration=5)], timestamp="2026-11-14T08:00:00+08:00")
        later = task("failure", [attempt(success=False, error_category="timeout", error="timed out")],
                     timestamp="2026-11-14T00:01:00Z")
        latest = task("latest", [attempt(success=False, error="still unavailable")],
                      timestamp="2026-11-14T00:02:00+00:00")
        result = self.reconstruct([latest, earlier, later], [])
        orb = result["channels"]["orb"]
        expected_epoch = datetime(2026, 11, 14, tzinfo=timezone.utc).timestamp()
        self.assertEqual(result["recovered_since"], expected_epoch)
        self.assertEqual((orb["successes"], orb["failures"], orb["consecutive_failures"]), (1, 2, 2))
        self.assertEqual(orb["last_success_at"], expected_epoch)
        self.assertEqual(orb["last_failure_at"], expected_epoch + 120)
        self.assertEqual(orb["cooldown_until"], 0)

    def test_cancelled_final_retry_is_not_a_completed_failure_but_previous_channel_is(self):
        result = self.reconstruct([
            task("one", [attempt("m", False), attempt("orb", False)], status="cancelled"),
            task("two", [attempt("orb", False)], status="expired"),
            task("three", [attempt("orb", True)], status="cancelled"),
        ], [])
        self.assertEqual(result["recovered_routes"], 2)
        self.assertEqual(result["channels"]["m"]["failures"], 1)
        self.assertEqual((result["channels"]["orb"]["successes"], result["channels"]["orb"]["failures"]), (1, 0))

    def test_duration_samples_keep_last_two_hundred_in_chronological_order(self):
        start = datetime(2026, 11, 14, tzinfo=timezone.utc)
        rows = [task(str(index), [attempt(duration=index)], timestamp=(start + timedelta(seconds=index)).isoformat())
                for index in range(250)]
        result = self.reconstruct(list(reversed(rows)), [])
        self.assertEqual(result["channels"]["orb"]["successes"], 250)
        self.assertEqual(result["channels"]["orb"]["durations"], list(range(50, 250)))

    def test_duplicate_task_ids_keep_latest_evidence_and_do_not_mutate_input(self):
        rows = [task("one", [attempt("m", False)], timestamp="2026-11-14T00:00:00Z"),
                task("one", [attempt("orb", True)], timestamp="2026-11-14T00:01:00Z")]
        histories = [record("image", [attempt("orb", True)], task_id="one")]
        before = copy.deepcopy((rows, histories))
        result = self.reconstruct(rows, histories)
        self.assertEqual(result["recovered_routes"], 1)
        self.assertEqual(set(result["channels"]), {"orb"})
        self.assertEqual((rows, histories), before)

    def test_history_without_valid_chain_never_becomes_inferred_provider_success(self):
        result = self.reconstruct([], [record("no-chain", [], channel_id="orb", fallback_count=0),
                                      record("contradiction", [attempt(success=False)], channel_id="orb")])
        self.assertEqual(result["channels"], {})
        self.assertEqual(result["recovered_routes"], 0)


if __name__ == "__main__":
    unittest.main()

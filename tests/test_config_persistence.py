import unittest

from config_persistence import (
    merge_native_fallback_snapshots,
    should_restore_fallback_value,
)

DYNAMIC_KEYS = frozenset({"active_drawing_channel", "reference_image_drawing_channel"})
DEFAULTS = {
    "active_drawing_channel": "",
    "reference_image_drawing_channel": "",
}


class DynamicConfigPolicyTest(unittest.TestCase):
    def _resolve(
        self,
        key,
        dynamic_value,
        current_config,
        *,
        has_override_meta=True,
        has_native_snapshot=False,
        native_value_at_fallback=None,
        native_config_is_newer=None,
    ):
        return should_restore_fallback_value(
            key,
            dynamic_value,
            dynamic_keys=DYNAMIC_KEYS,
            legacy_restore_keys=DYNAMIC_KEYS,
            current_config=current_config,
            has_override_meta=has_override_meta,
            schema_defaults=DEFAULTS,
            has_native_snapshot=has_native_snapshot,
            native_value_at_fallback=native_value_at_fallback,
            native_config_is_newer=native_config_is_newer,
        )

    def test_native_channel_selections_are_not_overwritten_by_old_fallbacks(self):
        current = {
            "active_drawing_channel": "miku",
            "reference_image_drawing_channel": "image-edit",
        }

        self.assertEqual(
            self._resolve("active_drawing_channel", "old-primary", current),
            (False, "keep-native-value"),
        )
        self.assertEqual(
            self._resolve("reference_image_drawing_channel", "old-reference", current),
            (False, "keep-native-value"),
        )

    def test_fallback_can_restore_a_missing_or_default_value_when_native_save_failed(self):
        self.assertEqual(
            self._resolve("active_drawing_channel", "fallback-primary", DEFAULTS),
            (True, "fallback-value"),
        )

    def test_failed_native_save_can_restore_automatic_channel_selection(self):
        # The user ran "#通道 自动" but the native save failed.  An empty
        # fallback is meaningful here: it must replace the old native channel.
        self.assertEqual(
            self._resolve(
                "active_drawing_channel",
                "",
                {"active_drawing_channel": "miku"},
                has_native_snapshot=True,
                native_value_at_fallback="miku",
            ),
            (True, "fallback-value"),
        )

    def test_later_native_config_save_wins_over_fallback_snapshot(self):
        # A save from AstrBot's own configuration page after the failed save
        # changes the native value, so it is now the source of truth.
        self.assertEqual(
            self._resolve(
                "active_drawing_channel",
                "",
                {"active_drawing_channel": "backup"},
                has_native_snapshot=True,
                native_value_at_fallback="miku",
            ),
            (False, "native-changed"),
        )

    def test_snapshot_missing_value_matches_schema_default_after_migration(self):
        self.assertEqual(
            self._resolve(
                "reference_image_drawing_channel",
                "image-edit",
                DEFAULTS,
                has_native_snapshot=True,
                native_value_at_fallback=None,
            ),
            (True, "fallback-value"),
        )

    def test_legacy_backup_uses_file_freshness_when_no_snapshot_exists(self):
        current = {"active_drawing_channel": "miku"}
        self.assertEqual(
            self._resolve(
                "active_drawing_channel",
                "old-primary",
                current,
                has_native_snapshot=False,
                native_config_is_newer=True,
            ),
            (False, "newer-native-config"),
        )
        self.assertEqual(
            self._resolve(
                "active_drawing_channel",
                "old-primary",
                current,
                has_native_snapshot=False,
                native_config_is_newer=False,
            ),
            (True, "fallback-newer"),
        )

    def test_failed_save_records_the_pre_save_native_value_for_changed_field(self):
        snapshots = merge_native_fallback_snapshots(
            {"reference_image_drawing_channel": "old-reference"},
            override_keys={"active_drawing_channel", "reference_image_drawing_channel"},
            changed_dynamic_keys={"active_drawing_channel"},
            native_values_before_save={"active_drawing_channel": "miku"},
            native_save_succeeded=False,
        )
        self.assertEqual(snapshots, {
            "active_drawing_channel": "miku",
            "reference_image_drawing_channel": "old-reference",
        })

    def test_successful_native_save_clears_fallback_snapshots(self):
        self.assertEqual(
            merge_native_fallback_snapshots(
                {"active_drawing_channel": "miku"},
                override_keys={"active_drawing_channel"},
                changed_dynamic_keys={"active_drawing_channel"},
                native_values_before_save={"active_drawing_channel": "miku"},
                native_save_succeeded=True,
            ),
            {},
        )

    def test_legacy_empty_fallback_cannot_erase_a_native_selection(self):
        self.assertEqual(
            self._resolve(
                "reference_image_drawing_channel",
                "",
                {"reference_image_drawing_channel": "image-edit"},
                has_override_meta=False,
            ),
            (False, "legacy-empty"),
        )


if __name__ == "__main__":
    unittest.main()

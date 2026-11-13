"""Keep old preset routes and image options intact when unifying their settings."""

import copy
import json
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from test_linghui_features import ROOT, _load_module


class PresetSettingsMigrationTest(unittest.TestCase):
    def setUp(self):
        self.options = _load_module("preset_options")

    @staticmethod
    def gif_row(**extra):
        return {
            "preset": "动图画",
            "image_output_format": "gif",
            "image_frame_count": 12,
            "image_frame_duration_ms": 170,
            "image_complexity": "high",
            **extra,
        }

    def split_config(self):
        return {
            "preset_generation_options": [self.gif_row()],
            "preset_channel_bindings": [
                {"preset": "动图画", "channel_ids": ["miku", "orb"]},
                {"preset": "照片", "channel_id": "m"},
            ],
            "unrelated_setting": {"keep": True},
        }

    def test_pre_schema_snapshot_survives_core_removing_obsolete_key(self):
        old = self.split_config()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_config.json"
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8-sig")
            snapshot = self.options.snapshot_native_preset_settings(path)
        # AstrBotConfig discards fields absent from the new schema before __init__.
        after_schema = {key: value for key, value in old.items() if key != "preset_channel_bindings"}
        after_schema["preset_config_version"] = 0
        self.options.restore_legacy_preset_snapshot(after_schema, snapshot)
        self.options.store_preset_settings(after_schema)
        by_name = {row["preset"]: row for row in after_schema["preset_generation_options"]}
        self.assertEqual(by_name["动图画"]["channel_ids"], ["miku", "orb"])
        self.assertEqual(by_name["照片"]["channel_ids"], ["m"])
        self.assertEqual(by_name["动图画"]["image_frame_duration_ms"], 170)
        self.assertNotIn("unrelated_setting", snapshot)
        self.assertNotIn("preset_channel_bindings", after_schema)

    def test_old_snapshot_never_revives_intentionally_unbound_new_settings(self):
        snapshot = self.split_config()
        config = {"preset_config_version": 1, "preset_generation_options": [self.gif_row(channel_ids=[])]}
        self.options.restore_legacy_preset_snapshot(config, snapshot)
        self.assertNotIn("preset_channel_bindings", config)
        self.assertEqual(self.options.preset_channels_map(config), {})

    def test_snapshot_invalid_or_missing_file_is_harmless(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_config.json"
            self.assertEqual(self.options.snapshot_native_preset_settings(path), {})
            path.write_text("broken json", encoding="utf-8")
            self.assertEqual(self.options.snapshot_native_preset_settings(path), {})

    def test_legacy_split_merge_preserves_gif_fields_and_channel_order(self):
        config = self.split_config()
        before = copy.deepcopy(config)
        rows = self.options.effective_preset_settings(config)
        by_name = {row["preset"]: row for row in rows}
        self.assertEqual(by_name["动图画"]["channel_ids"], ["miku", "orb"])
        self.assertEqual(by_name["照片"]["channel_ids"], ["m"])
        self.assertEqual(by_name["照片"]["image_output_format"], "auto")
        for field in self.options.IMAGE_OPTION_KEYS:
            self.assertEqual(by_name["动图画"][field], self.gif_row()[field])
        self.assertEqual(config, before)
        self.assertEqual(self.options.preset_channels_map(config), {
            "动图画": ["miku", "orb"], "照片": ["m"],
        })
        self.assertEqual(self.options.preset_options_map(config)["动图画"], {
            field: self.gif_row()[field] for field in self.options.IMAGE_OPTION_KEYS
        })

    def test_unmigrated_schema_injected_empty_selection_does_not_erase_legacy(self):
        for marker in (None, 0, "0"):
            with self.subTest(marker=marker):
                config = self.split_config()
                config["preset_generation_options"][0]["channel_ids"] = []
                if marker is not None:
                    config["preset_config_version"] = marker
                self.assertEqual(self.options.preset_channels_map(config)["动图画"], ["miku", "orb"])
                self.options.store_preset_settings(config)
                self.assertEqual(config["preset_generation_options"][0]["channel_ids"], ["miku", "orb"])
                self.assertEqual(config["preset_generation_options"][0]["image_output_format"], "gif")
                self.assertEqual(config["preset_config_version"], 1)
                self.assertNotIn("preset_channel_bindings", config)

    def test_migrated_explicit_empty_selection_unbinds_but_absent_field_is_compatible(self):
        config = self.split_config()
        config["preset_config_version"] = 1
        config["preset_generation_options"][0]["channel_ids"] = []
        self.assertNotIn("动图画", self.options.preset_channels_map(config))
        self.assertEqual(self.options.effective_preset_settings(config)[0]["channel_ids"], [])
        del config["preset_generation_options"][0]["channel_ids"]
        self.assertEqual(self.options.preset_channels_map(config)["动图画"], ["miku", "orb"])

    def test_normalizer_keeps_channel_ids_optional_and_preserves_empty_selection(self):
        normalize = self.options.normalize_preset_generation_options
        incoming = [self.gif_row()]
        before = copy.deepcopy(incoming)
        self.assertNotIn("channel_ids", normalize(incoming, {"动图画"})[0])
        self.assertEqual(incoming, before)
        self.assertEqual(normalize([self.gif_row(channel_ids=[])], {"动图画"})[0]["channel_ids"], [])
        self.assertEqual(normalize([self.gif_row(channel_ids=[" miku ", "orb"])], {"动图画"})[0]["channel_ids"], ["miku", "orb"])

    def test_normalizer_rejects_invalid_channel_lists_without_mutating_input(self):
        for channel_ids in (None, "miku", ("miku",), [True], [1], [""], [" "],
                            ["miku", " miku "], [str(i) for i in range(9)]):
            with self.subTest(channel_ids=channel_ids):
                incoming = [self.gif_row(channel_ids=channel_ids)]
                before = copy.deepcopy(incoming)
                with self.assertRaises(ValueError):
                    self.options.normalize_preset_generation_options(incoming, {"动图画"})
                self.assertEqual(incoming, before)

    def test_store_old_image_payload_keeps_routes_and_removes_legacy(self):
        config = self.split_config()
        incoming = [self.gif_row(image_frame_count=8)]
        before = copy.deepcopy(incoming)
        changed = self.options.store_preset_settings(config, options=incoming)
        self.assertEqual(set(changed), {
            "preset_generation_options", "preset_channel_bindings", "preset_config_version",
        })
        self.assertNotIn("preset_channel_bindings", config)
        self.assertEqual(config["preset_config_version"], 1)
        self.assertEqual(config["unrelated_setting"], {"keep": True})
        self.assertEqual(incoming, before)
        rows = {row["preset"]: row for row in config["preset_generation_options"]}
        self.assertEqual(rows["动图画"]["channel_ids"], ["miku", "orb"])
        self.assertEqual(rows["动图画"]["image_frame_count"], 8)
        self.assertEqual(rows["照片"]["channel_ids"], ["m"])
        self.assertTrue(all(row["__template_key"] == "preset_generation_option" for row in rows.values()))

    def test_store_unified_payload_can_change_or_clear_old_channel_selection(self):
        for channel_ids in (["orb", "miku"], []):
            with self.subTest(channel_ids=channel_ids):
                config = self.split_config()
                self.options.store_preset_settings(config, options=[self.gif_row(channel_ids=channel_ids)])
                self.assertEqual(config["preset_generation_options"][0]["channel_ids"], channel_ids)
                self.assertEqual(self.options.preset_channels_map(config).get("动图画", []), channel_ids)

    def test_store_empty_image_only_payload_retains_routes_with_auto_format(self):
        config = self.split_config()
        self.options.store_preset_settings(config, options=[])
        self.assertEqual(self.options.preset_channels_map(config), {"动图画": ["miku", "orb"], "照片": ["m"]})
        self.assertTrue(all(row["image_output_format"] == "auto" for row in config["preset_generation_options"]))

    def test_store_empty_options_and_bindings_delete_both_parts(self):
        config = self.split_config()
        self.options.store_preset_settings(config, options=[], bindings=[])
        self.assertEqual(config["preset_generation_options"], [])
        self.assertNotIn("preset_channel_bindings", config)
        self.assertEqual(self.options.preset_channels_map(config), {})

    def test_validation_failure_never_mutates_live_config(self):
        invalid_updates = (
            {"options": {}},
            {"options": [self.gif_row(image_frame_count=True)]},
            {"options": [self.gif_row(image_frame_duration_ms=167)]},
            {"options": [self.gif_row(channel_ids=["miku", "miku"])]},
            {"options": [self.gif_row(), self.gif_row()]},
            {"options": [self.gif_row()], "bindings": [{"preset": "动图画", "channel_ids": ["orb", "orb"]}]},
            {"bindings": [{"preset": "动图画", "channel_id": ""}]},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                config = self.split_config()
                before = copy.deepcopy(config)
                with self.assertRaises(ValueError):
                    self.options.store_preset_settings(config, **update)
                self.assertEqual(config, before)

    def test_native_schema_has_one_preset_settings_entry(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("preset_channel_bindings", schema)
        definition = schema["preset_generation_options"]
        self.assertEqual(definition["type"], "template_list")
        items = definition["templates"]["preset_generation_option"]["items"]
        self.assertEqual(items["channel_ids"]["type"], "list")
        self.assertEqual(items["channel_ids"]["default"], [])
        self.assertNotIn("channel_id", items)
        self.assertTrue(set(self.options.IMAGE_OPTION_KEYS).issubset(items))
        self.assertEqual(schema["preset_config_version"]["default"], 0)


class CanonicalPresetRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_router_reads_canonical_selection_and_falls_back_in_its_order(self):
        config = {
            "model": "default", "active_drawing_channel": "m", "preset_config_version": 1,
            "drawing_channels": [
                {"id": name, "model": "default", "api_keys": "test-key"}
                for name in ("m", "miku", "orb")
            ],
            "preset_generation_options": [PresetSettingsMigrationTest.gif_row(channel_ids=["orb", "miku"])],
        }
        router = _load_module("channel_router").DrawingChannelRouter(config)
        self.addAsyncCleanup(router.close)
        calls = []

        async def prepare(prompt):
            return prompt

        async def client_for(channel):
            async def generate(images, prompt, model, *args, **kwargs):
                calls.append((channel["id"], kwargs.get("image_options")))
                return "HTTP 503 unavailable" if channel["id"] == "orb" else b"generated"
            return types.SimpleNamespace(call_api=generate, get_last_metrics=lambda: {})

        router.prompt_processor.prepare = prepare
        router._client_for = client_for
        before = copy.deepcopy(config)
        self.assertEqual(router._preset_channel_bindings("动图画"), ["orb", "miku"])
        result = await router.call_api([], "simple loop", "default", preset_name="动图画")
        self.assertEqual(result, b"generated")
        self.assertEqual([channel for channel, _ in calls], ["orb", "miku"])
        self.assertTrue(all(options["image_output_format"] == "gif" for _, options in calls))
        self.assertEqual(config, before)


if __name__ == "__main__":
    unittest.main()

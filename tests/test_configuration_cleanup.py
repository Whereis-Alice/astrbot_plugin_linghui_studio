import copy
import unittest

from test_linghui_features import _load_module


class ConfigurationCleanupTest(unittest.TestCase):
    def setUp(self):
        self.cleanup = _load_module("configuration_cleanup")

    def test_only_retired_top_level_fields_are_removed(self):
        channel = {"id": "orb", "base_url": "https://example.com", "api_keys": "channel-secret",
                   "model": "configured-model", "image_resolution": "1K", "image_output_format": "auto"}
        config = {"drawing_channels": [channel], "base_url": "https://obsolete.example",
                  "api_keys": "old-secret", "model": "old-model", "image_resolution": "2K",
                  "preset_generation_options": [{"preset": "动图画", "image_output_format": "gif"}],
                  "persona_channel_ids": ["orb"], "allowed_users": ["10001"]}
        expected = copy.deepcopy(config)
        removed = self.cleanup.cleanup_removed_config(config)
        self.assertEqual(set(removed), {"base_url", "api_keys", "model", "image_resolution"})
        self.assertEqual(config["drawing_channels"], expected["drawing_channels"])
        self.assertEqual(config["preset_generation_options"], expected["preset_generation_options"])
        self.assertEqual(config["persona_channel_ids"], ["orb"])
        self.assertEqual(config["allowed_users"], ["10001"])
        self.assertEqual(self.cleanup.cleanup_removed_config(config), [])

    def test_cleanup_never_creates_implicit_legacy_channel(self):
        config = {"base_url": "https://obsolete.example", "api_keys": "old-secret", "drawing_channels": []}
        self.cleanup.cleanup_removed_config(config)
        self.assertEqual(config, {"drawing_channels": []})

    def test_access_lists_can_be_recovered_before_one_time_merge_and_cleanup(self):
        config = {"allowed_users": ["10001"], "blocked_users": []}
        snapshot = {"user_whitelist": ["10002"], "user_blacklist": ["10003"], "api_keys": "ignore"}
        self.cleanup.restore_legacy_access_snapshot(config, snapshot)
        self.assertEqual(config["user_whitelist"], ["10002"])
        self.assertEqual(config["user_blacklist"], ["10003"])
        self.assertNotIn("api_keys", config)
        self.cleanup.cleanup_removed_config(config)
        self.assertEqual(config, {"allowed_users": ["10001"], "blocked_users": []})


if __name__ == "__main__":
    unittest.main()

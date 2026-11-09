"""Upgrade the built-in preset pack without restoring previously deleted presets."""

import unittest

from default_presets import ADDITIONAL_DEFAULT_PRESETS, presets_for_upgrade


class DefaultPresetUpgradeTest(unittest.TestCase):
    def test_fresh_install_gets_the_complete_pack(self):
        self.assertEqual(presets_for_upgrade(0), ADDITIONAL_DEFAULT_PRESETS)

    def test_existing_install_gets_only_new_gif_preset(self):
        self.assertEqual(set(presets_for_upgrade(1)), {"GIF表情"})
        self.assertIn("animated GIF", presets_for_upgrade(1)["GIF表情"])

    def test_current_or_newer_install_does_not_reinstall_deleted_presets(self):
        self.assertEqual(presets_for_upgrade(2), {})
        self.assertEqual(presets_for_upgrade(3), {})


if __name__ == "__main__":
    unittest.main()

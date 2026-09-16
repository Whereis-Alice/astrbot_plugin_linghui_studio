import json
import pathlib
import unittest

from preset_descriptions import PRESET_DESCRIPTIONS, preset_description


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PresetDescriptionsTest(unittest.TestCase):
    def test_every_shipped_preset_has_a_short_description(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        names = []
        for raw in schema["prompt_list"]["default"]:
            separator = ":" if ":" in raw else "："
            names.append(raw.split(separator, 1)[0].strip())
        self.assertEqual(set(names), set(PRESET_DESCRIPTIONS))
        self.assertEqual(len(names), 52)
        self.assertTrue(all(preset_description(name).endswith("。") for name in names))

    def test_custom_preset_has_a_clear_fallback(self):
        self.assertIn("自定义预设", preset_description("我的新预设"))


if __name__ == "__main__":
    unittest.main()

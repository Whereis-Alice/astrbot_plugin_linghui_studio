from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = ROOT / "pages" / "linghui-studio"


class WebUiThemeAssetsTest(unittest.TestCase):
    def test_alice_skin_and_brand_sprite_are_packaged(self):
        script = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
        html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
        stylesheet = (PAGE_DIR / "style.css").read_text(encoding="utf-8")
        api = (ROOT / "dashboard_api.py").read_text(encoding="utf-8")
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        sprite = PAGE_DIR / "assets" / "alice-sprite.png"

        self.assertIn('"alice"', script)
        self.assertIn('changeTheme', script)
        self.assertIn('apiPost("dashboard_theme"', script)
        self.assertIn('id="theme-select"', html)
        self.assertIn('value="alice"', html)
        self.assertIn('src="./assets/alice-sprite.png"', html)
        self.assertIn('html[data-theme="alice"]', stylesheet)
        self.assertIn('backdrop-filter: blur', stylesheet)
        self.assertNotIn('gradient(', stylesheet)
        self.assertIn('save_dashboard_theme', api)
        self.assertEqual(schema["dashboard_theme"]["default"], "dark")
        self.assertTrue(sprite.is_file())
        self.assertEqual(sprite.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

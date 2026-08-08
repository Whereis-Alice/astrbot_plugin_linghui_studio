from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "pages" / "linghui-studio" / "app.js"
INDEX_HTML = ROOT / "pages" / "linghui-studio" / "index.html"


class WebUiConfirmationTest(unittest.TestCase):
    def test_reference_actions_use_the_embedded_confirmation_dialog(self):
        script = APP_JS.read_text(encoding="utf-8")
        html = INDEX_HTML.read_text(encoding="utf-8")

        self.assertNotIn("window.confirm(", script)
        self.assertIn('id="action-confirm"', html)
        self.assertIn('async function deleteReference', script)
        self.assertIn('async function clearReference', script)
        self.assertGreaterEqual(script.count("await confirmAction("), 5)
        self.assertIn('data-delete-reference=', script)
        self.assertIn('data-clear-ref="_persona_"', html)

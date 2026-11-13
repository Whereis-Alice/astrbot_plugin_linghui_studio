"""Keep the user command manual complete as registered commands evolve."""

import ast
from pathlib import Path
import unittest

from test_linghui_features import _load_module

ROOT = Path(__file__).resolve().parents[1]


class CommandDocumentationTest(unittest.TestCase):
    def test_registered_commands_and_aliases_have_a_manual_entry(self):
        manual = "\n".join((ROOT / "docs" / name).read_text(encoding="utf-8")
                           for name in ("COMMANDS.md", "ADMIN_COMMANDS.md"))
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        for method in ast.walk(tree):
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in method.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "command"):
                    names = [ast.literal_eval(decorator.args[0])]
                    for keyword in decorator.keywords:
                        if keyword.arg == "alias":
                            names.extend(ast.literal_eval(keyword.value))
                    for name in names:
                        self.assertIn(name, manual, f"Missing command/alias: {name}")

    def test_documented_channel_option_examples_match_the_parser(self):
        extract = _load_module("channel_policy").extract_channel_option
        for option in ("渠道m", "--渠道 m", "--渠道=m", "--channel m", "--channel=m"):
            with self.subTest(option=option):
                self.assertEqual(extract(option + " 一只猫"), ("一只猫", "m"))


if __name__ == "__main__":
    unittest.main()

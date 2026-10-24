import ast
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "linghui_persona_planner_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE, package)
module = importlib.import_module(PACKAGE + ".persona_planner")


class PersonaPlannerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"persona_expression_mode": "diverse"}
        self.planner = module.PersonaPhotoPlanner(Path(self.temp.name), self.config)
        self.state = {"outfit": "白色针织衫", "mood": "安静放松，浅浅微笑"}

    async def plan(self, **kwargs):
        return await self.planner.plan(session=kwargs.pop("session", "group-a"), request=kwargs.pop("request", "来张自拍"),
            count=kwargs.pop("count", 1), daily_state=self.state, **kwargs)

    async def test_tool_decisions_are_preserved_without_changing_day(self):
        direction = [{"outfit": "深蓝色外套", "expression": "认真专注的眼神", "gaze": "看向镜头", "pose": "倚靠栏杆"}]
        token, photos = await self.plan(photos=direction)
        self.assertEqual(photos, direction)
        self.assertEqual(self.state["outfit"], "白色针织衫")
        await self.planner.release(token)
        self.assertFalse(self.planner.path.exists())

    async def test_fixed_modes_and_explicit_tool_parameters(self):
        self.config.update(persona_outfit_mode="daily", persona_expression_mode="fixed")
        token, photos = await self.plan()
        self.assertEqual(photos[0]["outfit"], self.state["outfit"])
        self.assertEqual(photos[0]["expression"], self.state["mood"])
        await self.planner.release(token)
        token, photos = await self.plan(request="穿红色连衣裙，wink", visual_request="穿红色连衣裙，wink")
        self.assertEqual(photos[0]["outfit"], "穿红色连衣裙，wink")
        self.assertEqual(photos[0]["expression"], "穿红色连衣裙，wink")
        await self.planner.release(token)

    async def test_repetition_uses_successful_same_session_photos_and_survives_reload(self):
        token, first = await self.plan()
        await self.planner.complete_photo(token, 0, False)
        self.assertFalse(self.planner.path.exists())
        token, retry = await self.plan()
        self.assertEqual(first[0]["expression"], retry[0]["expression"])
        await self.planner.complete_photo(token, 0, True)
        self.planner = module.PersonaPhotoPlanner(Path(self.temp.name), self.config)
        token, second = await self.plan()
        self.assertNotEqual(retry[0]["expression"], second[0]["expression"])
        await self.planner.release(token)
        token, other = await self.plan(session="group-b")
        self.assertEqual(other[0]["expression"], retry[0]["expression"])
        await self.planner.release(token)

    async def test_concurrent_fallbacks_reserve_distinct_directions_and_release(self):
        results = await asyncio.gather(self.plan(), self.plan(), self.plan())
        self.assertEqual(len({photos[0]["expression"] for _, photos in results}), 3)
        for token, _ in results:
            await self.planner.release(token)
        self.assertFalse(self.planner._pending)

    async def test_batch_explicit_expression_remains_uniform(self):
        token, photos = await self.plan(request="拍三张眨眼的自拍", visual_request="眨眼", count=3)
        self.assertEqual({photo["expression"] for photo in photos}, {"眨眼"})
        for index, success in enumerate((True, False, True)):
            await self.planner.complete_photo(token, index, success)
        self.assertEqual(len(self.planner._history["group-a"]), 2)
        self.assertFalse(self.planner._pending)

    async def test_batch_has_distinct_fallbacks_and_zero_history_disables_cross_request_avoidance(self):
        self.config["persona_expression_history"] = 0
        token, photos = await self.plan(count=3)
        self.assertEqual(len({photo["expression"] for photo in photos}), 3)
        await self.planner.release(token)
        token, first = await self.plan()
        await self.planner.complete_photo(token, 0, True)
        token, second = await self.plan()
        self.assertEqual(first[0]["expression"], second[0]["expression"])
        await self.planner.release(token)

    async def test_repeated_llm_direction_is_not_replaced_by_a_default_smile(self):
        direction = [{"outfit": "蓝色外套", "expression": "悲伤的眼神", "gaze": "低头", "pose": "靠墙"}]
        token, first = await self.plan(photos=direction)
        await self.planner.complete_photo(token, 0, True)
        token, second = await self.plan(photos=direction)
        self.assertEqual(first, second)
        self.assertEqual((await self.planner.recent("group-a"))[0]["expression"], "悲伤的眼神")
        await self.planner.release(token)

    async def test_context_fallback_does_not_assign_a_fixed_smile(self):
        self.config["persona_expression_mode"] = "context"
        token, photos = await self.plan()
        self.assertIn("根据本次场景", photos[0]["expression"])
        self.assertNotEqual(photos[0]["expression"], self.state["mood"])
        await self.planner.release(token)

    async def test_unusual_free_text_direction_is_preserved_in_context_mode(self):
        self.config["persona_expression_mode"] = "context"
        token, photos = await self.plan(request="窗边 cobalt-blue utility overalls; lifted brows with lips pressed",
            visual_request="cobalt-blue utility overalls; lifted brows with lips pressed")
        self.assertIn("utility overalls", photos[0]["outfit"])
        self.assertIn("lips pressed", photos[0]["expression"])
        self.assertEqual(photos[0]["gaze"], "")
        await self.planner.release(token)

    async def test_planning_integration_does_not_access_chat_provider(self):
        source = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        main_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and any(
            isinstance(child, ast.AsyncFunctionDef) and child.name == "_plan_persona_photos" for child in node.body))
        function = next(node for node in main_class.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_plan_persona_photos")
        function.decorator_list = []
        harness = ast.ClassDef(name="Harness", bases=[], keywords=[], body=[function], decorator_list=[])
        namespace = {"AstrMessageEvent": object, "Optional": __import__("typing").Optional,
                     "List": list, "Dict": dict, "photo_plan_prompt": module.photo_plan_prompt}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[harness], type_ignores=[])), "harness", "exec"), namespace)
        plugin = namespace["Harness"]()
        plugin.persona_profile = types.SimpleNamespace(get_state=AsyncMock(return_value=self.state))
        plugin.persona_planner = self.planner
        plugin._get_conf_bool = lambda *args: True
        event = types.SimpleNamespace(unified_msg_origin="group-a")
        direction = [{"outfit": "红色裙子", "expression": "认真", "gaze": "向左", "pose": "坐着"}]
        token, prompts = await plugin._plan_persona_photos(event, "自拍", "Keep identity", 1, direction)
        self.assertIn("红色裙子", prompts[0])
        self.assertIn("expression: 认真", prompts[0])
        await self.planner.release(token)


class ExpressionRecognitionTest(unittest.TestCase):
    def test_chinese_and_english_expression_requests(self):
        for text in ("wink", "眨眼", "惊讶", "嘟嘴", "疑惑", "坏笑", "认真", "悲伤", "neutral expression", "wear a jacket and smile"):
            with self.subTest(text=text):
                self.assertTrue(module.has_expression_request(text))
        self.assertFalse(module.has_expression_request("来张公园的自拍"))

    def test_plan_parser_rejects_incomplete_or_wrong_types(self):
        for value in ('{"photos":[]}', '{"photos":[{"outfit":1,"expression":"smile"}]}', 'hello'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.parse_photo_plan(value, 1)
        parsed = module.parse_photo_plan('```json\n{"photos":[{"outfit":"shirt","expression":"curious"}]}\n```', 1)
        self.assertEqual(parsed[0]["outfit"], "shirt")


if __name__ == "__main__":
    unittest.main()

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "linghui_persona_dialogue_test",
    ROOT / "persona_dialogue.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class PersonaProgressPromptTest(unittest.TestCase):
    def test_persona_prompt_is_preserved_and_selfie_is_first_person(self):
        system_prompt, user_prompt = MODULE.build_persona_progress_prompts(
            "你是爱丽丝，说话简短而温柔。",
            "persona",
            count=2,
            has_user_images=True,
            is_clothing_request=True,
        )

        self.assertIn("你是爱丽丝，说话简短而温柔。", system_prompt)
        self.assertIn("严格沿用上方 AstrBot 当前人格", system_prompt)
        self.assertIn("以当前人格本人身份", system_prompt)
        self.assertIn("你自己的照片或自拍", user_prompt)
        self.assertIn("用户明确要 2 张结果", user_prompt)
        self.assertIn("用户还提供了参考图", user_prompt)
        self.assertIn("换装或穿搭要求", user_prompt)

    def test_prompt_does_not_contain_a_fixed_personality_example(self):
        system_prompt, user_prompt = MODULE.build_persona_progress_prompts(
            "保持当前人格。",
            "draw",
        )
        combined = system_prompt + user_prompt
        self.assertNotIn("嗯嗯，稍等", combined)
        self.assertNotIn("给你啦，别说我没帮你", combined)


class PersonaProgressSanitizerTest(unittest.TestCase):
    def test_short_in_character_reply_is_kept(self):
        self.assertEqual(
            MODULE.sanitize_persona_progress_reply("回复：等我一下，很快就回来。"),
            "等我一下，很快就回来。",
        )

    def test_technical_or_internal_reply_becomes_silent(self):
        for text in (
            "正在调用工具，请稍候。",
            "API 参数已经准备好。",
            "[TOOL_SUCCESS] 已发送。",
            "正在生成图片。",
        ):
            with self.subTest(text=text):
                self.assertEqual(MODULE.sanitize_persona_progress_reply(text), "")

    def test_reasoning_blocks_and_wrapping_quotes_are_removed(self):
        value = '<think>内部推理</think>“我去换件衣服，等我一下。”'
        self.assertEqual(
            MODULE.sanitize_persona_progress_reply(value),
            "我去换件衣服，等我一下。",
        )


class PersonaProgressIntegrationSourceTest(unittest.TestCase):
    def test_old_fixed_selfie_progress_line_is_removed(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("嗯嗯，稍等，我弄一下。", source)
        self.assertIn("await self._send_llm_progress(\n            event, \"persona\"", source)

    def test_schema_explains_extra_persona_request_and_silent_fallback(self):
        import json

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        hint = schema["llm_show_progress"]["hint"]
        self.assertIn("AstrBot 当前人格", hint)
        self.assertIn("多一次小型模型请求", hint)
        self.assertIn("静默继续", hint)


if __name__ == "__main__":
    unittest.main()

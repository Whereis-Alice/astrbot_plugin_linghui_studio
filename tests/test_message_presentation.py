"""Verify native QQ Markdown selection and preservation of generated media."""

import types
import unittest

from astrbot.api.message_components import At, Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_linghui_features import _load_module


class MessagePresentationTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_module("message_presentation")
        self.text = (
            "\n[爱丽丝的巧克力蛋糕(2127074778)] ✅ 生成成功 (42.59s)"
            " | 预设: 动图画 | 剩余: 额度豁免\n"
            "用户: 0 | 群组: 0 | 全局总次数：不限（所有会话累计已用 213 次）"
        )

    def test_official_success_has_heading_requester_and_separate_details(self):
        original = MessageChain([Plain(self.text)])
        result = self.module.prepare_outgoing_messages(original, platform_name="qq_official")
        self.assertEqual(len(result), 1)
        self.assertIsNot(result[0], original)
        self.assertTrue(result[0].use_markdown_)
        self.assertFalse(result[0].use_t2i_)
        text = result[0].chain[0].text
        self.assertTrue(text.startswith("### ✅ 生成成功 (42.59s)\n\n> "))
        for field in ("**预设：** 动图画", "**剩余：** 额度豁免", "**用户：** 0", "**群组：** 0"):
            self.assertIn(field, text)
        self.assertIn("**全局总次数：** 不限（所有会话累计已用 213 次）", text)
        self.assertEqual(original.chain[0].text, self.text)
        self.assertIsNone(original.use_markdown_)

    def test_official_media_is_sent_separately_from_markdown_caption(self):
        image = Image.fromBytes(b"original-gif-bytes")
        mention = At(qq="123")
        original = MessageChain([mention, Plain(" "), image, Plain(self.text)])
        media, notice = self.module.prepare_outgoing_messages(
            original, platform_name="qq_official_webhook"
        )
        self.assertEqual(media.chain, [image])
        self.assertIs(media.chain[0], image)
        self.assertFalse(media.use_markdown_)
        self.assertTrue(notice.use_markdown_)
        self.assertEqual(len(notice.chain), 2)
        self.assertIs(notice.chain[0], mention)
        self.assertIsInstance(notice.chain[1], Plain)
        self.assertTrue(notice.chain[1].text.startswith("\n### "))
        self.assertEqual(len(original.chain), 4)

    def test_non_official_platform_and_instance_named_like_official_remain_plain(self):
        original = MessageChain([Image.fromBytes(b"image"), Plain(self.text)])
        for platform in ("aiocqhttp", "qq", "webchat", "alice", "qq_official:bot"):
            self.assertEqual(
                self.module.prepare_outgoing_messages(original, platform_name=platform), [original]
            )
        event = types.SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq_official"
        )
        self.assertIs(self.module.prepare_outgoing_messages(original, event=event)[0], original)

    def test_event_platform_type_selects_native_markdown(self):
        original = MessageChain([Plain("⏳ 正在生成，请稍候。")])
        event = types.SimpleNamespace(get_platform_name=lambda: "qq_official_webhook")
        result = self.module.prepare_outgoing_messages(original, event=event)
        self.assertTrue(result[0].use_markdown_)

    def test_native_requester_mention_precedes_heading_on_its_own_line(self):
        mention = At(qq="123")
        original = MessageChain([mention, Plain(" "), Plain("✅ 图片完成")])
        result = self.module.prepare_outgoing_messages(original, platform_name="qq_official")[0]
        self.assertIs(result.chain[0], mention)
        self.assertTrue(result.chain[1].text.startswith("\n### ✅ 图片完成"))

    def test_legacy_chain_without_capability_is_unchanged(self):
        original = types.SimpleNamespace(chain=[Plain(self.text)])
        self.assertIs(
            self.module.prepare_outgoing_messages(original, platform_name="qq_official")[0], original
        )

    def test_user_markup_cannot_inject_links_images_html_or_headings(self):
        text = self.module.format_notice_markdown(
            "✅ 生成成功 | 预设: ![image](https://evil.invalid/a) <script>\n"
            "# injected\n- list\n&#35; entity\n模型: gpt-image-2"
        )
        self.assertIn(r"!\[image\](https://evil.invalid/a)", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn(r"\# injected", text)
        self.assertIn(r"\- list", text)
        self.assertIn(r"&amp;\#35; entity", text)
        self.assertIn("gpt-image-2", text)

    def test_failure_notice_has_readable_channel_lines(self):
        text = self.module.format_notice_markdown(
            "没弄好：\n• orb：内容审核未通过（HTTP 400）\n• miku：上游超时\n请调整提示词后重试。"
        )
        self.assertTrue(text.startswith("### 没弄好\n\n"))
        self.assertIn("- **orb：**", text)
        self.assertIn("- **miku：**", text)
        self.assertIn("  \n", text)

    def test_bracketed_preset_and_batch_position_do_not_become_requester(self):
        text = self.module.format_notice_markdown(
            "[[Alice](123)] ✅ [1/2] 生成成功 (1s) | 预设: [动画] | 剩余: 额度豁免"
        )
        self.assertTrue(text.startswith(r"### ✅ \[1/2\] 生成成功 (1s)"))
        self.assertIn(r"> \[\[Alice\](123)\]", text)
        self.assertIn(r"**预设：** \[动画\]", text)

    def test_empty_media_only_and_already_presented_messages_are_not_reformatted(self):
        for chain in (MessageChain(), MessageChain([Image.fromBytes(b"image")])):
            self.assertIs(
                self.module.prepare_outgoing_messages(chain, platform_name="qq_official")[0], chain
            )
        chain = MessageChain([Plain("✅ 图片完成")])
        once = self.module.prepare_outgoing_messages(chain, platform_name="qq_official")[0]
        twice = self.module.prepare_outgoing_messages(once, platform_name="qq_official")[0]
        self.assertIs(twice, once)


if __name__ == "__main__":
    unittest.main()

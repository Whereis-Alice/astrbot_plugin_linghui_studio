import importlib
import io
import pathlib
import sys
import types
import unittest

from astrbot.core.message.components import At, Plain
from PIL import Image as PILImage


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "linghui_test_package"


def load_image_manager():
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.image_manager").ImageManager


class MentionAvatarExtractionTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ImageManager = load_image_manager()

    async def test_multiple_mentions_are_downloaded_in_order_and_deduplicated(self):
        manager = self.ImageManager({})
        fetched = []

        async def get_avatar(user_id):
            fetched.append(user_id)
            return f"avatar:{user_id}".encode()

        manager.get_avatar = get_avatar
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=[
                At(qq="9000"),
                At(qq="1001"),
                At(qq="1002"),
                At(qq="1001"),
            ]),
            message_str="#bnn @9000 @1001 @1002 @1001 @1003 合影",
        )

        images = await manager.extract_images_from_event(
            event,
            ignore_id="9000",
            include_at_avatar=True,
        )

        self.assertEqual(fetched, ["1001", "1002", "1003"])
        self.assertEqual(images, [b"avatar:1001", b"avatar:1002", b"avatar:1003"])

    async def test_mentions_are_not_downloaded_when_avatar_inputs_are_disabled(self):
        manager = self.ImageManager({})
        fetched = []

        async def get_avatar(user_id):
            fetched.append(user_id)
            return b"avatar"

        manager.get_avatar = get_avatar
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=[At(qq="1001")]),
            message_str="#bnn @1001 portrait",
        )

        images = await manager.extract_images_from_event(event, include_at_avatar=False)

        self.assertEqual(images, [])
        self.assertEqual(fetched, [])

    def test_plain_prompt_excludes_at_cards_names_and_ids(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=[
                Plain(text="#bnn "),
                At(qq="1001", name="甲同学"),
                Plain(text=" @甲同学（1001） "),
                At(qq="1002", name="乙同学"),
                Plain(text=" 在海边合影，不要文字"),
            ]),
            message_str="#bnn @甲同学(1001) @乙同学(1002) 在海边合影，不要文字",
        )

        prompt = manager.extract_plain_text_without_mentions(event)

        self.assertEqual(prompt, "#bnn 在海边合影，不要文字")
        self.assertNotIn("甲同学", prompt)
        self.assertNotIn("1001", prompt)
        self.assertNotIn("乙同学", prompt)
        self.assertNotIn("1002", prompt)

    def test_mentioned_ids_are_ordered_deduplicated_and_ignore_bot(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=[
                At(qq="9000", name="机器人"),
                At(qq="1002", name="乙"),
                At(qq="1001", name="甲"),
                At(qq="1002", name="乙"),
            ]),
            message_str="#bnn @9000 @1002 @1001 @1003 合影",
        )

        self.assertEqual(
            manager.extract_mentioned_user_ids(event, ignore_id="9000"),
            ["1002", "1001", "1003"],
        )

    def test_text_only_adapter_cards_are_removed_and_used_as_avatar_ids(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(
            message_obj=types.SimpleNamespace(message=[
                Plain(text="#bnn @甲同学（1001） @乙同学(1002) 海边合影"),
            ]),
            message_str="#bnn @甲同学（1001） @乙同学(1002) 海边合影",
        )

        self.assertEqual(
            manager.extract_plain_text_without_mentions(event),
            "#bnn 海边合影",
        )
        self.assertEqual(manager.extract_mentioned_user_ids(event), ["1001", "1002"])

    def test_bot_in_prompt_is_a_reference_but_leading_bot_is_only_wake(self):
        manager = self.ImageManager({})
        for prefix in ("bnn", "#bnn", "绘图 bnn", "#画"):
            with self.subTest(prefix=prefix):
                event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[
                    At(qq="9000"), Plain(text=prefix + " "),
                    At(qq="1001"), At(qq="9000"), At(qq="9000"), Plain(text=" 合影"),
                ]), message_str=prefix + " @1001 @9000 @9000 合影")
                self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000"), ["1001", "9000"])
                event.message_obj.message = event.message_obj.message[:3]
                event.message_str = prefix + " @1001 合影"
                self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000"), ["1001"])

    def test_text_mentions_preserve_order_and_work_without_components(self):
        manager = self.ImageManager({})
        for chain in ([], [Plain(text="@9000 bnn @1002 @9000 @甲(1001) @1002 合影")]):
            event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=chain),
                message_str="@9000 bnn @1002 @9000 @甲(1001) @1002 合影")
            self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000"), ["1002", "9000", "1001"])
        event.message_obj.message = [Plain(text="bnn @1002 @1001 合影")]
        self.assertEqual(manager.extract_plain_text_without_mentions(event), "bnn 合影")

    def test_prefix_or_namespace_before_wake_mention_is_not_command_text(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[
            Plain(text="#绘图 "), At(qq="9000"), Plain(text="bnn "), At(qq="1001"),
        ]), message_str="绘图 bnn @1001 合影")
        self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000",
            command_markers=["#", "绘图"]), ["1001"])

    def test_raw_onebot_recovers_dropped_mentions_in_original_order(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(
            message=[Plain(text="bnn "), At(qq="1001"), Plain(text=" 合影")],
            raw_message={"message": [
                {"type": "at", "data": {"qq": "9000"}},
                {"type": "text", "data": {"text": "bnn "}},
                {"type": "at", "data": {"qq": "9000"}},
                {"type": "at", "data": {"qq": "1002"}},
                {"type": "at", "data": {"qq": "1001"}},
            ]}), message_str="bnn @1001 合影")
        self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000"), ["9000", "1002", "1001"])

    def test_cq_mentions_and_all_or_nonqq_ids(self):
        manager = self.ImageManager({})
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[
            Plain(text="[CQ:at,qq=9000] bnn [CQ:at,qq=1001] "),
            At(qq="all"), At(qq="alice"), At(qq="0"),
            At(qq="9000"), Plain(text=" 合影"),
        ]), message_str="bnn @1001 合影")
        self.assertEqual(manager.extract_mentioned_user_ids(event, wake_bot_id="9000"), ["1001", "9000"])
        self.assertEqual(manager.extract_plain_text_without_mentions(event), "bnn 合影")

    async def test_selected_bot_avatar_and_partial_failures_are_reported(self):
        manager = self.ImageManager({})
        async def get_avatar(uid):
            if uid == "1001":
                return None
            if uid == "1002":
                raise OSError("avatar unavailable")
            return b"bot-avatar"
        manager.get_avatar = get_avatar
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message=[Plain(text="bnn")]), message_str="bnn")
        failed = []
        images = await manager.extract_images_from_event(event, ignore_id="9000",
            avatar_user_ids=["9000", "1001", "1002", "9000"], failed_avatar_ids=failed)
        self.assertEqual(images, [b"bot-avatar"])
        self.assertEqual(failed, ["1001", "1002"])

    async def test_avatar_download_validates_image_and_falls_back(self):
        manager = self.ImageManager({})
        png = io.BytesIO()
        PILImage.new("RGB", (16, 16), "red").save(png, format="PNG")
        calls = []
        async def download(url):
            calls.append(url)
            return b"<html>temporary error</html>" if len(calls) == 1 else png.getvalue()
        manager._download_image = download
        self.assertTrue(await manager.get_avatar(9000))
        self.assertEqual(len(calls), 2)
        self.assertIn("dst_uin=9000", calls[1])
        for invalid in ("all", "0", "１２３", "9000&foo=1"):
            self.assertIsNone(await manager.get_avatar(invalid))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

import importlib
import pathlib
import sys
import types
import unittest

from astrbot.core.message.components import At, Plain


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


if __name__ == "__main__":
    unittest.main()

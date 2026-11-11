import json
import unittest
from unittest.mock import patch

import bot
import vk_bot

ADMIN = 100
OWNER = 200
STRANGER = 300


def computer(name: str, vk_users: list[int], ssh_user: str = "Owner") -> dict:
    return {
        "name": name,
        "mac": "AA:BB:CC:DD:EE:FF",
        "ip": "192.168.1.10",
        "ssh_user": ssh_user,
        "os": "Windows",
        "vk_users": vk_users,
    }


class FakeMessages:
    def __init__(self):
        self.calls = []

    def send(self, **kw):
        self.calls.append(("send", kw))
        return [{"conversation_message_id": 1}]

    def edit(self, **kw):
        self.calls.append(("edit", kw))
        return 1

    def sendMessageEventAnswer(self, **kw):
        self.calls.append(("answer", kw))
        return 1


class FakeApi:
    def __init__(self):
        self.messages = FakeMessages()


def commands(call) -> list[str]:
    keyboard = json.loads(call[1]["keyboard"])
    return [
        json.loads(button["action"]["payload"])["c"]
        for row in keyboard["buttons"]
        for button in row
    ]


class VkBotTests(unittest.TestCase):
    def setUp(self):
        computers = vk_bot.load_vk_computers(bot.load_computers(json.dumps([
            computer("One", [OWNER]),
            computer("Two", [], ssh_user=""),
        ])))
        patcher = patch.multiple(vk_bot, COMPUTERS=computers, ADMIN_IDS={ADMIN})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = FakeApi()
        self.bot = vk_bot.VkWakeBot(self.api)
        self.calls = self.api.messages.calls

    def message(self, uid, text="Начать", peer_id=None):
        self.bot.on_message({"from_id": uid, "peer_id": peer_id or uid, "text": text})

    def press(self, uid, command, peer_id=None):
        self.bot.on_event({
            "user_id": uid, "peer_id": peer_id or uid, "event_id": "e",
            "conversation_message_id": 5, "payload": {"c": command},
        })

    def last(self, kind):
        return next(call for call in reversed(self.calls) if call[0] == kind)

    def test_rejects_non_integer_vk_users(self):
        with self.assertRaisesRegex(bot.ConfigError, "vk_users"):
            vk_bot.load_vk_computers(bot.load_computers(json.dumps([computer("X", ["1"])])))

    def test_stranger_gets_no_access_and_sees_own_id(self):
        self.message(STRANGER)
        text = self.calls[-1][1]["message"]
        self.assertIn("Нет доступа", text)
        self.assertIn(str(STRANGER), text)
        self.assertNotIn("keyboard", self.calls[-1][1])

    def test_ignores_group_chats(self):
        self.message(ADMIN, peer_id=2000000001)
        self.assertEqual(self.calls, [])

    def test_owner_sees_only_own_computer(self):
        self.message(OWNER)
        self.assertEqual(
            commands(self.calls[-1]),
            ["wake:0", "status:0", "confirm_shutdown:0", "confirm_reboot:0", "back"],
        )

    def test_admin_sees_all_computers(self):
        self.message(ADMIN)
        self.assertEqual(commands(self.calls[-1]), ["select:0", "select:1"])
        self.assertIn("Режим администратора", self.calls[-1][1]["message"])

    def test_admin_command_is_admin_only(self):
        self.message(OWNER, "/admin")
        self.assertIn("только администратору", self.calls[-1][1]["message"])
        self.message(ADMIN, "/admin")
        self.assertIn("Панель администратора", self.calls[-1][1]["message"])

    def test_computer_without_ssh_has_no_power_buttons(self):
        self.press(ADMIN, "select:1")
        self.assertEqual(commands(self.last("edit")), ["wake:1", "status:1", "back"])

    def test_wake_sends_packets(self):
        with patch.object(bot, "send_wake_packets") as wake:
            self.press(OWNER, "wake:0")
        wake.assert_called_once()
        self.assertEqual(wake.call_args.args[0]["name"], "One")
        self.assertIn("отправлена на One", self.last("edit")[1]["message"])

    def test_owner_cannot_touch_foreign_computer(self):
        with patch.object(bot, "send_wake_packets") as wake:
            self.press(OWNER, "wake:1")
        wake.assert_not_called()
        self.assertIn("не найден", self.last("edit")[1]["message"])

    def test_stranger_button_press_does_nothing(self):
        with patch.object(bot, "send_wake_packets") as wake, \
                patch.object(bot, "ssh_command") as ssh:
            self.press(STRANGER, "wake:0")
            self.press(STRANGER, "shutdown:0")
        wake.assert_not_called()
        ssh.assert_not_called()
        self.assertFalse([call for call in self.calls if call[0] == "edit"])

    def test_button_press_in_group_chat_is_refused(self):
        with patch.object(bot, "send_wake_packets") as wake:
            self.press(ADMIN, "wake:0", peer_id=2000000001)
        wake.assert_not_called()

    def test_shutdown_requires_confirmation(self):
        with patch.object(bot, "ssh_command", return_value=(True, "")) as ssh:
            self.press(OWNER, "confirm_shutdown:0")
            ssh.assert_not_called()
            self.assertEqual(commands(self.last("edit")), ["shutdown:0", "select:0"])
            self.press(OWNER, "shutdown:0")
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args.args[1], "shutdown /s /t 0")
        self.assertIn("выключается", self.last("edit")[1]["message"])

    def test_status_reports_ping_result(self):
        with patch.object(bot, "ping_host", return_value=False):
            self.press(OWNER, "status:0")
        self.assertIn("оффлайн", self.last("edit")[1]["message"])

    def test_keyboards_fit_vk_limits(self):
        long_name = computer("N" * 60, [OWNER])
        loaded = vk_bot.load_vk_computers(bot.load_computers(json.dumps([long_name])))
        keyboard = json.loads(vk_bot.pc_keyboard(loaded[0]))
        self.assertLessEqual(len(keyboard["buttons"]), 6)
        for row in keyboard["buttons"]:
            for button in row:
                self.assertLessEqual(len(button["action"]["label"]), 40)


if __name__ == "__main__":
    unittest.main()

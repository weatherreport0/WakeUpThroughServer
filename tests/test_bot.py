import json
import logging
import subprocess
import unittest
from unittest.mock import patch

from telegram import User

import bot


def computer(name: str, user_id: int) -> dict:
    return {
        "name": name,
        "mac": "AA:BB:CC:DD:EE:FF",
        "ip": "192.168.1.10",
        "ssh_user": "Owner",
        "os": "Windows",
        "users": [user_id],
        "usernames": [],
    }


class ConfigTests(unittest.TestCase):
    def test_loads_multiple_computers_and_assigns_short_keys(self):
        loaded = bot.load_computers(json.dumps([computer("One", 1), computer("Two", 2)]))

        self.assertEqual([pc["_key"] for pc in loaded], ["0", "1"])
        self.assertEqual([pc["name"] for pc in loaded], ["One", "Two"])

    def test_rejects_invalid_json(self):
        with self.assertRaisesRegex(bot.ConfigError, "JSON"):
            bot.load_computers("not-json")

    def test_rejects_invalid_mac(self):
        value = computer("Broken", 1)
        value["mac"] = "not-a-mac"

        with self.assertRaisesRegex(bot.ConfigError, "mac"):
            bot.load_computers(json.dumps([value]))

    def test_rejects_invalid_broadcast(self):
        value = computer("Broken", 1)
        value["broadcast"] = "not-an-ip"

        with self.assertRaisesRegex(bot.ConfigError, "broadcast"):
            bot.load_computers(json.dumps([value]))

    def test_rejects_duplicate_names(self):
        with self.assertRaisesRegex(bot.ConfigError, "уникальны"):
            bot.load_computers(json.dumps([computer("PC", 1), computer("pc", 2)]))

    def test_rejects_string_user_id(self):
        value = computer("PC", 1)
        value["users"] = ["1"]

        with self.assertRaisesRegex(bot.ConfigError, "Telegram ID"):
            bot.load_computers(json.dumps([value]))

    def test_loads_additional_computers(self):
        loaded = bot.load_computers(
            json.dumps([computer("One", 1)]),
            json.dumps([computer("Two", 2)]),
        )

        self.assertEqual([pc["name"] for pc in loaded], ["One", "Two"])

    def test_allows_wake_only_computer_without_ssh_user(self):
        value = computer("Wake only", 1)
        value["ssh_user"] = ""

        loaded = bot.load_computers(json.dumps([value]))

        self.assertEqual(loaded[0]["ssh_user"], "")


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.old_computers = bot.COMPUTERS
        self.old_names = bot.ADMIN_USERNAMES
        self.old_ids = bot.ADMIN_USER_IDS
        bot.COMPUTERS = bot.load_computers(
            json.dumps([computer("One", 100), computer("Two", 200)])
        )
        bot.ADMIN_USERNAMES = bot.parse_admin_usernames(None)
        bot.ADMIN_USER_IDS = set()

    def tearDown(self):
        bot.COMPUTERS = self.old_computers
        bot.ADMIN_USERNAMES = self.old_names
        bot.ADMIN_USER_IDS = self.old_ids

    def test_regular_user_sees_only_assigned_computer(self):
        user = User(id=100, first_name="User", is_bot=False, username="someone")

        self.assertEqual([pc["name"] for pc in bot.get_user_pcs(user)], ["One"])

    def test_alacrity_username_is_admin_and_sees_all_computers(self):
        user = User(id=999, first_name="Admin", is_bot=False, username="Alacrityy0")

        self.assertTrue(bot.is_admin(user))
        self.assertEqual([pc["name"] for pc in bot.get_user_pcs(user)], ["One", "Two"])

    def test_admin_id_is_supported_for_stable_access(self):
        bot.ADMIN_USER_IDS = {777}
        user = User(id=777, first_name="Admin", is_bot=False)

        self.assertTrue(bot.is_admin(user))

    def test_regular_user_can_be_assigned_by_username(self):
        bot.COMPUTERS[1]["usernames"] = ["pudj2003"]
        user = User(id=999, first_name="User", is_bot=False, username="Pudj2003")

        self.assertEqual([pc["name"] for pc in bot.get_user_pcs(user)], ["Two"])


class CommandTests(unittest.TestCase):
    def test_log_formatter_redacts_secrets(self):
        formatter = bot.SecretRedactingFormatter("%(message)s", ["secret-token"])
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request to https://example/secret-token failed",
            (),
            None,
        )

        self.assertEqual(
            formatter.format(record),
            "request to https://example/<redacted> failed",
        )

    def test_http_client_logs_cannot_expose_bot_token_at_info_level(self):
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    @patch("bot.platform.system", return_value="Windows")
    @patch("bot.subprocess.run")
    def test_windows_ping_uses_millisecond_timeout(self, run, system):
        del system
        run.return_value = subprocess.CompletedProcess([], 0)

        self.assertTrue(bot.ping_host("192.168.1.2"))
        self.assertEqual(
            run.call_args.args[0],
            ["ping", "-n", "1", "-w", "2000", "192.168.1.2"],
        )

    @patch("bot.send_magic_packet")
    def test_wake_sends_repeated_packets_to_directed_and_global_broadcast(self, send):
        pc = computer("PC", 1)
        pc["broadcast"] = "192.168.1.255"

        bot.send_wake_packets(pc)

        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].kwargs["ip_address"], "192.168.1.255")
        self.assertEqual(send.call_args_list[1].kwargs["ip_address"], "255.255.255.255")
        self.assertEqual(send.call_args_list[0].args, (pc["mac"],) * 3)

    @patch("bot.subprocess.run")
    def test_linux_shutdown_never_prompts_for_sudo_password(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        pc = computer("Linux", 1)
        pc["os"] = "Linux"

        ok, _ = bot.ssh_command(pc, "shutdown /s /t 0")

        self.assertTrue(ok)
        args = run.call_args.args[0]
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[-1], "sudo -n poweroff")

    def test_application_has_separate_http_clients_and_error_handler(self):
        app = bot.build_application("123456:test-token")

        self.assertIsNot(app.bot._request[0], app.bot._request[1])
        self.assertEqual(sum(len(group) for group in app.handlers.values()), 3)
        self.assertEqual(len(app.error_handlers), 1)

    def test_wake_only_computer_has_no_shutdown_buttons(self):
        pc = bot.load_computers(
            json.dumps([{**computer("Wake only", 1), "ssh_user": ""}])
        )[0]

        callback_data = [
            button.callback_data
            for row in bot.pc_keyboard(pc).inline_keyboard
            for button in row
        ]

        self.assertEqual(callback_data, ["wake:0", "status:0", "back"])


if __name__ == "__main__":
    unittest.main()

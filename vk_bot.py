"""VK front-end of the Wake-on-LAN bot: a community bot (Bots Long Poll API)
with the same menu as the Telegram bot.

Config (.env): VK_GROUP_TOKEN, VK_ADMIN_USER_IDS (comma-separated VK user ids),
and an optional "vk_users" list of VK user ids on each entry of COMPUTERS.
The community needs messages, bot capabilities and Long Poll API enabled with
the events "message_new" and "message_event"."""

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import vk_api
from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll

import bot as core
from bot import ConfigError

logger = logging.getLogger("wakebot.vk")

LABEL_MAX = 40  # VK button label limit
COMPUTERS: list[dict] = []
ADMIN_IDS: set[int] = set()


def load_vk_computers(computers: list[dict]) -> list[dict]:
    """Validate the per-computer "vk_users" lists."""
    for pc in computers:
        users = pc.get("vk_users", [])
        if not isinstance(users, list) or any(
            isinstance(user_id, bool) or not isinstance(user_id, int) for user_id in users
        ):
            raise ConfigError(
                f"{pc['name']}: vk_users должен быть массивом числовых VK ID"
            )
        pc["vk_users"] = list(dict.fromkeys(users))
    return computers


def configure_from_environment() -> str:
    global COMPUTERS, ADMIN_IDS

    token = (os.getenv("VK_GROUP_TOKEN") or "").strip()
    if not token:
        raise ConfigError("VK_GROUP_TOKEN не задан")
    COMPUTERS = load_vk_computers(
        core.load_computers(os.getenv("COMPUTERS"), os.getenv("EXTRA_COMPUTERS"))
    )
    ADMIN_IDS = core.parse_admin_user_ids(os.getenv("VK_ADMIN_USER_IDS"))
    return token


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def get_user_pcs(uid: int) -> list[dict]:
    if is_admin(uid):
        return list(COMPUTERS)
    return [pc for pc in COMPUTERS if uid in pc["vk_users"]]


def find_pc(key: str, uid: int) -> dict | None:
    return next((pc for pc in get_user_pcs(uid) if pc["_key"] == key), None)


# --- Keyboards ---


def _button(label: str, command: str, color: str = "secondary") -> dict:
    if len(label) > LABEL_MAX:
        label = label[: LABEL_MAX - 1] + "…"
    return {
        "action": {
            "type": "callback",
            "label": label,
            "payload": json.dumps({"c": command}),
        },
        "color": color,
    }


def _keyboard(rows: list[list[dict]]) -> str:
    return json.dumps({"inline": True, "buttons": rows}, ensure_ascii=False)


def pc_keyboard(pc: dict) -> str:
    name = pc["name"]
    key = pc["_key"]
    rows = [
        [_button(f"Включить {name}", f"wake:{key}", "positive")],
        [_button(f"Статус {name}", f"status:{key}")],
    ]
    if pc.get("ssh_user"):
        rows.append([
            _button("Выключить", f"confirm_shutdown:{key}"),
            _button("Перезагрузить", f"confirm_reboot:{key}"),
        ])
    rows.append([_button("< Назад", "back")])
    return _keyboard(rows)


def pc_list_keyboard(uid: int) -> str:
    # VK inline keyboards hold at most 10 buttons in 6 rows: two per row.
    buttons = [_button(pc["name"], f"select:{pc['_key']}") for pc in get_user_pcs(uid)][:10]
    return _keyboard([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


class VkWakeBot:
    def __init__(self, api):
        self.api = api

    # --- VK API wrappers ---

    def send(self, peer_id: int, text: str, keyboard: str | None = None) -> None:
        params = {"peer_ids": peer_id, "message": text, "random_id": 0}
        if keyboard:
            params["keyboard"] = keyboard
        self.api.messages.send(**params)

    def edit(self, event: dict, text: str, keyboard: str | None = None) -> None:
        self.api.messages.edit(
            peer_id=event["peer_id"],
            conversation_message_id=event["conversation_message_id"],
            message=text,
            keyboard=keyboard or _keyboard([]),
        )

    def answer_event(self, event: dict, text: str | None = None) -> None:
        params = {
            "event_id": event["event_id"],
            "user_id": event["user_id"],
            "peer_id": event["peer_id"],
        }
        if text:
            params["event_data"] = json.dumps(
                {"type": "show_snackbar", "text": text}, ensure_ascii=False
            )
        try:
            self.api.messages.sendMessageEventAnswer(**params)
        except Exception:
            logger.debug("sendMessageEventAnswer failed", exc_info=True)

    # --- Incoming messages ---

    def on_message(self, msg: dict) -> None:
        uid = msg["from_id"]
        peer_id = msg["peer_id"]
        # Private dialogs with real users only: never show the menu in group chats.
        if uid <= 0 or peer_id != uid:
            return

        text = (msg.get("text") or "").strip().casefold().lstrip("/")
        pcs = get_user_pcs(uid)

        if text in ("admin", "админ"):
            if not is_admin(uid):
                self.send(peer_id, "Команда доступна только администратору.")
                return
            names = "\n".join(f"• {pc['name']} ({pc['os']})" for pc in COMPUTERS)
            self.send(
                peer_id,
                f"Панель администратора\nКомпьютеров: {len(COMPUTERS)}\n\n{names}",
                pc_list_keyboard(uid),
            )
            return

        if not pcs:
            self.send(peer_id, f"Нет доступа.\nТвой ID: {uid}")
            return

        suffix = "\nРежим администратора" if is_admin(uid) else ""
        header = f"Wake-on-LAN бот\nТвой ID: {uid}{suffix}"
        if len(pcs) == 1:
            self.send(peer_id, header, pc_keyboard(pcs[0]))
        else:
            self.send(peer_id, f"{header}\n\nВыбери компьютер:", pc_list_keyboard(uid))

    # --- Button presses ---

    def on_event(self, event: dict) -> None:
        uid = event["user_id"]
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        action, separator, key = str(payload.get("c", "")).partition(":")

        pcs = get_user_pcs(uid)
        if not pcs or event["peer_id"] != uid:
            self.answer_event(event, "Нет доступа.")
            return

        if action == "back" and not separator:
            self.answer_event(event)
            if len(pcs) == 1:
                self.edit(event, "Wake-on-LAN бот", pc_keyboard(pcs[0]))
            else:
                self.edit(event, "Выбери компьютер:", pc_list_keyboard(uid))
            return

        pc = find_pc(key, uid)
        if pc is None:
            self.answer_event(event)
            self.edit(
                event,
                "Компьютер не найден или доступ к нему отозван.",
                pc_list_keyboard(uid),
            )
            return

        if action == "select":
            self.answer_event(event)
            self.edit(event, pc["name"], pc_keyboard(pc))

        elif action == "wake":
            self.answer_event(event)
            logger.info(
                "Wake-on-LAN: vk_user_id=%s computer=%s broadcast=%s",
                uid, pc["name"], pc["broadcast"],
            )
            try:
                core.send_wake_packets(pc)
            except Exception:
                logger.exception("Ошибка Wake-on-LAN для %s", pc["name"])
                self.edit(event, f"Не удалось отправить команду на {pc['name']}.", pc_keyboard(pc))
            else:
                self.edit(event, f"Команда включения отправлена на {pc['name']}.", pc_keyboard(pc))

        elif action == "status":
            self.answer_event(event)
            state = "онлайн" if core.ping_host(pc["ip"]) else "оффлайн"
            self.edit(event, f"{pc['name']} ({pc['ip']}): {state}", pc_keyboard(pc))

        elif action in {"confirm_shutdown", "confirm_reboot"}:
            self.answer_event(event)
            operation = "shutdown" if action == "confirm_shutdown" else "reboot"
            button_text = "Да, выключить" if operation == "shutdown" else "Да, перезагрузить"
            question = "Выключить" if operation == "shutdown" else "Перезагрузить"
            keyboard = _keyboard([[
                _button(button_text, f"{operation}:{key}", "negative"),
                _button("Отмена", f"select:{key}"),
            ]])
            self.edit(event, f"{question} {pc['name']}?", keyboard)

        elif action in {"shutdown", "reboot"}:
            self.answer_event(event)
            logger.info("%s: vk_user_id=%s computer=%s", action, uid, pc["name"])
            command = "shutdown /s /t 0" if action == "shutdown" else "shutdown /r /t 0"
            ok, output = core.ssh_command(pc, command)
            if ok:
                message = (
                    f"{pc['name']} выключается..."
                    if action == "shutdown"
                    else f"{pc['name']} перезагружается..."
                )
            else:
                logger.error("SSH-команда для %s завершилась ошибкой: %s", pc["name"], output)
                message = "Команда не выполнена. Подробности записаны в журнал бота."
            self.edit(event, message, pc_keyboard(pc))

        else:
            self.answer_event(event, "Неизвестная команда. Напиши боту любое сообщение.")


def _group_id(api) -> int:
    configured = (os.getenv("VK_GROUP_ID") or "").strip()
    if configured:
        return int(configured)
    response = api.groups.getById()
    groups = response["groups"] if isinstance(response, dict) else response
    return int(groups[0]["id"])


def main() -> int:
    try:
        token = configure_from_environment()
    except ConfigError as exc:
        logger.critical("Ошибка конфигурации: %s", exc)
        return 2

    for log_handler in logging.getLogger().handlers:
        log_handler.setFormatter(core.SecretRedactingFormatter(core.LOG_FORMAT, [token]))

    session = vk_api.VkApi(token=token, api_version="5.199")
    api = session.get_api()
    wake_bot = VkWakeBot(api)
    pool = ThreadPoolExecutor(max_workers=4)

    def run(handler, obj) -> None:
        try:
            handler(obj)
        except Exception:
            logger.exception("Ошибка при обработке события VK")

    group_id = None
    while True:
        try:
            if group_id is None:
                group_id = _group_id(api)
                logger.info(
                    "VK-бот запущен: группа=%s, компьютеров=%d, администраторов=%d",
                    group_id, len(COMPUTERS), len(ADMIN_IDS),
                )
            longpoll = VkBotLongPoll(session, group_id, wait=25)
            for event in longpoll.listen():
                if event.type == VkBotEventType.MESSAGE_NEW:
                    pool.submit(run, wake_bot.on_message, event.obj["message"])
                elif event.type == VkBotEventType.MESSAGE_EVENT:
                    pool.submit(run, wake_bot.on_event, dict(event.obj))
        except Exception as exc:
            logger.error("Ошибка VK Long Poll: %s", exc)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())

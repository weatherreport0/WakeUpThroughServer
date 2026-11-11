import asyncio
import ipaddress
import json
import logging
import os
import platform
import re
import subprocess
import sys

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from wakeonlan import send_magic_packet

load_dotenv()
load_dotenv(".env.extra")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SecretRedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self.secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self.secrets:
            rendered = rendered.replace(secret, "<redacted>")
        return rendered


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format=LOG_FORMAT,
)
for log_handler in logging.getLogger().handlers:
    log_handler.setFormatter(
        SecretRedactingFormatter(LOG_FORMAT, [(os.getenv("BOT_TOKEN") or "").strip()])
    )
logger = logging.getLogger("wakebot")
# httpx includes the Telegram bot token in request URLs at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
DEFAULT_ADMIN_USERNAMES = {"alacrityy0"}
COMPUTERS: list[dict] = []
ADMIN_USERNAMES: set[str] = set(DEFAULT_ADMIN_USERNAMES)
ADMIN_USER_IDS: set[int] = set()


class ConfigError(ValueError):
    """Raised when the bot configuration cannot be used safely."""


def normalize_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@").casefold()


def parse_admin_usernames(raw: str | None) -> set[str]:
    configured = {
        normalize_username(value)
        for value in (raw or "").split(",")
        if normalize_username(value)
    }
    return DEFAULT_ADMIN_USERNAMES | configured


def parse_admin_user_ids(raw: str | None) -> set[int]:
    result: set[int] = set()
    for value in (raw or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            result.add(int(value))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_USER_IDS содержит нечисловой ID: {value!r}") from exc
    return result


def load_computers(
    raw: str | None, additional_raw: str | None = None
) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"COMPUTERS содержит некорректный JSON: {exc.msg}") from exc

    if not isinstance(data, list):
        raise ConfigError("COMPUTERS должен быть JSON-массивом")

    if additional_raw:
        try:
            additional = json.loads(additional_raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"EXTRA_COMPUTERS содержит некорректный JSON: {exc.msg}"
            ) from exc
        if not isinstance(additional, list):
            raise ConfigError("EXTRA_COMPUTERS должен быть JSON-массивом")
        data.extend(additional)

    if not data:
        raise ConfigError("Должен быть настроен хотя бы один компьютер")

    computers: list[dict] = []
    names: set[str] = set()
    required = ("name", "mac", "ip", "os")

    for index, value in enumerate(data):
        label = f"COMPUTERS[{index}]"
        if not isinstance(value, dict):
            raise ConfigError(f"{label} должен быть JSON-объектом")

        pc = dict(value)
        for field in required:
            if not isinstance(pc.get(field), str) or not pc[field].strip():
                raise ConfigError(f"{label}.{field} должен быть непустой строкой")
            pc[field] = pc[field].strip()

        ssh_user = pc.get("ssh_user", "")
        if not isinstance(ssh_user, str):
            raise ConfigError(f"{label}.ssh_user должен быть строкой")
        pc["ssh_user"] = ssh_user.strip()

        broadcast = pc.get("broadcast", "255.255.255.255")
        try:
            broadcast_ip = ipaddress.ip_address(broadcast)
        except ValueError as exc:
            raise ConfigError(f"{label}.broadcast имеет неверный IP-адрес") from exc
        if broadcast_ip.version != 4:
            raise ConfigError(f"{label}.broadcast должен быть IPv4-адресом")
        pc["broadcast"] = str(broadcast_ip)

        if not MAC_RE.fullmatch(pc["mac"]):
            raise ConfigError(f"{label}.mac имеет неверный формат")

        normalized_name = pc["name"].casefold()
        if normalized_name in names:
            raise ConfigError(f"Имена компьютеров должны быть уникальны: {pc['name']!r}")
        names.add(normalized_name)

        users = pc.get("users", [])
        if not isinstance(users, list) or any(
            isinstance(user_id, bool) or not isinstance(user_id, int) for user_id in users
        ):
            raise ConfigError(f"{label}.users должен быть массивом числовых Telegram ID")

        pc["users"] = list(dict.fromkeys(users))
        usernames = pc.get("usernames", [])
        if not isinstance(usernames, list) or any(
            not isinstance(username, str) or not normalize_username(username)
            for username in usernames
        ):
            raise ConfigError(
                f"{label}.usernames должен быть массивом Telegram-ников"
            )
        pc["usernames"] = list(
            dict.fromkeys(normalize_username(username) for username in usernames)
        )
        pc["_key"] = str(index)
        computers.append(pc)

    return computers


def configure_from_environment() -> str:
    global COMPUTERS, ADMIN_USERNAMES, ADMIN_USER_IDS

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN не задан")

    COMPUTERS = load_computers(
        os.getenv("COMPUTERS"), os.getenv("EXTRA_COMPUTERS")
    )
    ADMIN_USERNAMES = parse_admin_usernames(os.getenv("ADMIN_USERNAMES"))
    ADMIN_USER_IDS = parse_admin_user_ids(os.getenv("ADMIN_USER_IDS"))
    return token


def is_admin(user: User | None) -> bool:
    if user is None:
        return False
    return user.id in ADMIN_USER_IDS or normalize_username(user.username) in ADMIN_USERNAMES


def get_user_pcs(user: User | None) -> list[dict]:
    if user is None:
        return []
    if is_admin(user):
        return list(COMPUTERS)
    username = normalize_username(user.username)
    return [
        pc
        for pc in COMPUTERS
        if user.id in pc["users"] or username in pc["usernames"]
    ]


def find_pc(key: str, user: User | None) -> dict | None:
    return next((pc for pc in get_user_pcs(user) if pc["_key"] == key), None)


def ping_host(ip: str) -> bool:
    if platform.system().lower() == "windows":
        args = ["ping", "-n", "1", "-w", "2000", ip]
    else:
        args = ["ping", "-c", "1", "-W", "2", ip]

    try:
        result = subprocess.run(args, capture_output=True, timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        logger.exception("Не удалось выполнить ping для %s", ip)
        return False


def send_wake_packets(pc: dict) -> None:
    destinations = list(
        dict.fromkeys([pc.get("broadcast", "255.255.255.255"), "255.255.255.255"])
    )
    for destination in destinations:
        send_magic_packet(
            pc["mac"],
            pc["mac"],
            pc["mac"],
            ip_address=destination,
            port=9,
        )


def ssh_command(pc: dict, command: str) -> tuple[bool, str]:
    if not pc.get("ssh_user"):
        return False, "SSH user is not configured"

    remote_command = command
    if pc["os"].casefold() != "windows":
        if command == "shutdown /s /t 0":
            remote_command = "sudo -n poweroff"
        elif command == "shutdown /r /t 0":
            remote_command = "sudo -n reboot"

    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{pc['ssh_user']}@{pc['ip']}",
        remote_command,
    ]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def pc_keyboard(pc: dict) -> InlineKeyboardMarkup:
    name = pc["name"]
    key = pc["_key"]
    rows = [
        [InlineKeyboardButton(f"Включить {name}", callback_data=f"wake:{key}")],
        [InlineKeyboardButton(f"Статус {name}", callback_data=f"status:{key}")],
    ]
    if pc.get("ssh_user"):
        rows.append(
            [
                InlineKeyboardButton(
                    "Выключить", callback_data=f"confirm_shutdown:{key}"
                ),
                InlineKeyboardButton(
                    "Перезагрузить", callback_data=f"confirm_reboot:{key}"
                ),
            ]
        )
    rows.append([InlineKeyboardButton("< Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def pc_list_keyboard(user: User | None) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(pc["name"], callback_data=f"select:{pc['_key']}")]
        for pc in get_user_pcs(user)
    ]
    return InlineKeyboardMarkup(buttons)


async def show_pc_list(update: Update, text: str) -> None:
    user = update.effective_user
    pcs = get_user_pcs(user)
    if not pcs:
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Нет доступа.\nТвой ID: {user.id if user else 'неизвестен'}"
            )
        return

    if len(pcs) == 1:
        await update.effective_message.reply_text(text, reply_markup=pc_keyboard(pcs[0]))
    else:
        await update.effective_message.reply_text(
            f"{text}\n\nВыбери компьютер:", reply_markup=pc_list_keyboard(user)
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    user = update.effective_user
    suffix = "\nРежим администратора" if is_admin(user) else ""
    await show_pc_list(
        update,
        f"Wake-on-LAN бот\nТвой ID: {user.id if user else 'неизвестен'}{suffix}",
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not is_admin(update.effective_user):
        await update.effective_message.reply_text("Команда доступна только администратору.")
        return

    names = "\n".join(f"• {pc['name']} ({pc['os']})" for pc in COMPUTERS)
    await update.effective_message.reply_text(
        f"Панель администратора\nКомпьютеров: {len(COMPUTERS)}\n\n{names}",
        reply_markup=pc_list_keyboard(update.effective_user),
    )


async def edit_query_message(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    if query is None:
        return

    await query.answer()
    user = query.from_user
    pcs = get_user_pcs(user)
    if not pcs:
        await edit_query_message(query, "Нет доступа.")
        return

    action, separator, key = (query.data or "").partition(":")

    if action == "back" and not separator:
        if len(pcs) == 1:
            await edit_query_message(
                query, "Wake-on-LAN бот", reply_markup=pc_keyboard(pcs[0])
            )
        else:
            await edit_query_message(
                query, "Выбери компьютер:", reply_markup=pc_list_keyboard(user)
            )
        return

    pc = find_pc(key, user)
    if pc is None:
        await edit_query_message(
            query,
            "Компьютер не найден или доступ к нему отозван.",
            reply_markup=pc_list_keyboard(user),
        )
        return

    if action == "select":
        await edit_query_message(query, pc["name"], reply_markup=pc_keyboard(pc))

    elif action == "wake":
        logger.info(
            "Wake-on-LAN: user_id=%s username=%s computer=%s broadcast=%s",
            user.id,
            normalize_username(user.username) or "-",
            pc["name"],
            pc["broadcast"],
        )
        try:
            await asyncio.to_thread(send_wake_packets, pc)
        except Exception:
            logger.exception("Ошибка Wake-on-LAN для %s", pc["name"])
            await edit_query_message(
                query,
                f"Не удалось отправить команду на {pc['name']}.",
                reply_markup=pc_keyboard(pc),
            )
        else:
            await edit_query_message(
                query,
                f"Команда включения отправлена на {pc['name']}.",
                reply_markup=pc_keyboard(pc),
            )

    elif action == "status":
        online = await asyncio.to_thread(ping_host, pc["ip"])
        state = "онлайн" if online else "оффлайн"
        await edit_query_message(
            query,
            f"{pc['name']} ({pc['ip']}): {state}",
            reply_markup=pc_keyboard(pc),
        )

    elif action in {"confirm_shutdown", "confirm_reboot"}:
        operation = "shutdown" if action == "confirm_shutdown" else "reboot"
        button_text = "Да, выключить" if operation == "shutdown" else "Да, перезагрузить"
        question = "Выключить" if operation == "shutdown" else "Перезагрузить"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        button_text, callback_data=f"{operation}:{key}"
                    ),
                    InlineKeyboardButton("Отмена", callback_data=f"select:{key}"),
                ]
            ]
        )
        await edit_query_message(query, f"{question} {pc['name']}?", reply_markup=keyboard)

    elif action in {"shutdown", "reboot"}:
        command = "shutdown /s /t 0" if action == "shutdown" else "shutdown /r /t 0"
        ok, output = await asyncio.to_thread(ssh_command, pc, command)
        if ok:
            message = (
                f"{pc['name']} выключается..."
                if action == "shutdown"
                else f"{pc['name']} перезагружается..."
            )
        else:
            logger.error("SSH-команда для %s завершилась ошибкой: %s", pc["name"], output)
            message = "Команда не выполнена. Подробности записаны в журнал бота."
        await edit_query_message(query, message, reply_markup=pc_keyboard(pc))

    else:
        await query.answer("Неизвестная команда. Открой меню заново через /start.", show_alert=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(
        "Необработанная ошибка при обработке Telegram update",
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )
    if not isinstance(update, Update) or update.effective_message is None:
        return
    try:
        await update.effective_message.reply_text(
            "Произошла ошибка. Бот продолжает работать; попробуй ещё раз или введи /start."
        )
    except TelegramError:
        logger.exception("Не удалось отправить пользователю сообщение об ошибке")


def make_request(*, read_timeout: float) -> HTTPXRequest:
    # Telegram may be unreachable directly; PROXY_URL (e.g. socks5://127.0.0.1:1080)
    # routes the bot's Telegram traffic through a proxy.
    proxy_url = (os.getenv("PROXY_URL") or "").strip()
    if proxy_url:
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url, retries=3)
    else:
        transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=3)
    return HTTPXRequest(
        connect_timeout=10,
        read_timeout=read_timeout,
        write_timeout=10,
        pool_timeout=5,
        http_version="1.1",
        httpx_kwargs={"transport": transport},
    )


def build_application(token: str) -> Application:
    app = (
        Application.builder()
        .token(token)
        .request(make_request(read_timeout=20))
        .get_updates_request(make_request(read_timeout=40))
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
    return app


def main() -> int:
    try:
        token = configure_from_environment()
    except ConfigError as exc:
        logger.critical("Ошибка конфигурации: %s", exc)
        return 2

    app = build_application(token)
    logger.info(
        "Бот запускается: компьютеров=%d, администраторов_по_нику=%s, администраторов_по_id=%d",
        len(COMPUTERS),
        ",".join(sorted(ADMIN_USERNAMES)),
        len(ADMIN_USER_IDS),
    )
    app.run_polling(
        poll_interval=0.5,
        timeout=20,
        bootstrap_retries=-1,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

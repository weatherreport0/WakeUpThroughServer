import os
import subprocess
import platform
from dotenv import load_dotenv
from wakeonlan import send_magic_packet
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USERS = set(map(int, os.getenv("ALLOWED_USERS", "").split(",")))
PC_MAC = os.getenv("PC_MAC", "")
PC_IP = os.getenv("PC_IP", "")
PC_NAME = os.getenv("PC_NAME", "PC")
PC_SSH_USER = os.getenv("PC_SSH_USER", "")


def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USERS


def ping_host(ip: str) -> bool:
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        result = subprocess.run(
            ["ping", param, "1", "-W", "2", ip],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def ssh_command(cmd: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
             f"{PC_SSH_USER}@{PC_IP}", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        return False, str(e)


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Включить {PC_NAME}", callback_data="wake")],
        [InlineKeyboardButton(f"Статус {PC_NAME}", callback_data="status")],
        [
            InlineKeyboardButton("Выключить", callback_data="confirm_shutdown"),
            InlineKeyboardButton("Перезагрузить", callback_data="confirm_reboot"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    await update.message.reply_text(
        f"Wake-on-LAN бот\nТвой ID: {update.effective_user.id}",
        reply_markup=main_keyboard(),
    )


async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    send_magic_packet(PC_MAC)
    await update.message.reply_text(f"Magic packet отправлен на {PC_NAME} ({PC_MAC})")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Нет доступа.")
        return
    online = ping_host(PC_IP)
    state = "онлайн" if online else "оффлайн"
    await update.message.reply_text(f"{PC_NAME} ({PC_IP}): {state}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_authorized(query.from_user.id):
        await query.edit_message_text("Нет доступа.")
        return

    if query.data == "wake":
        send_magic_packet(PC_MAC)
        await query.edit_message_text(
            f"Magic packet отправлен на {PC_NAME} ({PC_MAC})",
            reply_markup=main_keyboard(),
        )

    elif query.data == "status":
        online = ping_host(PC_IP)
        state = "онлайн" if online else "оффлайн"
        await query.edit_message_text(
            f"{PC_NAME} ({PC_IP}): {state}",
            reply_markup=main_keyboard(),
        )

    elif query.data == "confirm_shutdown":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Да, выключить", callback_data="shutdown"),
                InlineKeyboardButton("Отмена", callback_data="cancel"),
            ],
        ])
        await query.edit_message_text(
            f"Выключить {PC_NAME}?", reply_markup=keyboard,
        )

    elif query.data == "confirm_reboot":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Да, перезагрузить", callback_data="reboot"),
                InlineKeyboardButton("Отмена", callback_data="cancel"),
            ],
        ])
        await query.edit_message_text(
            f"Перезагрузить {PC_NAME}?", reply_markup=keyboard,
        )

    elif query.data == "shutdown":
        ok, out = ssh_command("shutdown /s /t 0")
        msg = f"{PC_NAME} выключается..." if ok else f"Ошибка: {out}"
        await query.edit_message_text(msg, reply_markup=main_keyboard())

    elif query.data == "reboot":
        ok, out = ssh_command("shutdown /r /t 0")
        msg = f"{PC_NAME} перезагружается..." if ok else f"Ошибка: {out}"
        await query.edit_message_text(msg, reply_markup=main_keyboard())

    elif query.data == "cancel":
        await query.edit_message_text(
            "Отменено.", reply_markup=main_keyboard(),
        )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wake", wake_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

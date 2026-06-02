import logging
import os
import asyncio
import requests
import psutil
from pathlib import Path
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== تنظیمات اصلی ==================

TOKEN = os.getenv("API_KEY")   # ← از Environment Variable
ADMIN_ID = 6300997264

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================== کمک‌تابع‌ها ==================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def format_size(num_bytes: int) -> str:
    if num_bytes is None:
        return "نامشخص"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"


def list_downloaded_files():
    files = []
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file():
            files.append(p)
    return files


# ================== منو ==================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📥 دانلودرها", callback_data="menu_downloaders")],
        [InlineKeyboardButton("🖥 سیستم", callback_data="menu_system")],
        [InlineKeyboardButton("📂 فایل‌ها", callback_data="menu_files")],
    ]
    return InlineKeyboardMarkup(buttons)


def downloaders_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 دانلودر لینک مستقیم", callback_data="dl_direct")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])


def system_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 وضعیت سرور", callback_data="sys_status")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])


def files_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📃 لیست فایل‌ها", callback_data="files_list")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])


# ================== هندلرها ==================

async def start(update: Update, context: ContextTypes.Context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        return

    await update.message.reply_text(
        "سلام امیر 👋\nربات روشنه.",
        reply_markup=main_menu_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.Context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("این ربات خصوصی است.")
        return

    data = query.data

    if data == "back_main":
        await query.edit_message_text("منوی اصلی:", reply_markup=main_menu_keyboard())
        return

    if data == "menu_downloaders":
        await query.edit_message_text("دانلودرها:", reply_markup=downloaders_menu_keyboard())
        return

    if data == "menu_system":
        await query.edit_message_text("سیستم:", reply_markup=system_menu_keyboard())
        return

    if data == "menu_files":
        await query.edit_message_text("فایل‌ها:", reply_markup=files_menu_keyboard())
        return

    if data == "dl_direct":
        context.user_data["mode"] = "direct_download"
        await query.edit_message_text("لینک مستقیم را بفرست.")
        return

    if data == "sys_status":
        ram = psutil.virtual_memory()
        cpu = psutil.cpu_percent(interval=0.5)
        await query.edit_message_text(
            f"CPU: {cpu}%\nRAM: {format_size(ram.used)} / {format_size(ram.total)}"
        )
        return

    if data == "files_list":
        files = list_downloaded_files()
        if not files:
            await query.edit_message_text("هیچ فایلی نیست.")
            return
        text = "\n".join([f.name for f in files])
        await query.edit_message_text(text)
        return


async def text_handler(update: Update, context: ContextTypes.Context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        return

    mode = context.user_data.get("mode")

    if mode == "direct_download":
        await handle_direct_download(update, context, update.message.text)
        context.user_data["mode"] = None
        return

    await update.message.reply_text("از منو استفاده کن.", reply_markup=main_menu_keyboard())


async def handle_direct_download(update: Update, context: ContextTypes.Context, url: str):
    msg = await update.message.reply_text("در حال بررسی لینک...")

    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        if not head.ok:
            await msg.edit_text(f"خطا: {head.status_code}")
            return

        filename = url.split("/")[-1]
        file_path = DOWNLOAD_DIR / filename

        r = requests.get(url, stream=True)
        with open(file_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                f.write(chunk)

        await update.message.reply_document(open(file_path, "rb"))
        await msg.edit_text("تمام شد.")

    except Exception as e:
        await msg.edit_text(f"خطا: {e}")


async def unknown_command(update: Update, context: ContextTypes.Context):
    await update.message.reply_text("دستور نامعتبر.")


# ================== اجرای ربات (Webhook برای Render) ==================

PORT = int(os.environ.get("PORT", 8443))

async def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    await app.initialize()
    await app.start()

    webhook_url = f"https://telegram-bot-05x3.onrender.com/{TOKEN}"

    await app.bot.set_webhook(url=webhook_url)

    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url
    )


if __name__ == "__main__":
    asyncio.run(main())
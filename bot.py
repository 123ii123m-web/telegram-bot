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

TOKEN = os.getenv("8603497838:AAGm1LIeZJ9B-hBh3ry7YOy8uAWjZEmC1W8")   # ← از Environment Variable
ADMIN_ID = 6300997264          # ← آیدی عددی تو

VERSION = "v1.0.0"
LAST_UPDATE = "2026-06-02"
CHANGELOG = "نسخه کامل: منو، دانلودر لینک مستقیم، وضعیت سرور، حافظه، آپدیت، خصوصی بودن، مدیریت فایل‌ها."

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


def classify_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in [".mp4", ".mkv", ".avi", ".mov"]:
        return "video"
    if ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
        return "image"
    if ext in [".zip", ".rar", ".7z", ".tar", ".gz"]:
        return "zip"
    return "other"


def get_disk_info() -> str:
    disk = psutil.disk_usage(str(BASE_DIR))
    total = format_size(disk.total)
    used = format_size(disk.used)
    free = format_size(disk.free)
    percent = disk.percent
    return f"کل: {total}\nمصرف‌شده: {used}\nآزاد: {free}\nدرصد مصرف: {percent}%"


def get_ram_cpu_info() -> str:
    ram = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.5)
    return (
        f"CPU: {cpu}%\n"
        f"RAM: {format_size(ram.used)} / {format_size(ram.total)} ({ram.percent}%)"
    )


def list_downloaded_files():
    files = []
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file():
            files.append(p)
    return files


# ================== منوها ==================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📥 دانلودرها", callback_data="menu_downloaders")],
        [InlineKeyboardButton("🖥 سیستم", callback_data="menu_system")],
        [InlineKeyboardButton("📂 فایل‌ها", callback_data="menu_files")],
        [InlineKeyboardButton("🔄 آپدیت", callback_data="menu_update")],
        [InlineKeyboardButton("👤 ادمین", callback_data="menu_admin")],
    ]
    return InlineKeyboardMarkup(buttons)


def downloaders_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🔗 دانلودر لینک مستقیم", callback_data="dl_direct")],
        [InlineKeyboardButton("▶️ دانلودر یوتیوب (فعلاً غیرفعال)", callback_data="dl_youtube_disabled")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def system_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📊 وضعیت سرور", callback_data="sys_status")],
        [InlineKeyboardButton("💾 وضعیت حافظه فایل‌ها", callback_data="sys_storage")],
        [InlineKeyboardButton("♻️ ریست ربات", callback_data="sys_reset")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def files_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📃 لیست فایل‌ها", callback_data="files_list")],
        [InlineKeyboardButton("🗑 حذف ویدیوها", callback_data="files_del_video")],
        [InlineKeyboardButton("🗑 حذف عکس‌ها", callback_data="files_del_image")],
        [InlineKeyboardButton("🗑 حذف zip", callback_data="files_del_zip")],
        [InlineKeyboardButton("🗑 حذف سایر", callback_data="files_del_other")],
        [InlineKeyboardButton("🗑 حذف همه فایل‌ها", callback_data="files_del_all")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def update_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("ℹ️ نسخه فعلی", callback_data="upd_current")],
        [InlineKeyboardButton("🕒 آخرین آپدیت", callback_data="upd_last")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("👥 لیست کاربران ثبت‌شده", callback_data="admin_users")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(buttons)


# ================== هندلرها ==================

async def start(update: Update, context: ContextTypes.Context):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("این ربات خصوصی است و فقط برای صاحبش فعال است.")
        return

    users = context.application.bot_data.setdefault("users", set())
    users.add(user.id)

    await update.message.reply_text(
        "سلام امیر 👋\nربات حرفه‌ای‌ات روشنه.\nاز منوی زیر انتخاب کن:",
        reply_markup=main_menu_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.Context):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_admin(user.id):
        await query.edit_message_text("این ربات خصوصی است.")
        return

    data = query.data

    # برگشت
    if data == "back_main":
        await query.edit_message_text("منوی اصلی:", reply_markup=main_menu_keyboard())
        return

    # منوها
    if data == "menu_downloaders":
        await query.edit_message_text("دانلودرها:", reply_markup=downloaders_menu_keyboard())
        return

    if data == "menu_system":
        await query.edit_message_text("سیستم:", reply_markup=system_menu_keyboard())
        return

    if data == "menu_files":
        await query.edit_message_text("فایل‌ها:", reply_markup=files_menu_keyboard())
        return

    if data == "menu_update":
        await query.edit_message_text("آپدیت:", reply_markup=update_menu_keyboard())
        return

    if data == "menu_admin":
        await query.edit_message_text("ادمین:", reply_markup=admin_menu_keyboard())
        return

    # دانلودر لینک مستقیم
    if data == "dl_direct":
        context.user_data["mode"] = "direct_download"
        await query.edit_message_text("لینک مستقیم فایل را بفرست.")
        return

    if data == "dl_youtube_disabled":
        await query.edit_message_text("دانلودر یوتیوب فعلاً غیرفعال است.", reply_markup=downloaders_menu_keyboard())
        return

    # سیستم
    if data == "sys_status":
        info = get_ram_cpu_info()
        await query.edit_message_text(f"وضعیت سرور:\n\n{info}", reply_markup=system_menu_keyboard())
        return

    if data == "sys_storage":
        disk_info = get_disk_info()
        files = list_downloaded_files()
        total_size = sum(f.stat().st_size for f in files) if files else 0
        await query.edit_message_text(
            f"وضعیت حافظه فایل‌ها:\n"
            f"تعداد فایل‌ها: {len(files)}\n"
            f"حجم پوشه دانلودها: {format_size(total_size)}\n\n"
            f"وضعیت دیسک:\n{disk_info}",
            reply_markup=system_menu_keyboard(),
        )
        return

    if data == "sys_reset":
        await query.edit_message_text("ربات در حال ریست است...")
        os._exit(0)

    # فایل‌ها
    if data == "files_list":
        files = list_downloaded_files()
        if not files:
            text = "هیچ فایلی وجود ندارد."
        else:
            lines = []
            for f in files:
                t = classify_file(f)
                size = format_size(f.stat().st_size)
                lines.append(f"- {f.name} | نوع: {t} | {size}")
            text = "فایل‌ها:\n\n" + "\n".join(lines)
        await query.edit_message_text(text, reply_markup=files_menu_keyboard())
        return

    async def delete_by_type(ftype: str):
        files = list_downloaded_files()
        count = 0
        for f in files:
            if ftype == "all" or classify_file(f) == ftype:
                try:
                    f.unlink()
                    count += 1
                except:
                    pass
        return count

    if data.startswith("files_del_"):
        if data == "files_del_video":
            c = await delete_by_type("video")
            msg = f"{c} ویدیو حذف شد."
        elif data == "files_del_image":
            c = await delete_by_type("image")
            msg = f"{c} عکس حذف شد."
        elif data == "files_del_zip":
            c = await delete_by_type("zip")
            msg = f"{c} فایل zip حذف شد."
        elif data == "files_del_other":
            c = await delete_by_type("other")
            msg = f"{c} فایل سایر حذف شد."
        elif data == "files_del_all":
            c = await delete_by_type("all")
            msg = f"همه فایل‌ها حذف شدند."
        else:
            msg = "خطا"

        await query.edit_message_text(msg, reply_markup=files_menu_keyboard())
        return

    # آپدیت
    if data == "upd_current":
        await query.edit_message_text(
            f"نسخه فعلی: {VERSION}\nتوضیح: {CHANGELOG}",
            reply_markup=update_menu_keyboard(),
        )
        return

    if data == "upd_last":
        await query.edit_message_text(
            f"آخرین آپدیت: {LAST_UPDATE}\nنسخه: {VERSION}\nتوضیح: {CHANGELOG}",
            reply_markup=update_menu_keyboard(),
        )
        return

    # ادمین
    if data == "admin_users":
        users = context.application.bot_data.get("users", set())
        if not users:
            text = "هیچ کاربری ثبت نشده."
        else:
            text = "کاربران:\n" + "\n".join([str(u) for u in users])
        await query.edit_message_text(text, reply_markup=admin_menu_keyboard())
        return


async def text_handler(update: Update, context: ContextTypes.Context):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        return

    text = update.message.text
    mode = context.user_data.get("mode")

    if mode == "direct_download":
        await handle_direct_download(update, context, text)
        context.user_data["mode"] = None
        return

    await update.message.reply_text("از منوی زیر استفاده کن:", reply_markup=main_menu_keyboard())


async def handle_direct_download(update: Update, context: ContextTypes.Context, url: str):
    msg = await update.message.reply_text("در حال بررسی لینک...")

    try:
        head = requests.head(url, allow_redirects=True, timeout=10)
        if not head.ok:
            await msg.edit_text(f"خطا در بررسی لینک.\nکد: {head.status_code}")
            return

        size = int(head.headers.get("Content-Length", 0)) if head.headers.get("Content-Length") else None
        size_text = format_size(size) if size else "نامشخص"

        await msg.edit_text(f"حجم فایل: {size_text}\nدر حال دانلود...")

        filename = url.split("/")[-1] or f"file_{int(datetime.now().timestamp())}"
        filename = filename.split("?")[0]
        file_path = DOWNLOAD_DIR / filename

        r = requests.get(url, stream=True, timeout=20)
        if not r.ok:
            await msg.edit_text(f"خطا در دانلود.\nکد: {r.status_code}")
            return

        total = int(r.headers.get("Content-Length", 0)) if r.headers.get("Content-Length") else None
        downloaded = 0
        chunk_size = 1024 * 256

        with open(file_path, "wb") as f:
            last_percent = -1
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)

                if total:
                    percent = int(downloaded * 100 / total)
                    if percent != last_percent and percent % 5 == 0:
                        last_percent = percent
                        try:
                            await msg.edit_text(
                                f"در حال دانلود...\n"
                                f"پیشرفت: {percent}%\n"
                                f"{format_size(downloaded)} / {format_size(total)}"
                            )
                        except:
                            pass

        await msg.edit_text("دانلود کامل شد. در حال ارسال...")

        await update.message.reply_document(
            document=open(file_path, "rb"),
            filename=file_path.name,
            caption="دانلود کامل شد."
        )

        await msg.edit_text("فایل ارسال شد.")

    except Exception as e:
        await msg.edit_text(f"خطا: {e}")


async def unknown_command(update: Update, context: ContextTypes.Context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        return
    await update.message.reply_text("دستور نامعتبر.", reply_markup=main_menu_keyboard())


# ================== اجرای ربات (Webhook برای Render) ==================

PORT = int(os.environ.get("PORT", 8443))

async def main():
    app = Application.builder().token(TOKEN).build()

    # هندلرها
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # شروع وبهوک
    await app.initialize()
    await app.start()

    webhook_url = f"https://telegram-bot-05x3.onrender.com/{TOKEN}"

    await app.bot.set_webhook(url=webhook_url)

    await app.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url
    )

    await app.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
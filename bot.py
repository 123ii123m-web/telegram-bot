import os
import logging
import asyncio
import aiohttp
import requests
import urllib.request
import subprocess
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================== تنظیمات اصلی ====================

TOKEN = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6300997264"))

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

DOWNLOAD_TIMEOUT = 300  # ثانیه
CHUNK_SIZE = 1024 * 256  # 256KB

RATE_LIMIT_MAX = 10       # حداکثر 10 دانلود
RATE_LIMIT_WINDOW = 60    # در 60 ثانیه

AUTO_CLEANUP_HOURS = 24   # حذف خودکار فایل‌های قدیمی‌تر از 24 ساعت

user_downloads: Dict[int, List[datetime]] = defaultdict(list)
active_downloads: Dict[int, asyncio.Task] = {}  # user_id -> task

# ==================== Logging ====================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== ابزارهای کمکی ====================

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def rate_limited(user_id: int) -> bool:
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW)
    user_times = user_downloads[user_id]
    user_times = [t for t in user_times if t > window_start]
    user_downloads[user_id] = user_times
    if len(user_times) >= RATE_LIMIT_MAX:
        return True
    user_times.append(now)
    return False


def list_downloaded_files() -> List[Path]:
    return [p for p in DOWNLOAD_DIR.iterdir() if p.is_file()]


def cleanup_old_files(hours: int = AUTO_CLEANUP_HOURS) -> int:
    now = datetime.utcnow()
    removed = 0
    for f in list_downloaded_files():
        try:
            mtime = datetime.utcfromtimestamp(f.stat().st_mtime)
            if now - mtime > timedelta(hours=hours):
                f.unlink()
                removed += 1
        except Exception as e:
            logger.error(f"خطا در حذف فایل قدیمی {f.name}: {e}")
    return removed


def system_status() -> str:
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(DOWNLOAD_DIR))
        return (
            f"🖥 وضعیت سیستم:\n"
            f"CPU: {cpu:.1f}%\n"
            f"RAM: {mem.percent:.1f}%\n"
            f"Disk: {disk.percent:.1f}%\n"
        )
    except Exception as e:
        logger.error(f"خطا در گرفتن وضعیت سیستم: {e}")
        return "خطا در گرفتن وضعیت سیستم."


# ==================== کیبوردها ====================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلود از لینک", callback_data="download_link")],
        [InlineKeyboardButton("📁 مدیریت فایل‌ها", callback_data="files_menu")],
        [InlineKeyboardButton("🖥 وضعیت سیستم", callback_data="system_status")],
    ])


def files_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📃 لیست فایل‌ها", callback_data="files_list")],
        [InlineKeyboardButton("🧹 پاک کردن حافظه", callback_data="cleanup_files")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])


def download_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ لغو دانلود", callback_data="cancel_download"),
            InlineKeyboardButton("🔁 تلاش دوباره", callback_data="retry_download"),
        ]
    ])


def files_list_keyboard(files: List[Path]) -> InlineKeyboardMarkup:
    buttons = []
    for f in files:
        buttons.append([InlineKeyboardButton(f.name, callback_data=f"send_file:{f.name}")])
    buttons.append([InlineKeyboardButton("⬅️ برگشت", callback_data="files_menu")])
    return InlineKeyboardMarkup(buttons)


# ==================== دانلود چند روشی ====================

async def download_with_aiohttp(url: str, dest: Path) -> None:
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            with dest.open("wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)


def download_with_requests(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    f.write(chunk)


def download_with_urllib(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r:
        with dest.open("wb") as f:
            while True:
                chunk = r.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)


def download_with_curl(url: str, dest: Path) -> None:
    cmd = ["curl", "-L", "-m", str(DOWNLOAD_TIMEOUT), "-o", str(dest), url]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl error: {result.stderr.decode(errors='ignore')}")


async def multi_method_download(url: str, dest: Path) -> None:
    """
    تلاش چند روشی برای دانلود:
    aiohttp → requests → urllib → curl
    """
    last_error = None

    # 1) aiohttp (async)
    try:
        await download_with_aiohttp(url, dest)
        return
    except Exception as e:
        last_error = e
        logger.warning(f"aiohttp failed: {e}")

    # 2) requests (sync در ترد جدا)
    try:
        await asyncio.to_thread(download_with_requests, url, dest)
        return
    except Exception as e:
        last_error = e
        logger.warning(f"requests failed: {e}")

    # 3) urllib (sync در ترد جدا)
    try:
        await asyncio.to_thread(download_with_urllib, url, dest)
        return
    except Exception as e:
        last_error = e
        logger.warning(f"urllib failed: {e}")

    # 4) curl (sync در ترد جدا)
    try:
        await asyncio.to_thread(download_with_curl, url, dest)
        return
    except Exception as e:
        last_error = e
        logger.warning(f"curl failed: {e}")

    raise RuntimeError(f"تمام روش‌های دانلود شکست خوردند: {last_error}")


# ==================== هندلرها ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if not is_admin(user.id):
        await update.message.reply_text("🚫 دسترسی غیرمجاز.")
        return

    await update.message.reply_text(
        "سلام ادمین 👋\nیک گزینه را انتخاب کنید:",
        reply_markup=main_menu_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    if not is_admin(user.id):
        await update.message.reply_text("🚫 دسترسی غیرمجاز.")
        return

    text = (update.message.text or "").strip()

    # فرض: اگر در حالت انتظار لینک هستیم، از context استفاده می‌کنیم
    state = context.user_data.get("state")
    if state == "awaiting_url":
        url = text  # بدون URL validation طبق خواسته تو
        await process_download_request(update, context, url)
        context.user_data["state"] = None
    else:
        await update.message.reply_text(
            "برای دانلود، از منو گزینه «📥 دانلود از لینک» را انتخاب کنید.",
            reply_markup=main_menu_keyboard(),
        )


async def process_download_request(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user = update.effective_user
    if not user:
        return

    if rate_limited(user.id):
        await update.message.reply_text(
            "⏳ محدودیت دانلود فعال شد، لطفاً کمی صبر کنید.",
            reply_markup=main_menu_keyboard(),
        )
        return

    # نام فایل را از URL می‌گیریم، بدون sanitize
    filename = url.split("/")[-1] or "downloaded_file"
    dest = DOWNLOAD_DIR / filename

    # اگر دانلود فعال قبلی هست، لغوش می‌کنیم
    old_task = active_downloads.get(user.id)
    if old_task and not old_task.done():
        old_task.cancel()

    msg: Message = await update.message.reply_text(
        f"در حال دانلود:\n{url}",
        reply_markup=download_control_keyboard(),
    )

    async def download_task():
        try:
            await multi_method_download(url, dest)
            await msg.edit_text(
                f"✅ دانلود کامل شد:\n{dest.name}",
                reply_markup=main_menu_keyboard(),
            )
        except asyncio.CancelledError:
            if dest.exists():
                try:
                    dest.unlink()
                except Exception as e:
                    logger.error(f"خطا در حذف فایل بعد از لغو: {e}")
            await msg.edit_text(
                "❌ دانلود لغو شد.",
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            logger.error(f"خطا در دانلود: {e}")
            await msg.edit_text(
                f"❌ خطا در دانلود:\n{e}",
                reply_markup=download_control_keyboard(),
            )

    task = asyncio.create_task(download_task())
    active_downloads[user.id] = task
    context.user_data["last_url"] = url
    context.user_data["last_message_id"] = msg.message_id
    context.user_data["last_chat_id"] = msg.chat_id


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_admin(user.id):
        await query.edit_message_text("🚫 دسترسی غیرمجاز.")
        return

    data = query.data

    if data == "download_link":
        context.user_data["state"] = "awaiting_url"
        await query.edit_message_text(
            "لینک فایل را ارسال کنید:",
        )

    elif data == "files_menu":
        await query.edit_message_text(
            "مدیریت فایل‌ها:",
            reply_markup=files_menu_keyboard(),
        )

    elif data == "back_main":
        await query.edit_message_text(
            "منوی اصلی:",
            reply_markup=main_menu_keyboard(),
        )

    elif data == "system_status":
        status = system_status()
        await query.edit_message_text(
            status,
            reply_markup=main_menu_keyboard(),
        )

    elif data == "files_list":
        files = list_downloaded_files()
        if not files:
            await query.edit_message_text(
                "هیچ فایلی موجود نیست.",
                reply_markup=files_menu_keyboard(),
            )
        else:
            await query.edit_message_text(
                "لیست فایل‌ها:",
                reply_markup=files_list_keyboard(files),
            )

    elif data == "cleanup_files":
        try:
            files = list_downloaded_files()
            count = len(files)
            for file_path in files:
                try:
                    file_path.unlink()
                    logger.info(f"فایل حذف شد: {file_path.name}")
                except Exception as e:
                    logger.error(f"خطا در حذف: {e}")
            await query.edit_message_text(
                f"✅ {count} فایل حذف شد.",
                reply_markup=files_menu_keyboard(),
            )
        except Exception as e:
            logger.error(f"خطا در پاک کردن حافظه: {e}")
            await query.edit_message_text(
                "❌ خطا در پاک کردن حافظه.",
                reply_markup=files_menu_keyboard(),
            )

    elif data.startswith("send_file:"):
        filename = data.split(":", 1)[1]
        file_path = DOWNLOAD_DIR / filename
        if not file_path.exists():
            await query.edit_message_text(
                "فایل پیدا نشد.",
                reply_markup=files_menu_keyboard(),
            )
            return
        try:
            await query.message.reply_document(file_path.open("rb"), filename=filename)
        except Exception as e:
            logger.error(f"خطا در ارسال فایل: {e}")
            await query.edit_message_text(
                "خطا در ارسال فایل.",
                reply_markup=files_menu_keyboard(),
            )

    elif data == "cancel_download":
        task = active_downloads.get(user.id)
        if task and not task.done():
            task.cancel()
        else:
            await query.edit_message_text(
                "دانلود فعالی برای لغو وجود ندارد.",
                reply_markup=main_menu_keyboard(),
            )

    elif data == "retry_download":
        url = context.user_data.get("last_url")
        chat_id = context.user_data.get("last_chat_id")
        msg_id = context.user_data.get("last_message_id")
        if not url or not chat_id or not msg_id:
            await query.edit_message_text(
                "لینک قبلی پیدا نشد.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # پیام جدید برای دانلود مجدد
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔁 تلاش دوباره برای دانلود:\n{url}",
                reply_markup=download_control_keyboard(),
            )
            fake_update = Update(
                update.update_id,
                message=msg,
            )
            await process_download_request(fake_update, context, url)
        except Exception as e:
            logger.error(f"خطا در تلاش دوباره: {e}")
            await query.edit_message_text(
                "خطا در تلاش دوباره.",
                reply_markup=main_menu_keyboard(),
            )


async def auto_cleanup_job(app: Application) -> None:
    while True:
        try:
            removed = cleanup_old_files(AUTO_CLEANUP_HOURS)
            if removed:
                logger.info(f"{removed} فایل قدیمی حذف شد.")
        except Exception as e:
            logger.error(f"خطا در پاک‌سازی خودکار: {e}")
        await asyncio.sleep(3600)  # هر یک ساعت


# ==================== main ====================

async def on_startup(app: Application) -> None:
    asyncio.create_task(auto_cleanup_job(app))
    logger.info("ربات با موفقیت راه‌اندازی شد.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("API_KEY تنظیم نشده است.")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.post_init = on_startup

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
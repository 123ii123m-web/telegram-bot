import logging
import os
import asyncio
import aiohttp
import psutil
import mimetypes
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
from typing import Optional

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

TOKEN = os.getenv("API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6300997264"))  # از environment variable بخونید

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# تنظیمات محدودیت
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
DOWNLOAD_TIMEOUT = 300  # 5 دقیقه
CHUNK_SIZE = 1024 * 256  # 256 KB

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    """بررسی کنید آیا کاربر ادمین است."""
    return user_id == ADMIN_ID

def format_size(num_bytes: int) -> str:
    """تبدیل بایت به فرمت خوانا."""
    if num_bytes is None:
        return "نامشخص"
    num_bytes = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"

# ✅ تغییر: حذف sanitize_filename() - نام فایل اصلی را نگه‌دار

def list_downloaded_files():
    """لیست تمام فایل های دانلود شده."""
    try:
        return sorted([p for p in DOWNLOAD_DIR.iterdir() if p.is_file()], 
                     key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:
        logger.error(f"خطا در لیست فایل ها: {e}")
        return []

def cleanup_old_files(max_age_hours: int = 24):
    """فایل های قدیمی را حذف کنید."""
    try:
        now = datetime.now()
        for file_path in DOWNLOAD_DIR.iterdir():
            if file_path.is_file():
                file_age = (now - datetime.fromtimestamp(file_path.stat().st_mtime)).total_seconds() / 3600
                if file_age > max_age_hours:
                    file_path.unlink()
                    logger.info(f"فایل قدیمی حذف شد: {file_path.name}")
    except Exception as e:
        logger.error(f"خطا در پاک کردن فایل های قدیمی: {e}")

def main_menu_keyboard():
    """منوی اصلی."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلودرها", callback_data="menu_downloaders")],
        [InlineKeyboardButton("🖥 سیستم", callback_data="menu_system")],
        [InlineKeyboardButton("📂 فایل‌ها", callback_data="menu_files")],
    ])

def downloaders_menu_keyboard():
    """منوی دانلودرها."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 دانلودر لینک مستقیم", callback_data="dl_direct")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])

def system_menu_keyboard():
    """منوی سیستم."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 وضعیت سرور", callback_data="sys_status")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])

def files_menu_keyboard():
    """منوی فایل ها."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📃 لیست فایل‌ها", callback_data="files_list")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])

async def start(update: Update, context: ContextTypes.Context):
    """شروع ربات."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        logger.warning(f"دسترسی غیرمجاز: {update.effective_user.id}")
        return
    await update.message.reply_text("سلام 👋", reply_markup=main_menu_keyboard())

async def handle_callback(update: Update, context: ContextTypes.Context):
    """مدیریت دکمه های منو."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("این ربات خصوصی است.")
        logger.warning(f"دسترسی غیرمجاز: {query.from_user.id}")
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
        try:
            ram = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.5)
            disk = psutil.disk_usage(DOWNLOAD_DIR)
            
            status_text = (
                f"📊 وضعیت سرور:\n\n"
                f"CPU: {cpu}%\n"
                f"RAM: {format_size(ram.used)} / {format_size(ram.total)} ({ram.percent}%)\n"
                f"Disk: {format_size(disk.used)} / {format_size(disk.total)} ({disk.percent}%)"
            )
            await query.edit_message_text(status_text, reply_markup=system_menu_keyboard())
        except Exception as e:
            logger.error(f"خطا در دریافت وضعیت سیستم: {e}")
            await query.edit_message_text(f"خطا: {e}")
        return

    if data == "files_list":
        files = list_downloaded_files()
        if not files:
            await query.edit_message_text("هیچ فایلی نیست.", reply_markup=files_menu_keyboard())
            return
        
        # اگر فایل های زیادی هستند، فقط آخری 10 تا نمایش دهید
        file_list = "\n".join([
            f"📄 {f.name} ({format_size(f.stat().st_size)})" 
            for f in files[:10]
        ])
        if len(files) > 10:
            file_list += f"\n... و {len(files) - 10} فایل دیگر"
        
        await query.edit_message_text(f"📂 فایل های دانلود شده:\n\n{file_list}", 
                                      reply_markup=files_menu_keyboard())
        return

async def text_handler(update: Update, context: ContextTypes.Context):
    """مدیریت پیام های متنی."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("این ربات خصوصی است.")
        logger.warning(f"دسترسی غیرمجاز: {update.effective_user.id}")
        return

    mode = context.user_data.get("mode")

    if mode == "direct_download":
        await handle_direct_download(update, context, update.message.text)
        context.user_data["mode"] = None
        return

    await update.message.reply_text("از منو استفاده کن.", reply_markup=main_menu_keyboard())

async def handle_direct_download(update: Update, context: ContextTypes.Context, url: str) -> None:
    """دانلودر لینک مستقیم."""
    url = url.strip()
    
    # اعتبارسنجی URL
    try:
        result = urlparse(url)
        if not all([result.scheme, result.netloc]):
            await update.message.reply_text("❌ URL نامعتبر است!")
            return
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در URL: {e}")
        return

    msg = await update.message.reply_text("⏳ در حال دانلود...")

    try:
        # دانلود با aiohttp برای عملکرد بهتر
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as head_response:
                if head_response.status != 200:
                    await msg.edit_text(f"❌ خطا: وضعیت {head_response.status}")
                    return
                
                # بررسی حجم فایل
                content_length = head_response.content_length
                if content_length and content_length > MAX_FILE_SIZE:
                    await msg.edit_text(f"❌ فایل خیلی بزرگ است! (بیش از {format_size(MAX_FILE_SIZE)})")
                    return

            # دانلود فایل
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as response:
                if response.status != 200:
                    await msg.edit_text(f"❌ خطا: وضعیت {response.status}")
                    return

                # ✅ تغییر: استخراج نام فایل بدون sanitize
                # نام فایل اصلی را دقیقاً همانطور که است ذخیره کن
                filename = url.split("/")[-1].split("?")[0] or "downloaded_file"
                # بدون استفاده از sanitize_filename()
                file_path = DOWNLOAD_DIR / filename

                # دانلود و ذخیره فایل
                downloaded_size = 0
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as resp:
                        with open(file_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                                    downloaded_size += len(chunk)
                                    
                                    # بررسی حجم زیادی
                                    if downloaded_size > MAX_FILE_SIZE:
                                        file_path.unlink()
                                        await msg.edit_text(f"❌ فایل خیلی بزرگ است!")
                                        return

        # ارسال فایل به کاربر
        try:
            with open(file_path, "rb") as f:
                await update.message.reply_document(f, caption=f"✅ دانلود تمام شد\n📄 {filename}\n📊 {format_size(file_path.stat().st_size)}")
            await msg.edit_text("✅ فایل ارسال شد!")
        except Exception as e:
            logger.error(f"خطا در ارسال فایل: {e}")
            await msg.edit_text(f"❌ خطا در ارسال فایل: {e}")

    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ زمان دانلود تمام شد (بیش از 5 دقیقه)")
        logger.error(f"Timeout برای دانلود: {url}")
    except Exception as e:
        logger.error(f"خطا در دانلود: {e}")
        await msg.edit_text(f"❌ خطا: {str(e)[:100]}")

async def unknown_command(update: Update, context: ContextTypes.Context):
    """مدیریت دستورات نامعتبر."""
    await update.message.reply_text("❌ دستور نامعتبر است.")

async def main():
    """تابع اصلی."""
    if not TOKEN:
        logger.error("TOKEN یافت نشد! لطفا API_KEY را تنظیم کنید.")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("ربات شروع شد...")
    
    # پاک کردن فایل های قدیمی هر ساعت
    async def periodic_cleanup():
        while True:
            await asyncio.sleep(3600)  # هر ساعت
            cleanup_old_files()
    
    # شروع cleanup در پس زمینه
    asyncio.create_task(periodic_cleanup())

    await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد.")

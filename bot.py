import os
import re
import json
import hashlib
import logging
import asyncio
import time
import aiohttp
import requests
import urllib.request
import subprocess
import psutil
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")  # بدون @

# رمز ورود — هش شده ذخیره می‌شه
BOT_PASSWORD = "Amir1388"
BOT_PASSWORD_HASH = hashlib.sha256(BOT_PASSWORD.encode()).hexdigest()

# Google Drive
GDRIVE_CREDENTIALS = os.getenv("GDRIVE_CREDENTIALS")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
STATS_FILE = BASE_DIR / "stats.json"
AUTH_FILE = BASE_DIR / "auth.json"

# دسته‌بندی پوشه‌ها
CATEGORIES = {
    "music":  {"dir": DOWNLOAD_DIR / "music",  "label": "🎵 آهنگ",     "exts": {".mp3",".flac",".aac",".ogg",".wav",".m4a",".opus",".wma"}},
    "video":  {"dir": DOWNLOAD_DIR / "video",  "label": "🎬 ویدیو",    "exts": {".mp4",".mkv",".avi",".mov",".webm",".flv",".wmv",".m4v",".ts"}},
    "image":  {"dir": DOWNLOAD_DIR / "image",  "label": "🖼 عکس",      "exts": {".jpg",".jpeg",".png",".gif",".webp",".bmp",".svg",".tiff"}},
    "zip":    {"dir": DOWNLOAD_DIR / "zip",    "label": "🗜 فشرده",     "exts": {".zip",".rar",".7z",".tar",".gz",".bz2",".xz",".tar.gz"}},
    "other":  {"dir": DOWNLOAD_DIR / "other",  "label": "📄 سایر",      "exts": set()},
}
for cat in CATEGORIES.values():
    cat["dir"].mkdir(parents=True, exist_ok=True)

DOWNLOAD_TIMEOUT = 300
CHUNK_SIZE = 1024 * 256
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 60
AUTO_CLEANUP_HOURS = 24
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024
MAX_QUEUE_SIZE = 20

user_downloads: Dict[int, List[datetime]] = defaultdict(list)
active_downloads: Dict[int, asyncio.Task] = {}
download_queue: Dict[int, List[str]] = defaultdict(list)
queue_processing: Dict[int, bool] = defaultdict(bool)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== احراز هویت ====================

def load_auth() -> dict:
    if AUTH_FILE.exists():
        try:
            with AUTH_FILE.open("r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"authenticated_users": []}


def save_auth(data: dict) -> None:
    with AUTH_FILE.open("w") as f:
        json.dump(data, f)


def is_authenticated(user_id: int) -> bool:
    auth = load_auth()
    return user_id in auth.get("authenticated_users", [])


def authenticate_user(user_id: int) -> None:
    auth = load_auth()
    users = auth.get("authenticated_users", [])
    if user_id not in users:
        users.append(user_id)
    auth["authenticated_users"] = users
    save_auth(auth)


def check_password(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == BOT_PASSWORD_HASH


def is_admin(user_id: int, username: str = "") -> bool:
    if user_id == ADMIN_ID:
        return True
    if ADMIN_USERNAME and username and username.lower() == ADMIN_USERNAME.lower():
        return True
    return False


def is_allowed(user_id: int, username: str = "") -> bool:
    """کاربر باید هم ادمین باشه هم احراز هویت کرده باشه"""
    return is_admin(user_id, username) and is_authenticated(user_id)


# ==================== دسته‌بندی فایل ====================

def get_category(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    # بررسی tar.gz و مشابه
    if filename.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return "zip"
    for cat_name, cat_info in CATEGORIES.items():
        if cat_name == "other":
            continue
        if ext in cat_info["exts"]:
            return cat_name
    return "other"


def get_dest_path(filename: str) -> Path:
    cat = get_category(filename)
    return CATEGORIES[cat]["dir"] / filename


def list_files_by_category(category: str) -> List[Path]:
    cat_dir = CATEGORIES[category]["dir"]
    return sorted(
        [p for p in cat_dir.iterdir() if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def list_all_files() -> List[Path]:
    all_files = []
    for cat in CATEGORIES.values():
        all_files.extend([p for p in cat["dir"].iterdir() if p.is_file()])
    return sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)


# ==================== آمار ====================

def load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            with STATS_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_downloads": 0, "total_bytes": 0,
        "failed_downloads": 0, "cancelled_downloads": 0,
        "cloud_uploads": 0, "yt_dlp_downloads": 0,
        "downloads_by_category": {},
        "daily_downloads": {},
        "last_download_time": None, "last_download_url": None,
    }


def save_stats(stats: dict) -> None:
    try:
        with STATS_FILE.open("w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطا در ذخیره آمار: {e}")


def update_stats(**kwargs) -> None:
    stats = load_stats()
    for key, value in kwargs.items():
        if key in ("total_downloads","total_bytes","failed_downloads",
                   "cancelled_downloads","cloud_uploads","yt_dlp_downloads"):
            stats[key] = stats.get(key, 0) + value
        elif key == "category":
            cat_stats = stats.setdefault("downloads_by_category", {})
            cat_stats[value] = cat_stats.get(value, 0) + 1
        elif key == "daily":
            today = datetime.utcnow().strftime("%Y-%m-%d")
            daily = stats.setdefault("daily_downloads", {})
            daily[today] = daily.get(today, 0) + 1
        else:
            stats[key] = value
    save_stats(stats)


def format_stats() -> str:
    s = load_stats()
    total_mb = s.get("total_bytes", 0) / 1024 / 1024
    total_gb = total_mb / 1024

    # آمار روزانه — 7 روز اخیر
    daily = s.get("daily_downloads", {})
    today = datetime.utcnow()
    weekly = []
    for i in range(6, -1, -1):
        day = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        day_label = (today - timedelta(days=i)).strftime("%m/%d")
        count = daily.get(day, 0)
        bar = "█" * min(count, 10)
        weekly.append(f"  {day_label}: {bar} {count}")

    # دسته‌بندی
    by_cat = s.get("downloads_by_category", {})
    cat_lines = []
    for cat_name, cat_info in CATEGORIES.items():
        count = by_cat.get(cat_name, 0)
        if count > 0:
            cat_lines.append(f"  {cat_info['label']}: {count}")

    size_str = f"{total_gb:.2f} GB" if total_gb >= 1 else f"{total_mb:.1f} MB"
    last_time = s.get("last_download_time", "—")
    last_url = s.get("last_download_url", "—") or "—"
    if len(last_url) > 45:
        last_url = last_url[:42] + "..."

    text = (
        f"📊 آمار کامل ربات\n"
        f"{'─'*25}\n"
        f"✅ موفق: {s.get('total_downloads',0)}\n"
        f"❌ ناموفق: {s.get('failed_downloads',0)}\n"
        f"🚫 لغو شده: {s.get('cancelled_downloads',0)}\n"
        f"☁️ آپلود ابر: {s.get('cloud_uploads',0)}\n"
        f"🎬 yt-dlp: {s.get('yt_dlp_downloads',0)}\n"
        f"💾 حجم کل: {size_str}\n"
    )
    if cat_lines:
        text += f"\n📁 دسته‌بندی:\n" + "\n".join(cat_lines) + "\n"
    text += f"\n📅 7 روز اخیر:\n" + "\n".join(weekly) + "\n"
    text += f"\n🕐 آخرین: {last_time}\n🔗 {last_url}"
    return text


# ==================== سرعت و شبکه ====================

async def measure_network() -> str:
    results = []

    # پینگ به چند سرور
    ping_targets = [
        ("Google", "8.8.8.8"),
        ("Cloudflare", "1.1.1.1"),
        ("Telegram", "149.154.167.51"),
    ]

    ping_lines = []
    for name, host in ping_targets:
        try:
            t_start = time.time()
            proc = await asyncio.create_subprocess_exec(
                "ping", "-c", "3", "-W", "2", host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode()
            # استخراج avg از خروجی ping
            match = re.search(r"min/avg/max.*?=([\d.]+)/([\d.]+)/([\d.]+)", output)
            if match:
                avg_ms = float(match.group(2))
                emoji = "🟢" if avg_ms < 100 else "🟡" if avg_ms < 300 else "🔴"
                ping_lines.append(f"  {emoji} {name}: {avg_ms:.1f}ms")
            else:
                ping_lines.append(f"  🔴 {name}: timeout")
        except Exception:
            ping_lines.append(f"  🔴 {name}: خطا")

    results.append("🏓 پینگ:\n" + "\n".join(ping_lines))

    # سرعت دانلود — فایل 5MB از Cloudflare
    try:
        t_start = time.time()
        test_url = "https://speed.cloudflare.com/__down?bytes=5000000"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(test_url) as resp:
                data = await resp.read()
        elapsed = time.time() - t_start
        size_mb = len(data) / 1024 / 1024
        speed_mbps = (size_mb * 8) / elapsed
        emoji = "🟢" if speed_mbps > 10 else "🟡" if speed_mbps > 2 else "🔴"
        results.append(f"⬇️ سرعت دانلود:\n  {emoji} {speed_mbps:.1f} Mbps ({size_mb:.1f}MB در {elapsed:.1f}s)")
    except Exception as e:
        results.append(f"⬇️ سرعت دانلود:\n  🔴 خطا: {e}")

    # سرعت آپلود — ارسال 2MB به httpbin
    try:
        t_start = time.time()
        data = b"x" * 2 * 1024 * 1024
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://httpbin.org/post", data=data) as resp:
                await resp.read()
        elapsed = time.time() - t_start
        size_mb = len(data) / 1024 / 1024
        speed_mbps = (size_mb * 8) / elapsed
        emoji = "🟢" if speed_mbps > 5 else "🟡" if speed_mbps > 1 else "🔴"
        results.append(f"⬆️ سرعت آپلود:\n  {emoji} {speed_mbps:.1f} Mbps ({size_mb:.1f}MB در {elapsed:.1f}s)")
    except Exception as e:
        results.append(f"⬆️ سرعت آپلود:\n  🔴 خطا: {e}")

    # سرعت پاسخ ربات
    t_start = time.time()
    await asyncio.sleep(0)
    bot_latency = (time.time() - t_start) * 1000
    results.append(f"🤖 تأخیر ربات:\n  🟢 {bot_latency:.2f}ms")

    # وضعیت سیستم
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(DOWNLOAD_DIR.parent))
    files = list_all_files()
    total_size = sum(f.stat().st_size for f in files)

    results.append(
        f"🖥 سیستم:\n"
        f"  CPU: {cpu:.1f}%\n"
        f"  RAM: {mem.percent:.1f}% ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB)\n"
        f"  Disk: {disk.percent:.1f}% ({disk.free//1024//1024//1024}GB آزاد)\n"
        f"  📁 {len(files)} فایل ({total_size//1024//1024}MB)"
    )

    now = datetime.utcnow().strftime("%H:%M:%S UTC")
    return f"📡 گزارش شبکه و سیستم\n🕐 {now}\n{'─'*25}\n\n" + "\n\n".join(results)


# ==================== ابزارهای کمکی ====================

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


def safe_filename(url: str) -> str:
    raw_name = url.split("/")[-1].split("?")[0] or "downloaded_file"
    safe = re.sub(r"[^\w.\-]", "_", raw_name)
    safe = Path(safe).name
    if not safe or safe.startswith("."):
        safe = "downloaded_file"
    return safe[:200]


def is_yt_dlp_url(url: str) -> bool:
    domains = [
        "youtube.com","youtu.be","instagram.com",
        "twitter.com","x.com","tiktok.com",
        "vimeo.com","dailymotion.com","twitch.tv","soundcloud.com",
    ]
    return any(d in url.lower() for d in domains)


def format_file_info(path: Path) -> str:
    size = path.stat().st_size
    if size > 1024*1024:
        size_str = f"{size/1024/1024:.1f}MB"
    elif size > 1024:
        size_str = f"{size/1024:.1f}KB"
    else:
        size_str = f"{size}B"
    mtime = datetime.utcfromtimestamp(path.stat().st_mtime).strftime("%m/%d %H:%M")
    return f"{path.name[:30]} | {size_str} | {mtime}"


def cleanup_old_files(hours: int = AUTO_CLEANUP_HOURS) -> int:
    now = datetime.utcnow()
    removed = 0
    for f in list_all_files():
        try:
            mtime = datetime.utcfromtimestamp(f.stat().st_mtime)
            if now - mtime > timedelta(hours=hours):
                f.unlink()
                removed += 1
        except Exception as e:
            logger.error(f"خطا در حذف {f.name}: {e}")
    return removed


# ==================== Google Drive ====================

async def upload_to_gdrive(file_path: Path) -> Optional[str]:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2 import service_account

        if not GDRIVE_CREDENTIALS or not Path(GDRIVE_CREDENTIALS).exists():
            return None

        creds = service_account.Credentials.from_service_account_file(
            GDRIVE_CREDENTIALS,
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        service = build("drive", "v3", credentials=creds)
        file_metadata = {"name": file_path.name}
        if GDRIVE_FOLDER_ID:
            file_metadata["parents"] = [GDRIVE_FOLDER_ID]
        media = MediaFileUpload(str(file_path), resumable=True)
        uploaded = await asyncio.to_thread(
            lambda: service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )
        update_stats(cloud_uploads=1)
        return uploaded.get("webViewLink", "")
    except ImportError:
        return None
    except Exception as e:
        logger.error(f"خطا در Google Drive: {e}")
        return None


# ==================== yt-dlp ====================

async def download_with_ytdlp(url: str, category: str = "video") -> Tuple[bool, str, Optional[Path]]:
    try:
        import yt_dlp

        dest_dir = CATEGORIES[category]["dir"]

        # تشخیص صدا یا ویدیو
        is_audio = category == "music"
        output_template = str(dest_dir / "%(title).50s.%(ext)s")

        if is_audio:
            ydl_opts = {
                "outtmpl": output_template,
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
                "quiet": True,
            }
        else:
            ydl_opts = {
                "outtmpl": output_template,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "quiet": True,
            }

        info = {}

        def do_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                nonlocal info
                info = ydl.extract_info(url, download=True)

        await asyncio.to_thread(do_download)

        files = sorted(dest_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            update_stats(yt_dlp_downloads=1, total_downloads=1,
                        total_bytes=files[0].stat().st_size, category=category, daily=1)
            return True, f"✅ دانلود شد: {files[0].name}", files[0]

        return False, "❌ فایل پیدا نشد.", None

    except ImportError:
        return False, "❌ yt-dlp نصب نیست.\nنصب: pip install yt-dlp", None
    except Exception as e:
        logger.error(f"خطا در yt-dlp: {e}")
        return False, f"❌ خطا: {e}", None


# ==================== کیبوردها ====================

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 دانلود از لینک", callback_data="download_link")],
        [InlineKeyboardButton("🎬 دانلود ویدیو", callback_data="download_yt_video"),
         InlineKeyboardButton("🎵 دانلود صدا", callback_data="download_yt_audio")],
        [InlineKeyboardButton("📋 صف دانلود", callback_data="queue_menu")],
        [InlineKeyboardButton("📁 مدیریت فایل‌ها", callback_data="files_menu")],
        [InlineKeyboardButton("📊 آمار", callback_data="show_stats"),
         InlineKeyboardButton("📡 شبکه و سرعت", callback_data="network_test")],
    ])


def files_menu_keyboard() -> InlineKeyboardMarkup:
    # نشون دادن تعداد هر دسته
    buttons = []
    for cat_name, cat_info in CATEGORIES.items():
        files = list_files_by_category(cat_name)
        count = len(files)
        size = sum(f.stat().st_size for f in files) // 1024 // 1024
        label = f"{cat_info['label']} ({count} | {size}MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"files_cat:{cat_name}")])

    buttons.append([
        InlineKeyboardButton("📦 دریافت همه فایل‌ها", callback_data="send_all_files"),
        InlineKeyboardButton("🗑 حذف همه", callback_data="cleanup_all"),
    ])
    buttons.append([InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def category_files_keyboard(category: str) -> InlineKeyboardMarkup:
    files = list_files_by_category(category)
    cat_info = CATEGORIES[category]
    buttons = []
    for f in files[:20]:
        size_mb = f.stat().st_size / 1024 / 1024
        label = f"📄 {f.name[:28]} ({size_mb:.1f}MB)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"send_file:{f.name}")])

    buttons.append([
        InlineKeyboardButton(f"🗑 حذف همه {cat_info['label']}", callback_data=f"cleanup_cat:{category}"),
        InlineKeyboardButton("⬅️ برگشت", callback_data="files_menu"),
    ])
    return InlineKeyboardMarkup(buttons)


def download_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ لغو", callback_data="cancel_download"),
        InlineKeyboardButton("🔁 تلاش دوباره", callback_data="retry_download"),
    ]])


def after_download_keyboard(file_path: Optional[Path] = None) -> InlineKeyboardMarkup:
    buttons = []
    if file_path and GDRIVE_CREDENTIALS:
        buttons.append([InlineKeyboardButton("☁️ آپلود به Google Drive", callback_data=f"gdrive:{file_path.name}")])
    buttons.append([
        InlineKeyboardButton("📤 ارسال فایل", callback_data=f"send_file:{file_path.name}" if file_path else "back_main"),
        InlineKeyboardButton("🏠 منو", callback_data="back_main"),
    ])
    return InlineKeyboardMarkup(buttons)


def queue_keyboard(queue: List[str]) -> InlineKeyboardMarkup:
    buttons = []
    for i, url in enumerate(queue[:8]):
        short = url[:33] + "..." if len(url) > 33 else url
        buttons.append([InlineKeyboardButton(f"🗑 {i+1}. {short}", callback_data=f"remove_queue:{i}")])
    buttons.append([
        InlineKeyboardButton("▶️ شروع پردازش", callback_data="process_queue"),
        InlineKeyboardButton("🗑 پاک کردن کل", callback_data="clear_queue"),
    ])
    buttons.append([InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


def yt_quality_keyboard(url: str, is_audio: bool = False) -> InlineKeyboardMarkup:
    safe_url = url[:200]
    if is_audio:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 MP3 بهترین کیفیت", callback_data=f"yt_dl:audio:best:{safe_url}")],
            [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 بهترین کیفیت (MP4)", callback_data=f"yt_dl:video:best:{safe_url}")],
        [InlineKeyboardButton("📱 720p", callback_data=f"yt_dl:video:720:{safe_url}")],
        [InlineKeyboardButton("📺 480p", callback_data=f"yt_dl:video:480:{safe_url}")],
        [InlineKeyboardButton("💾 360p (کم‌حجم)", callback_data=f"yt_dl:video:360:{safe_url}")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
    ])


# ==================== دانلود چند روشی ====================

async def download_with_aiohttp(url: str, dest: Path, progress_callback=None) -> None:
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with dest.open("wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total > 0:
                            await progress_callback(downloaded, total)


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


async def multi_method_download(url: str, dest: Path, progress_callback=None) -> None:
    last_error = None
    for method_name, method in [
        ("aiohttp", lambda: download_with_aiohttp(url, dest, progress_callback)),
        ("requests", lambda: asyncio.to_thread(download_with_requests, url, dest)),
        ("urllib",   lambda: asyncio.to_thread(download_with_urllib, url, dest)),
        ("curl",     lambda: asyncio.to_thread(download_with_curl, url, dest)),
    ]:
        try:
            coro = method()
            if asyncio.iscoroutine(coro):
                await coro
            else:
                await coro
            return
        except Exception as e:
            last_error = e
            logger.warning(f"{method_name} failed: {e}")

    raise RuntimeError(f"تمام روش‌های دانلود شکست خوردند: {last_error}")


# ==================== پردازش دانلود ====================

async def process_download_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    edit_message: Optional[Message] = None,
    from_queue: bool = False,
    yt_category: str = "video",
    yt_quality: str = "best",
) -> None:
    user = update.effective_user
    if not user:
        return

    if not from_queue and rate_limited(user.id):
        text = "⏳ محدودیت دانلود فعال. لطفاً کمی صبر کنید."
        if edit_message:
            await edit_message.edit_text(text, reply_markup=main_menu_keyboard())
        elif hasattr(update, 'message') and update.message:
            await update.message.reply_text(text, reply_markup=main_menu_keyboard())
        return

    context.user_data["last_url"] = url
    use_ytdlp = is_yt_dlp_url(url)

    status_text = f"⏳ در حال دانلود...\n🔗 {url[:60]}{'...' if len(url)>60 else ''}"

    if edit_message:
        msg = edit_message
        await msg.edit_text(status_text, reply_markup=download_control_keyboard())
    elif hasattr(update, 'message') and update.message:
        msg = await update.message.reply_text(status_text, reply_markup=download_control_keyboard())
    else:
        return

    context.user_data["last_message_id"] = msg.message_id
    context.user_data["last_chat_id"] = msg.chat_id

    last_progress = {"pct": -1}

    async def progress_callback(downloaded: int, total: int):
        pct = int(downloaded / total * 100)
        if pct - last_progress["pct"] >= 10:
            last_progress["pct"] = pct
            filled = pct // 10
            bar = "▓" * filled + "░" * (10 - filled)
            speed_kb = (downloaded / 1024)
            try:
                await msg.edit_text(
                    f"⏳ {bar} {pct}%\n"
                    f"📥 {downloaded//1024//1024}MB / {total//1024//1024}MB\n"
                    f"🔗 {url[:50]}{'...' if len(url)>50 else ''}",
                    reply_markup=download_control_keyboard(),
                )
            except Exception:
                pass

    async def download_task():
        downloaded_file: Optional[Path] = None
        try:
            if use_ytdlp:
                success, result_msg, downloaded_file = await download_with_ytdlp(url, yt_category)
                if not success:
                    await msg.edit_text(result_msg, reply_markup=main_menu_keyboard())
                    update_stats(failed_downloads=1)
                    return
            else:
                filename = safe_filename(url)
                dest = get_dest_path(filename)
                downloaded_file = dest
                await multi_method_download(url, dest, progress_callback)
                cat = get_category(filename)
                update_stats(total_downloads=1, total_bytes=dest.stat().st_size,
                           category=cat, daily=1,
                           last_download_time=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                           last_download_url=url)

            file_size = downloaded_file.stat().st_size
            context.user_data["last_downloaded_file"] = str(downloaded_file)

            size_str = f"{file_size/1024/1024:.1f}MB"
            cat_label = CATEGORIES[get_category(downloaded_file.name)]["label"]

            if file_size > MAX_TELEGRAM_FILE_SIZE:
                await msg.edit_text(
                    f"✅ دانلود کامل!\n"
                    f"📄 {downloaded_file.name}\n"
                    f"💾 {size_str} | {cat_label}\n"
                    f"⚠️ حجم بیشتر از 50MB — در سرور ذخیره شد.",
                    reply_markup=after_download_keyboard(downloaded_file),
                )
            else:
                await msg.edit_text(
                    f"✅ دانلود کامل!\n"
                    f"📄 {downloaded_file.name}\n"
                    f"💾 {size_str} | {cat_label}",
                    reply_markup=after_download_keyboard(downloaded_file),
                )

        except asyncio.CancelledError:
            if downloaded_file and downloaded_file.exists():
                try: downloaded_file.unlink()
                except Exception: pass
            update_stats(cancelled_downloads=1)
            await msg.edit_text("❌ دانلود لغو شد.", reply_markup=main_menu_keyboard())

        except Exception as e:
            logger.error(f"خطا در دانلود: {e}")
            update_stats(failed_downloads=1)
            await msg.edit_text(f"❌ خطا:\n{e}", reply_markup=download_control_keyboard())

    task = asyncio.create_task(download_task())
    active_downloads[user.id] = task


# ==================== صف دانلود ====================

async def process_queue(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if queue_processing[user_id]:
        return
    queue_processing[user_id] = True
    total = len(download_queue[user_id])
    done = 0
    try:
        while download_queue[user_id]:
            url = download_queue[user_id].pop(0)
            done += 1
            remaining = len(download_queue[user_id])

            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 صف [{done}/{total}]\n⏳ {url[:60]}...\n{remaining} لینک باقی",
                reply_markup=download_control_keyboard(),
            )

            class FakeUser:
                id = user_id

            class FakeUpdate:
                effective_user = FakeUser()
                message = status_msg

            await process_download_request(
                FakeUpdate(), context, url,
                edit_message=status_msg, from_queue=True,
            )

            task = active_downloads.get(user_id)
            if task:
                try:
                    await asyncio.wait_for(task, timeout=DOWNLOAD_TIMEOUT + 60)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            await asyncio.sleep(2)
    finally:
        queue_processing[user_id] = False
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ پردازش صف تمام شد! ({done} دانلود)",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass


# ==================== هندلرهای اصلی ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    username = user.username or ""

    if not is_admin(user.id, username):
        await update.message.reply_text("🚫 دسترسی غیرمجاز.")
        return

    if not is_authenticated(user.id):
        context.user_data["state"] = "awaiting_password"
        await update.message.reply_text(
            "🔐 برای اولین بار نیاز به تأیید هویت دارید.\n"
            "رمز عبور را وارد کنید:"
        )
        return

    await update.message.reply_text(
        f"سلام {user.first_name} 👋\nیک گزینه را انتخاب کنید:",
        reply_markup=main_menu_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    username = user.username or ""

    if not is_admin(user.id, username):
        await update.message.reply_text("🚫 دسترسی غیرمجاز.")
        return

    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    # بررسی رمز عبور
    if state == "awaiting_password":
        if check_password(text):
            authenticate_user(user.id)
            context.user_data["state"] = None
            await update.message.reply_text(
                f"✅ احراز هویت موفق!\nسلام {user.first_name} 👋",
                reply_markup=main_menu_keyboard(),
            )
        else:
            await update.message.reply_text(
                "❌ رمز اشتباه است. دوباره امتحان کنید:"
            )
        return

    if not is_authenticated(user.id):
        context.user_data["state"] = "awaiting_password"
        await update.message.reply_text("🔐 رمز عبور را وارد کنید:")
        return

    if state == "awaiting_url":
        context.user_data["state"] = None
        url = text
        if is_yt_dlp_url(url):
            context.user_data["pending_url"] = url
            await update.message.reply_text(
                f"🔗 لینک شناسایی شد:\n{url[:60]}{'...' if len(url)>60 else ''}\n\n"
                "کیفیت مورد نظر را انتخاب کنید:",
                reply_markup=yt_quality_keyboard(url),
            )
        else:
            await process_download_request(update, context, url)

    elif state == "awaiting_yt_url_video":
        context.user_data["state"] = None
        context.user_data["pending_url"] = text
        await update.message.reply_text(
            "کیفیت مورد نظر را انتخاب کنید:",
            reply_markup=yt_quality_keyboard(text, is_audio=False),
        )

    elif state == "awaiting_yt_url_audio":
        context.user_data["state"] = None
        context.user_data["pending_url"] = text
        await update.message.reply_text(
            "نوع فایل صوتی را انتخاب کنید:",
            reply_markup=yt_quality_keyboard(text, is_audio=True),
        )

    elif state == "awaiting_queue_url":
        queue = download_queue[user.id]
        if len(queue) >= MAX_QUEUE_SIZE:
            await update.message.reply_text(
                f"⚠️ صف پر است ({MAX_QUEUE_SIZE}).",
                reply_markup=main_menu_keyboard(),
            )
        else:
            queue.append(text)
            await update.message.reply_text(
                f"✅ اضافه شد! ({len(queue)}/{MAX_QUEUE_SIZE})\n\n"
                "لینک بعدی را بفرستید یا /done برای شروع:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ شروع پردازش", callback_data="process_queue")],
                    [InlineKeyboardButton("📋 مشاهده صف", callback_data="queue_menu")],
                ])
            )
    else:
        await update.message.reply_text("از منو گزینه‌ای انتخاب کنید:", reply_markup=main_menu_keyboard())


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_allowed(user.id, user.username or ""):
        return
    context.user_data["state"] = None
    queue = download_queue[user.id]
    if not queue:
        await update.message.reply_text("صف خالی است.", reply_markup=main_menu_keyboard())
        return
    await update.message.reply_text(f"▶️ شروع پردازش {len(queue)} لینک...", reply_markup=main_menu_keyboard())
    asyncio.create_task(process_queue(user.id, context, update.effective_chat.id))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    username = user.username or ""

    if not is_admin(user.id, username):
        await query.edit_message_text("🚫 دسترسی غیرمجاز.")
        return

    if not is_authenticated(user.id):
        context.user_data["state"] = "awaiting_password"
        await query.edit_message_text("🔐 رمز عبور را در چت وارد کنید:")
        return

    data = query.data

    # ==================== منوی اصلی ====================
    if data == "back_main":
        await query.edit_message_text("منوی اصلی:", reply_markup=main_menu_keyboard())

    elif data == "download_link":
        context.user_data["state"] = "awaiting_url"
        await query.edit_message_text("🔗 لینک فایل را ارسال کنید:")

    elif data == "download_yt_video":
        context.user_data["state"] = "awaiting_yt_url_video"
        await query.edit_message_text(
            "🎬 لینک یوتیوب/اینستاگرام/TikTok... را بفرستید:"
        )

    elif data == "download_yt_audio":
        context.user_data["state"] = "awaiting_yt_url_audio"
        await query.edit_message_text(
            "🎵 لینک را بفرستید (صدا به MP3 تبدیل می‌شود):"
        )

    elif data == "show_stats":
        await query.edit_message_text(format_stats(), reply_markup=main_menu_keyboard())

    elif data == "network_test":
        await query.edit_message_text("📡 در حال اندازه‌گیری... (ممکن است 30 ثانیه طول بکشد)")
        result = await measure_network()
        await query.edit_message_text(result, reply_markup=main_menu_keyboard())

    # ==================== yt-dlp با کیفیت ====================
    elif data.startswith("yt_dl:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            await query.edit_message_text("خطا در پردازش.", reply_markup=main_menu_keyboard())
            return
        _, media_type, quality, url = parts
        cat = "music" if media_type == "audio" else "video"
        await process_download_request(
            update, context, url,
            edit_message=query.message,
            yt_category=cat, yt_quality=quality,
        )

    # ==================== مدیریت فایل‌ها ====================
    elif data == "files_menu":
        await query.edit_message_text("📁 مدیریت فایل‌ها:", reply_markup=files_menu_keyboard())

    elif data.startswith("files_cat:"):
        category = data.split(":", 1)[1]
        if category not in CATEGORIES:
            await query.edit_message_text("دسته‌بندی پیدا نشد.", reply_markup=files_menu_keyboard())
            return
        files = list_files_by_category(category)
        cat_label = CATEGORIES[category]["label"]
        if not files:
            await query.edit_message_text(
                f"{cat_label}: هیچ فایلی نیست.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data="files_menu")]]),
            )
        else:
            await query.edit_message_text(
                f"{cat_label} — {len(files)} فایل:",
                reply_markup=category_files_keyboard(category),
            )

    elif data.startswith("cleanup_cat:"):
        category = data.split(":", 1)[1]
        files = list_files_by_category(category)
        count = len(files)
        for f in files:
            try: f.unlink()
            except Exception: pass
        cat_label = CATEGORIES[category]["label"]
        await query.edit_message_text(
            f"✅ {count} فایل از {cat_label} حذف شد.",
            reply_markup=files_menu_keyboard(),
        )

    elif data == "cleanup_all":
        files = list_all_files()
        count = len(files)
        for f in files:
            try: f.unlink()
            except Exception: pass
        await query.edit_message_text(f"✅ {count} فایل حذف شد.", reply_markup=main_menu_keyboard())

    elif data == "send_all_files":
        files = list_all_files()
        if not files:
            await query.edit_message_text("هیچ فایلی موجود نیست.", reply_markup=files_menu_keyboard())
            return
        await query.edit_message_text(f"📦 ارسال {len(files)} فایل...")
        sent = 0
        skipped = 0
        for f in files:
            if f.stat().st_size > MAX_TELEGRAM_FILE_SIZE:
                skipped += 1
                continue
            try:
                with f.open("rb") as fp:
                    await query.message.reply_document(fp, filename=f.name)
                sent += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"خطا در ارسال {f.name}: {e}")
        summary = f"✅ {sent} فایل ارسال شد."
        if skipped:
            summary += f"\n⚠️ {skipped} فایل بزرگ‌تر از 50MB ارسال نشد."
        await query.message.reply_text(summary, reply_markup=files_menu_keyboard())

    elif data.startswith("send_file:"):
        filename = data.split(":", 1)[1]
        # جستجو در همه دسته‌ها
        file_path = None
        for cat in CATEGORIES.values():
            candidate = cat["dir"] / filename
            if candidate.exists():
                file_path = candidate
                break

        if not file_path:
            await query.edit_message_text("فایل پیدا نشد.", reply_markup=files_menu_keyboard())
            return

        if file_path.stat().st_size > MAX_TELEGRAM_FILE_SIZE:
            await query.edit_message_text(
                f"⚠️ حجم {file_path.stat().st_size//1024//1024}MB — بیشتر از حد تلگرام.",
                reply_markup=after_download_keyboard(file_path),
            )
            return

        try:
            with file_path.open("rb") as f:
                await query.message.reply_document(f, filename=filename)
        except Exception as e:
            await query.edit_message_text(f"خطا در ارسال: {e}", reply_markup=files_menu_keyboard())

    # ==================== کنترل دانلود ====================
    elif data == "cancel_download":
        task = active_downloads.get(user.id)
        if task and not task.done():
            task.cancel()
        else:
            await query.edit_message_text("دانلود فعالی وجود ندارد.", reply_markup=main_menu_keyboard())

    elif data == "retry_download":
        url = context.user_data.get("last_url")
        if not url:
            await query.edit_message_text("لینک قبلی پیدا نشد.", reply_markup=main_menu_keyboard())
            return
        await process_download_request(update, context, url, edit_message=query.message)

    # ==================== Google Drive ====================
    elif data.startswith("gdrive:"):
        filename = data.split(":", 1)[1]
        file_path = None
        for cat in CATEGORIES.values():
            candidate = cat["dir"] / filename
            if candidate.exists():
                file_path = candidate
                break

        if not file_path:
            await query.edit_message_text("فایل پیدا نشد.", reply_markup=main_menu_keyboard())
            return

        await query.edit_message_text(f"☁️ در حال آپلود به Google Drive...")
        link = await upload_to_gdrive(file_path)
        if link:
            await query.edit_message_text(f"✅ آپلود موفق!\n🔗 {link}", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("❌ آپلود ناموفق.", reply_markup=main_menu_keyboard())

    # ==================== صف دانلود ====================
    elif data == "queue_menu":
        queue = download_queue[user.id]
        if not queue:
            await query.edit_message_text(
                "📋 صف خالی است.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ اضافه کردن به صف", callback_data="add_to_queue")],
                    [InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")],
                ]),
            )
        else:
            await query.edit_message_text(
                f"📋 صف دانلود ({len(queue)}/{MAX_QUEUE_SIZE}):",
                reply_markup=queue_keyboard(queue),
            )

    elif data == "add_to_queue":
        context.user_data["state"] = "awaiting_queue_url"
        await query.edit_message_text(
            f"🔗 لینک را بفرستید ({len(download_queue[user.id])}/{MAX_QUEUE_SIZE}):"
        )

    elif data == "process_queue":
        queue = download_queue[user.id]
        if not queue:
            await query.edit_message_text("صف خالی است.", reply_markup=main_menu_keyboard())
            return
        if queue_processing[user.id]:
            await query.edit_message_text("⏳ صف در حال پردازش است.", reply_markup=main_menu_keyboard())
            return
        await query.edit_message_text(f"▶️ شروع پردازش {len(queue)} لینک...", reply_markup=main_menu_keyboard())
        asyncio.create_task(process_queue(user.id, context, query.message.chat_id))

    elif data == "clear_queue":
        download_queue[user.id].clear()
        await query.edit_message_text("✅ صف پاک شد.", reply_markup=main_menu_keyboard())

    elif data.startswith("remove_queue:"):
        idx = int(data.split(":", 1)[1])
        queue = download_queue[user.id]
        if 0 <= idx < len(queue):
            queue.pop(idx)
        await query.edit_message_text(
            f"📋 صف ({len(queue)}):",
            reply_markup=queue_keyboard(queue) if queue else main_menu_keyboard(),
        )


# ==================== پاک‌سازی خودکار ====================

async def auto_cleanup_job(app: Application) -> None:
    while True:
        try:
            removed = cleanup_old_files(AUTO_CLEANUP_HOURS)
            if removed:
                logger.info(f"{removed} فایل قدیمی حذف شد.")
        except Exception as e:
            logger.error(f"خطا در پاک‌سازی: {e}")
        await asyncio.sleep(3600)


async def on_startup(app: Application) -> None:
    asyncio.create_task(auto_cleanup_job(app))
    logger.info("ربات راه‌اندازی شد.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("API_KEY تنظیم نشده است.")

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(on_startup)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

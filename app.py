import sys, subprocess, importlib, shutil, sysconfig


def _run(cmd):
    try:
        return subprocess.call(cmd) == 0
    except Exception as _ex:
        print(f"[BOOT] {cmd[0]} failed: {_ex}", flush=True)
        return False


def _install(spec):
    """Install a package. Works even when this virtualenv has NO pip (like some bot hosts)."""
    py = sys.executable
    # 1) normal pip
    if _run([py, "-m", "pip", "install", "--upgrade", spec]):
        return True
    # 2) the venv has no pip -> bring it in with ensurepip, then retry
    print("[BOOT] pip is missing here - bootstrapping it with ensurepip", flush=True)
    _run([py, "-m", "ensurepip", "--upgrade"])
    if _run([py, "-m", "pip", "install", "--upgrade", spec]):
        return True
    # 3) uv, if the host has it
    uv = shutil.which("uv")
    if uv and _run([uv, "pip", "install", "--python", py, "--upgrade", spec]):
        return True
    # 4) any pip on the machine, installing straight into this environment
    target = sysconfig.get_paths()["purelib"]
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    for exe in ("pip3", "pip"):
        found = shutil.which(exe)
        if found and _run([found, "install", "--upgrade", "--target", target, "--only-binary=:all:",
                           "--python-version", ver, spec]):
            return True
    return False


for _imp, _pip in [("psutil", "psutil"), ("aiogram", "aiogram>=3.22.0"),
                   ("aiohttp", "aiohttp"), ("dotenv", "python-dotenv"),
                   ("requests", "requests")]:
    try:
        importlib.import_module(_imp)
    except ImportError:
        print(f"[BOOT] Installing missing package: {_pip}", flush=True)
        if _install(_pip):
            importlib.invalidate_caches()
        elif _imp == "psutil":
            print("[BOOT] psutil could not be installed - using the built-in fallback", flush=True)
        else:
            raise SystemExit(f"[BOOT] Could not install '{_pip}'. Install it from the host panel "
                             f"(pip install -r requirements.txt) and start again.")

import asyncio
import os
import re
import math
import time
import logging
import sqlite3
import hashlib
import zipfile
import shutil
import html
import ast
import importlib.util
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from logging.handlers import RotatingFileHandler
import signal
from collections import namedtuple

try:
    import psutil
except ImportError:
    class psutil:          # tiny Linux-only stand-in, used only when the real psutil cannot be installed
        class NoSuchProcess(Exception):
            pass

        class Process:
            def __init__(self, pid):
                if not os.path.exists(f"/proc/{pid}"):
                    raise psutil.NoSuchProcess()
                self.pid = pid

            @staticmethod
            def _ppids():
                out = {}
                for d in os.listdir("/proc"):
                    if d.isdigit():
                        try:
                            with open(f"/proc/{d}/stat") as f:
                                out[int(d)] = int(f.read().rsplit(")", 1)[1].split()[1])
                        except Exception:
                            pass
                return out

            def children(self, recursive=False):
                pp = self._ppids()
                found, todo = [], [self.pid]
                while todo:
                    cur = todo.pop()
                    for pid, parent in pp.items():
                        if parent == cur:
                            found.append(psutil.Process(pid))
                            if recursive:
                                todo.append(pid)
                return found

            def _sig(self, sig):
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    raise psutil.NoSuchProcess()

            def terminate(self):
                self._sig(signal.SIGTERM)

            def kill(self):
                self._sig(signal.SIGKILL)

            def is_running(self):
                try:
                    with open(f"/proc/{self.pid}/stat") as f:
                        return f.read().rsplit(")", 1)[1].split()[0] != "Z"     # a zombie is already dead
                except Exception:
                    return False

        @staticmethod
        def wait_procs(procs, timeout=None):
            end = time.time() + (timeout or 0)
            alive = list(procs)
            while alive and time.time() < end:
                alive = [p for p in alive if p.is_running()]
                if alive:
                    time.sleep(0.1)
            return [p for p in procs if p not in alive], alive

        @staticmethod
        def _cpu_times():
            with open("/proc/stat") as f:
                v = [int(x) for x in f.readline().split()[1:]]
            return sum(v), v[3] + (v[4] if len(v) > 4 else 0)

        @staticmethod
        def cpu_percent(interval=None):
            try:
                t1, i1 = psutil._cpu_times()
                time.sleep(interval or 0.2)
                t2, i2 = psutil._cpu_times()
                return round(100.0 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)
            except Exception:
                return 0.0

        @staticmethod
        def virtual_memory():
            info = {}
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        k, v = line.split(":")
                        info[k] = int(v.split()[0]) * 1024
            except Exception:
                pass
            total = info.get("MemTotal", 1)
            avail = info.get("MemAvailable", total)
            return namedtuple("vmem", "total available percent")(total, avail, round(100 * (total - avail) / total, 1))

        @staticmethod
        def disk_usage(path):
            u = shutil.disk_usage(path)
            return namedtuple("disk", "total used free percent")(u.total, u.used, u.free, round(100 * u.used / max(1, u.total), 1))
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile,
                           ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiohttp import web
from dotenv import load_dotenv

load_dotenv(override=True)      # the .env file wins over any old value set in the host panel

# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).parent.absolute()
UPLOAD_BOTS_DIR = BASE_DIR / 'upload_bots'
IROTECH_DIR = BASE_DIR / 'inf'
DATABASE_PATH = IROTECH_DIR / 'bot_data.db'
LOG_PATH = IROTECH_DIR / 'bot.log'

UPLOAD_BOTS_DIR.mkdir(exist_ok=True)
IROTECH_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
)
logger = logging.getLogger(__name__)

TOKEN = "8948083705:AAEEBtisFpAkPd68bv5nTguiHOSCczuEMJY"
OWNER_ID_STR = os.getenv('OWNER_ID')
ADMIN_ID_STR = os.getenv('ADMIN_ID')
YOUR_USERNAME = os.getenv('YOUR_USERNAME')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL')
# Optional: exact chat for the force-join check. Use this when UPDATE_CHANNEL is a private
# invite link (t.me/+xxxx). Value: @channelusername  OR  numeric id like -1001234567890
FORCE_JOIN_CHANNEL = os.getenv('FORCE_JOIN_CHANNEL')

if not TOKEN:
    logger.error("BOT_TOKEN not found in environment variables!")
    raise ValueError("BOT_TOKEN is required. Please set it in .env file or environment variables.")

if not OWNER_ID_STR or not ADMIN_ID_STR:
    logger.error("OWNER_ID or ADMIN_ID not found in environment variables!")
    raise ValueError("OWNER_ID and ADMIN_ID are required. Please set them in .env file.")

try:
    OWNER_ID = int(OWNER_ID_STR)
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    logger.error("OWNER_ID or ADMIN_ID must be valid integers!")
    raise

YOUR_USERNAME = (YOUR_USERNAME or '@OLD-STUDIO').strip()
UPDATE_CHANNEL = (UPDATE_CHANNEL or 'https://t.me/YourChannel').strip()
YOUTUBE_CHANNEL = 'https://www.youtube.com/@py_bot_hosting_update'


def _normalize_url(value: str) -> str:
    """Make any form (@name, t.me/name, https://t.me/name) a valid https url."""
    v = value.strip()
    if v.startswith('@'):
        return f"https://t.me/{v[1:]}"
    if v.startswith(('t.me/', 'telegram.me/')):
        return "https://" + v
    return v


UPDATE_CHANNEL_URL = _normalize_url(UPDATE_CHANNEL)
CONTACT_URL = f"https://t.me/{YOUR_USERNAME.replace('@', '')}"


def _resolve_force_join_chat():
    """Return chat id/username used by get_chat_member, or None if it cannot be worked out."""
    raw = (FORCE_JOIN_CHANNEL or '').strip()
    if raw:
        if raw.lstrip('-').isdigit():
            return int(raw)
        return raw if raw.startswith('@') else '@' + raw.rstrip('/').split('/')[-1]
    tail = UPDATE_CHANNEL_URL.rstrip('/').split('/')[-1]
    # private invite links (+xxxx / joinchat) cannot be checked by username
    if not tail or tail.startswith('+') or 'joinchat' in UPDATE_CHANNEL_URL:
        return None
    return '@' + tail.split('?')[0]


FORCE_JOIN_CHAT = _resolve_force_join_chat()
if FORCE_JOIN_CHAT is None:
    logger.info("No force-join channel in .env. Admins can add channels from Admin Panel -> Force-Join.")

# Bot-hosting limits (how many .py / .js bots a user can host at the same time)
FREE_USER_LIMIT_DEFAULT = 1                                 # normal users: 1 bot (the owner can change it in the bot)
try:
    PREMIUM_USER_LIMIT = max(2, int(os.getenv('PREMIUM_BOT_LIMIT') or 10))   # set PREMIUM_BOT_LIMIT in .env
except ValueError:
    PREMIUM_USER_LIMIT = 10
OWNER_LIMIT = float('inf')                                  # owner / admins: unlimited

MAX_TG_DOWNLOAD = 20 * 1024 * 1024          # Bot API: bots can download max 20 MB
MAX_ZIP_UNPACKED = 300 * 1024 * 1024        # zip-bomb protection
MAX_ZIP_FILES = 3000
PAGE_SIZE = 6

RUNNABLE = ('.py', '.js')
SUPPORT_EXT = {'.txt', '.json', '.env', '.db', '.csv', '.yml', '.yaml', '.ini', '.cfg',
               '.md', '.session', '.sqlite', '.sqlite3', '.toml'}

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                    link_preview_is_disabled=True))
dp = Dispatcher(storage=MemoryStorage())
BOT_START_TIME = datetime.now()

# ════════════════════════════════════════════════════════════════
#  IN-MEMORY STATE
# ════════════════════════════════════════════════════════════════
bot_scripts = {}
user_subscriptions = {}
user_files = {}
user_favorites = {}
banned_users = set()
active_users = set()
bot_settings = {}           # owner-controlled values (free-user bot limit …)
user_profiles = {}          # user_id -> (full name, username) – shown to admins in the Users list
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False
bot_stats = {'total_uploads': 0, 'total_downloads': 0, 'total_runs': 0}
pending_input = {}          # user_id -> action name waiting for the next text message
user_lang = {}              # user_id -> language code (missing = English)
fj_channels = []            # force-join channels: [{'id','chat','title','url'}]
fj_settings = {}            # force-join texts / colors / on-off
TR_CACHE = {}               # (lang, english text) -> translated text
_join_warned = False        # owner is told only once if the force-join check is broken

# ════════════════════════════════════════════════════════════════
#  BUTTON / KEYBOARD HELPERS  (colored buttons: primary=blue, success=green, danger=red)
#  Telegram clients that do not support colors simply show the normal button.
# ════════════════════════════════════════════════════════════════
BLUE, GREEN, RED = "primary", "success", "danger"


_RED_WORDS = ("delete", "stop", "cancel", "ban", "remove", "clean", "kill", "restart", "🔒", "wipe", "admin")
_GREEN_WORDS = ("run", "upload", "add", "premium", "joined", "join", "confirm", "yes", "start",
                "extract", "save", "contact", "open", "download", "unban", "unlock", "✅")


def auto_style(text):
    """Pick a color for a button that has none: red = danger, green = go/positive, blue = everything else."""
    t = (text or "").lower()
    if any(w in t for w in _RED_WORDS):
        return RED
    if any(w in t for w in _GREEN_WORDS):
        return GREEN
    return BLUE


def ib(text, data=None, url=None, style=None):
    style = style or auto_style(text)
    kw = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = data
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def rb(text, style=None):
    style = style or auto_style(text)
    kw = {"text": text}
    if style:
        kw["style"] = style
    return KeyboardButton(**kw)


BTN_UPLOAD = "📤 Upload File"
BTN_FILES = "📁 My Files"
BTN_FAVS = "⭐ Favorites"
BTN_SEARCH = "🔍 Search"
BTN_STATS = "📊 My Stats"
BTN_SPEED = "⚡ Bot Speed"
BTN_HELP = "ℹ️ Help"
BTN_LINKS = "📢 Updates"
BTN_ADMIN = "👑 Admin Panel"
BTN_LANG = "🌐 Language"
BTN_GITHUB = "🐙 Deploy GitHub"
MENU_TEXTS = {BTN_UPLOAD, BTN_FILES, BTN_FAVS, BTN_SEARCH, BTN_STATS, BTN_SPEED,
              BTN_HELP, BTN_LINKS, BTN_ADMIN, BTN_LANG, BTN_GITHUB}


def reply_menu(user_id):
    """The permanent keyboard shown where the normal phone keyboard is."""
    rows = [
        [rb(BTN_UPLOAD, GREEN), rb(BTN_FILES, BLUE)],
        [rb(BTN_FAVS), rb(BTN_SEARCH)],
        [rb(BTN_STATS), rb(BTN_SPEED)],
        [rb(BTN_GITHUB, BLUE), rb(BTN_LINKS, BLUE)],
        [rb(BTN_LANG, BLUE)],
    ]
    if user_id in admin_ids:
        rows.append([rb(BTN_ADMIN, RED)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="📤 Send a .py / .js / .zip file or pick an option…")


def home_kb(user_id):
    rows = [
        [ib("📤 Upload File", "nav:upload", style=GREEN), ib("📁 My Files", "nav:files:0", style=BLUE)],
        [ib("⭐ Favorites", "nav:favs"), ib("🔍 Search", "nav:search")],
        [ib("📢 Updates", url=UPDATE_CHANNEL_URL), ib("💬 Contact", url=CONTACT_URL)],
        [ib("🌐 Language", "nav:lang", style=BLUE)],
    ]
    if user_id in admin_ids:
        rows.append([ib("👑 Admin Panel", "adm:panel", style=RED)])
    return kb(*rows)


def back_home():
    return kb([ib("🏠 Home", "nav:home")])


HR = "━━━━━━━━━━━━━━━━━━"


def head(icon, title, sub=""):
    s = f"{icon} <b>{title}</b>\n"
    if sub:
        s += f"<i>{sub}</i>\n"
    return s + HR + "\n"


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_dur(seconds):
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def e(x):
    return html.escape(str(x))


# ════════════════════════════════════════════════════════════════
#  SAFE TELEGRAM CALLS  (no more crashes on "message is not modified", flood limits, etc.)
# ════════════════════════════════════════════════════════════════
async def safe_edit(msg, text, reply_markup=None):
    """Edit a message. Falls back to sending a new one if it cannot be edited."""
    for attempt in range(2):
        try:
            return await msg.edit_text(text, reply_markup=reply_markup)
        except TelegramRetryAfter as ex:
            await asyncio.sleep(min(ex.retry_after, 10))
        except TelegramBadRequest as ex:
            m = str(ex).lower()
            if "not modified" in m:
                return msg
            if "can't be edited" in m or "not found" in m or "can't edit" in m:
                break
            logger.warning(f"safe_edit bad request: {ex}")
            break
        except Exception as ex:
            logger.warning(f"safe_edit failed: {ex}")
            break
    try:
        return await msg.answer(text, reply_markup=reply_markup)
    except Exception as ex:
        logger.warning(f"safe_edit fallback send failed: {ex}")


async def cb_ok(cb, text=None, alert=False):
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════
def db_run(query, params=(), many=False):
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    try:
        c = conn.cursor()
        if many:
            c.executemany(query, params)
        else:
            c.execute(query, params)
        conn.commit()
        return c.fetchall()
    finally:
        conn.close()


def migrate_db():
    logger.info("Running database migrations...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute("PRAGMA table_info(user_files)")
        columns = [row[1] for row in c.fetchall()]
        if 'upload_date' not in columns:
            c.execute('ALTER TABLE user_files ADD COLUMN upload_date TEXT')
        c.execute("PRAGMA table_info(subscriptions)")
        if 'bot_limit' not in [row[1] for row in c.fetchall()]:
            c.execute('ALTER TABLE subscriptions ADD COLUMN bot_limit INTEGER')
        c.execute("PRAGMA table_info(subscriptions)")
        if 'expiry_done' not in [row[1] for row in c.fetchall()]:
            c.execute('ALTER TABLE subscriptions ADD COLUMN expiry_done INTEGER DEFAULT 0')
            # premiums that already ended before this update must not trigger "premium ended" notices
            c.execute('UPDATE subscriptions SET expiry_done = 1 WHERE expiry < ?', (datetime.now().isoformat(),))
        c.execute("PRAGMA table_info(active_users)")
        columns = [row[1] for row in c.fetchall()]
        if 'join_date' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN join_date TEXT')
        if 'last_active' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN last_active TEXT')
        if 'full_name' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN full_name TEXT')
        if 'username' not in columns:
            c.execute('ALTER TABLE active_users ADD COLUMN username TEXT')
        conn.commit()
        conn.close()
        logger.info("Database migrations completed successfully.")
    except Exception as ex:
        logger.error(f"Database migration error: {ex}", exc_info=True)


def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT, upload_date TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY, join_date TEXT, last_active TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_date TEXT, reason TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS favorites
                     (user_id INTEGER, file_name TEXT, PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_stats
                     (stat_name TEXT PRIMARY KEY, stat_value INTEGER)''')
        c.execute('''CREATE TABLE IF NOT EXISTS incidents
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,
                      file_name TEXT, kind TEXT, details TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS fj_channels
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE, title TEXT, url TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS fj_settings
                     (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_lang
                     (user_id INTEGER PRIMARY KEY, lang TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS tr_cache
                     (lang TEXT, src TEXT, dst TEXT, PRIMARY KEY (lang, src))''')
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        for stat in ['total_uploads', 'total_downloads', 'total_runs']:
            c.execute('INSERT OR IGNORE INTO bot_stats (stat_name, stat_value) VALUES (?, 0)', (stat,))
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as ex:
        logger.error(f"Database initialization error: {ex}", exc_info=True)


def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry, bot_limit, expiry_done FROM subscriptions')
        for user_id, expiry, bot_limit, done in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry), 'bot_limit': bot_limit,
                                               'done': bool(done)}
            except (ValueError, TypeError):
                logger.warning(f"Invalid expiry date for user {user_id}")
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            user_files.setdefault(user_id, []).append((file_name, file_type))
        c.execute('SELECT user_id, full_name, username FROM active_users')
        for user_id, full_name, username in c.fetchall():
            active_users.add(user_id)
            if full_name:
                user_profiles[user_id] = (full_name, username or "")
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())
        c.execute('SELECT user_id, file_name FROM favorites')
        for user_id, file_name in c.fetchall():
            user_favorites.setdefault(user_id, []).append(file_name)
        c.execute('SELECT stat_name, stat_value FROM bot_stats')
        for stat_name, stat_value in c.fetchall():
            bot_stats[stat_name] = stat_value
        c.execute('SELECT id, chat_id, title, url FROM fj_channels ORDER BY id')
        fj_channels[:] = [{'id': i, 'chat': ch, 'title': t, 'url': u} for i, ch, t, u in c.fetchall()]
        c.execute('SELECT key, value FROM bot_settings')
        bot_settings.update({k: v for k, v in c.fetchall()})
        c.execute('SELECT key, value FROM fj_settings')
        fj_settings.update({k: v for k, v in c.fetchall()})
        c.execute('SELECT user_id, lang FROM user_lang')
        user_lang.update({u: l for u, l in c.fetchall()})
        c.execute('SELECT lang, src, dst FROM tr_cache')
        for l, src, dst in c.fetchall():
            TR_CACHE[(l, src)] = dst
        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(banned_users)} banned, {len(admin_ids)} admins.")
    except Exception as ex:
        logger.error(f"Error loading data: {ex}", exc_info=True)


init_db()
migrate_db()
load_data()

# ════════════════════════════════════════════════════════════════
#  LANGUAGES  (🌐 Language button)
#  Every message / button the bot sends is translated into the user's chosen language.
#  Translations are cached in the database, so each text is translated only once.
# ════════════════════════════════════════════════════════════════
import contextvars
import json
import aiohttp
from aiogram.client.session.middlewares.base import BaseRequestMiddleware

# (code used by the translator, name shown to the user – always in English)
LANGUAGES = [
    ("en", "English"), ("ta", "Tamil"), ("hi", "Hindi"), ("te", "Telugu"), ("kn", "Kannada"),
    ("ml", "Malayalam"), ("bn", "Bengali"), ("mr", "Marathi"), ("gu", "Gujarati"), ("pa", "Punjabi"),
    ("ur", "Urdu"), ("or", "Odia"), ("as", "Assamese"), ("ne", "Nepali"), ("si", "Sinhala"),
    ("ar", "Arabic"), ("fa", "Persian"), ("iw", "Hebrew"), ("tr", "Turkish"), ("ps", "Pashto"),
    ("ru", "Russian"), ("uk", "Ukrainian"), ("pl", "Polish"), ("cs", "Czech"), ("sk", "Slovak"),
    ("ro", "Romanian"), ("hu", "Hungarian"), ("bg", "Bulgarian"), ("sr", "Serbian"), ("hr", "Croatian"),
    ("el", "Greek"), ("de", "German"), ("fr", "French"), ("es", "Spanish"), ("pt", "Portuguese"),
    ("it", "Italian"), ("nl", "Dutch"), ("sv", "Swedish"), ("no", "Norwegian"), ("da", "Danish"),
    ("fi", "Finnish"), ("is", "Icelandic"), ("ga", "Irish"), ("id", "Indonesian"), ("ms", "Malay"),
    ("tl", "Filipino"), ("vi", "Vietnamese"), ("th", "Thai"), ("my", "Burmese"), ("km", "Khmer"),
    ("lo", "Lao"), ("zh-CN", "Chinese (Simp.)"), ("zh-TW", "Chinese (Trad.)"), ("ja", "Japanese"),
    ("ko", "Korean"), ("mn", "Mongolian"), ("kk", "Kazakh"), ("uz", "Uzbek"), ("az", "Azerbaijani"),
    ("ka", "Georgian"), ("hy", "Armenian"), ("sw", "Swahili"), ("am", "Amharic"), ("ha", "Hausa"),
    ("yo", "Yoruba"), ("zu", "Zulu"), ("af", "Afrikaans"), ("so", "Somali"), ("ku", "Kurdish"),
]
LANG_NAME = dict(LANGUAGES)
NO_TR = "\u2063"                     # invisible marker: "do not translate this button text"
_cur_uid = contextvars.ContextVar("cur_uid", default=None)
REV = {(l, d): src for (l, src), d in TR_CACHE.items()}   # (lang, translated) -> english (for menu taps)
_http = None
_tr_sem = None
_PROV_DOWN = {}              # provider name -> do-not-try-before timestamp
_PROV_ERR = {}               # provider name -> last error text (shown by /langtest)
_tr_alert_ts = 0.0
_ms_token = {"v": None, "exp": 0.0}
_NUM_RE = re.compile(r'\d+(?:[.,:/]\d+)*')
_TAG_RE = re.compile(r'(<[^>]+>)')
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
MS_CODE = {"iw": "he", "zh-CN": "zh-Hans", "zh-TW": "zh-Hant", "tl": "fil", "no": "nb",
           "sr": "sr-Cyrl", "mn": "mn-Cyrl", "ku": "ckb"}
MM_CODE = {"iw": "he", "no": "nb"}


def lang_of(uid):
    return user_lang.get(uid, "en")


def _session():
    global _http, _tr_sem
    if _tr_sem is None:
        _tr_sem = asyncio.Semaphore(8)
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                      headers={"User-Agent": UA}, trust_env=True)
    return _http


# ── free translation providers (no API key). Tried in this order; the first one that works wins. ──
async def _ms_get_token():
    now = time.time()
    if _ms_token["v"] and _ms_token["exp"] > now:
        return _ms_token["v"]
    async with _session().get("https://edge.microsoft.com/translate/auth") as r:
        if r.status != 200:
            raise RuntimeError(f"token HTTP {r.status}")
        tok = (await r.text()).strip()
    if not tok:
        raise RuntimeError("empty token")
    _ms_token.update(v=tok, exp=now + 480)
    return tok


async def _p_microsoft(lang, texts):
    """Microsoft Edge translator – translates MANY texts in ONE request (fast, rarely rate-limited)."""
    code = MS_CODE.get(lang, lang)
    out, i = [], 0
    while i < len(texts):
        chunk, size = [], 0
        while i < len(texts) and len(chunk) < 40 and (not chunk or size + len(texts[i]) < 15000):
            chunk.append(texts[i][:4500])
            size += len(texts[i])
            i += 1
        data = None
        for attempt in range(2):
            tok = await _ms_get_token()
            async with _tr_sem:
                async with _session().post(
                        "https://api-edge.cognitive.microsofttranslator.com/translate",
                        params={"from": "en", "to": code, "api-version": "3.0"},
                        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
                        data=json.dumps([{"Text": t} for t in chunk])) as r:
                    if r.status == 401 and attempt == 0:
                        _ms_token["v"] = None
                        continue
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    data = await r.json(content_type=None)
            break
        if not data or len(data) != len(chunk):
            raise RuntimeError("bad response")
        out.extend(d["translations"][0]["text"] for d in data)
    return out


async def _g_one(text, lang):
    last = None
    for url in ("https://translate.googleapis.com/translate_a/single",
                "https://translate.google.com/translate_a/single"):
        try:
            params = {"client": "gtx", "sl": "en", "tl": lang, "dt": "t"}
            async with _tr_sem:
                if len(text) <= 1500:
                    params["q"] = text
                    resp = _session().get(url, params=params)
                else:
                    resp = _session().post(url, params=params, data={"q": text})
                async with resp as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    data = await r.json(content_type=None)
            res = "".join(part[0] for part in data[0] if part and part[0])
            if res.strip():
                return res
            raise RuntimeError("empty answer")
        except Exception as ex:
            last = ex
    raise last


async def _p_google(lang, texts):
    res = await asyncio.gather(*(_g_one(t, lang) for t in texts), return_exceptions=True)
    out = [r if isinstance(r, str) else None for r in res]
    if not any(out):
        raise next((r for r in res if isinstance(r, Exception)), RuntimeError("no answer"))
    return out


async def _mm_one(text, lang):
    if len(text) > 480:
        raise RuntimeError("text too long for MyMemory")
    async with _tr_sem:
        async with _session().get("https://api.mymemory.translated.net/get",
                                  params={"q": text, "langpair": f"en|{MM_CODE.get(lang, lang)}"}) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            data = await r.json(content_type=None)
    t = (data.get("responseData") or {}).get("translatedText") or ""
    if not t.strip() or "MYMEMORY WARNING" in t.upper() or str(data.get("responseStatus")) != "200":
        raise RuntimeError("MyMemory refused (quota?)")
    return html.unescape(t)


async def _p_mymemory(lang, texts):
    res = await asyncio.gather(*(_mm_one(t, lang) for t in texts), return_exceptions=True)
    out = [r if isinstance(r, str) else None for r in res]
    if not any(out):
        raise next((r for r in res if isinstance(r, Exception)), RuntimeError("no answer"))
    return out


PROVIDERS = [("microsoft", _p_microsoft), ("google", _p_google), ("mymemory", _p_mymemory)]


async def _tell_owner_tr_down():
    global _tr_alert_ts
    if time.time() - _tr_alert_ts < 3600:
        return
    _tr_alert_ts = time.time()
    errs = "\n".join(f"• {n}: <code>{e(str(v)[:120])}</code>" for n, v in _PROV_ERR.items())
    try:
        await bot.send_message(OWNER_ID, "⚠️ <b>Language translation is not working</b>\n\n"
                               "Every free translator failed from this server:\n" + errs +
                               "\n\nUsers will see English until it is fixed. "
                               "Check the server's internet access, then run /langtest.")
    except Exception:
        pass


async def translate_batch(lang, texts):
    """Translate a list of English strings. Returns a list (None where it failed)."""
    for name, fn in PROVIDERS:
        if time.time() < _PROV_DOWN.get(name, 0):
            continue
        try:
            outs = await fn(lang, texts)
            if outs and len(outs) == len(texts) and any(outs):
                _PROV_ERR.pop(name, None)
                return outs
            raise RuntimeError("empty result")
        except Exception as ex:
            _PROV_DOWN[name] = time.time() + 20
            _PROV_ERR[name] = f"{type(ex).__name__}: {ex}"
            logger.warning(f"translator '{name}' failed ({lang}): {ex}")
    asyncio.create_task(_tell_owner_tr_down())
    return [None] * len(texts)


def _remember(lang, src, dst):
    TR_CACHE[(lang, src)] = dst
    REV[(lang, dst)] = src
    try:
        db_run('INSERT OR REPLACE INTO tr_cache (lang, src, dst) VALUES (?, ?, ?)', (lang, src, dst))
    except Exception as ex:
        logger.warning(f"tr_cache save failed: {ex}")


def _mask_nums(s):
    nums = _NUM_RE.findall(s)
    if not nums:
        return s, nums
    counter = iter(range(1000))
    return _NUM_RE.sub(lambda m: "{%d}" % next(counter), s), nums


def _fill(t, nums):
    for k, n in enumerate(nums):
        t = t.replace("{%d}" % k, n)
    return t


def _ph_ok(t, nums):
    return all("{%d}" % k in t for k in range(len(nums)))


async def tr_many(lang, items):
    """Translate many strings with ONE provider request. Never raises – failed items stay English."""
    result = list(items)
    if lang == "en":
        return result
    todo = {}                                   # masked text -> [(index, original, numbers)]
    for i, s in enumerate(items):
        if not s or not any(ch.isalpha() for ch in s):
            continue
        hit = TR_CACHE.get((lang, s))
        if hit is not None:
            result[i] = hit
            continue
        masked, nums = _mask_nums(s)
        if nums:
            c = TR_CACHE.get((lang, masked))
            if c is not None and _ph_ok(c, nums):
                result[i] = _fill(c, nums)
                continue
        todo.setdefault(masked, []).append((i, s, nums))
    if not todo:
        return result
    keys = list(todo)
    outs = await translate_batch(lang, keys)
    raw = []                                    # texts whose number placeholders got damaged
    for key, out in zip(keys, outs):
        if out is None:
            continue
        for i, s, nums in todo[key]:
            if nums and not _ph_ok(out, nums):
                raw.append((i, s))
                continue
            _remember(lang, key if nums else s, out)
            result[i] = _fill(out, nums) if nums else out
    if raw:
        for (i, s), out in zip(raw, await translate_batch(lang, [s for _, s in raw])):
            if out:
                result[i] = out
    return result


async def tr_plain(lang, s):
    return (await tr_many(lang, [s]))[0]


async def tr_html(lang, text):
    """Translate Telegram-HTML text: tags stay untouched, <code>/<pre> content is never translated."""
    parts = _TAG_RE.split(text)
    out = list(parts)
    skip = 0
    idx = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            m = re.match(r'</?\s*(code|pre|a)\b', part.lower())
            if m:
                skip = max(0, skip - 1) if part.startswith("</") else skip + 1
            continue
        if skip or not part.strip():
            continue
        idx.append(i)
    if not idx:
        return text
    res = await tr_many(lang, [html.unescape(parts[i].strip()) for i in idx])
    for i, t in zip(idx, res):
        part = parts[i]
        lead = part[:len(part) - len(part.lstrip())]
        trail = part[len(part.rstrip()):]
        out[i] = lead + html.escape(t, quote=False) + trail
    return "".join(out)


async def tr_markup(lang, mk):
    """Keep ALL button labels in English.

    User-selected language may translate bot messages, but keyboard/inline
    button text is intentionally never translated. This keeps navigation
    labels stable and always in the default English format.
    """
    return mk


async def screen_langtest(code="ta"):
    lines = []
    for name, fn in PROVIDERS:
        t0 = time.perf_counter()
        try:
            out = await asyncio.wait_for(fn(code, ["Hello, how are you?"]), 15)
            lines.append(f"✅ <b>{name}</b> · {(time.perf_counter() - t0) * 1000:.0f} ms\n    <code>{e(out[0])}</code>")
        except Exception as ex:
            lines.append(f"❌ <b>{name}</b>\n    <code>{e(type(ex).__name__)}: {e(str(ex)[:150])}</code>")
    text = (head("🌐", "Translator test", f"English → {LANG_NAME.get(code, code)}") + "\n".join(lines) +
            "\n\nAt least one ✅ means languages work. All ❌ = the server cannot reach the internet / these sites.")
    return text, kb([ib("🔄 Test again", "adm:langtest", style=BLUE)], [ib("👑 Admin Panel", "adm:panel")])


class TrRequestMiddleware(BaseRequestMiddleware):
    """Translates every outgoing text / caption / button into the receiving user's language."""
    async def __call__(self, make_request, bot, method):
        try:
            cid = getattr(method, "chat_id", None)
            uid = cid if isinstance(cid, int) and cid > 0 else _cur_uid.get()
            lang = lang_of(uid) if uid else "en"
            if lang != "en":
                jobs = []
                for field in ("text", "caption"):
                    v = getattr(method, field, None)
                    if isinstance(v, str) and v:
                        jobs.append((field, tr_html(lang, v)))
                mk = getattr(method, "reply_markup", None)
                if mk is not None:
                    jobs.append(("reply_markup", tr_markup(lang, mk)))
                if jobs:
                    done = await asyncio.gather(*(c for _, c in jobs))
                    for (field, _), val in zip(jobs, done):
                        setattr(method, field, val)
        except Exception as ex:
            logger.warning(f"translation skipped: {ex}")
        return await make_request(bot, method)


class LangMiddleware(BaseMiddleware):
    """Remembers who is talking, and maps a tapped translated menu button back to its English name."""
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            _cur_uid.set(user.id)
            lang = lang_of(user.id)
            if (lang != "en" and isinstance(event, types.Message) and event.text
                    and event.text not in MENU_TEXTS):
                src = REV.get((lang, event.text))
                if src in MENU_TEXTS:
                    object.__setattr__(event, "text", src)
        return await handler(event, data)


bot.session.middleware(TrRequestMiddleware())
dp.message.outer_middleware(LangMiddleware())
dp.callback_query.outer_middleware(LangMiddleware())


def set_user_lang(uid, code):
    user_lang[uid] = code
    try:
        db_run('INSERT OR REPLACE INTO user_lang (user_id, lang) VALUES (?, ?)', (uid, code))
    except Exception as ex:
        logger.error(f"save language failed: {ex}")


def screen_lang(uid):
    cur = lang_of(uid)
    btns = []
    for code, name in LANGUAGES:
        on = code == cur
        btns.append(ib(NO_TR + ("✅ " if on else "") + name, f"lang:{code}", style=GREEN if on else BLUE))
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([ib("🏠 Home", "nav:home", style=BLUE)])
    text = (head("🌐", "Language", "Choose your language") +
            f"Current: <b>{LANG_NAME.get(cur, 'English')}</b>\n\n"
            "Tap a language below – the whole bot will switch to it 👇")
    return text, kb(*rows)



def bump_stat(name):
    bot_stats[name] = bot_stats.get(name, 0) + 1
    try:
        db_run('UPDATE bot_stats SET stat_value = stat_value + 1 WHERE stat_name = ?', (name,))
    except Exception as ex:
        logger.error(f"bump_stat error: {ex}")


def register_user(user_id):
    if user_id in active_users:
        return
    active_users.add(user_id)
    try:
        now = datetime.now().isoformat()
        db_run('INSERT OR IGNORE INTO active_users (user_id, join_date, last_active) VALUES (?, ?, ?)',
               (user_id, now, now))
    except Exception as ex:
        logger.error(f"Error saving active user: {ex}")


def remember_profile(user):
    new = (user.full_name or "", user.username or "")
    if user_profiles.get(user.id) == new:
        return
    user_profiles[user.id] = new
    try:
        db_run('UPDATE active_users SET full_name = ?, username = ? WHERE user_id = ?', (new[0], new[1], user.id))
    except Exception as ex:
        logger.error(f"remember_profile error: {ex}")


def role_label(user_id):
    if user_id == OWNER_ID:
        return "👑 Owner"
    if user_id in admin_ids:
        return "👑 Admin"
    if is_premium(user_id):
        return "💎 Premium"
    return "🆓 Free"


def is_premium(user_id):
    d = user_subscriptions.get(user_id)
    return bool(d and d['expiry'] > datetime.now())


def free_limit():
    """How many bots a normal (free) user may host and run. Only the owner can change it."""
    try:
        return max(1, int(bot_settings.get("free_limit", FREE_USER_LIMIT_DEFAULT)))
    except (TypeError, ValueError):
        return FREE_USER_LIMIT_DEFAULT


def set_free_limit(n):
    bot_settings["free_limit"] = str(int(n))
    db_run('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ("free_limit", str(int(n))))


def github_deploy_enabled():
    return bot_settings.get("github_deploy", "0") == "1"


def set_github_deploy(enabled):
    value = "1" if enabled else "0"
    bot_settings["github_deploy"] = value
    db_run('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ("github_deploy", value))


def github_deploy_premium_only():
    # Default: Premium users only. Owner can switch this to everyone.
    return bot_settings.get("github_deploy_access", "premium") != "all"


def set_github_deploy_access(premium_only):
    value = "premium" if premium_only else "all"
    bot_settings["github_deploy_access"] = value
    db_run('INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)', ("github_deploy_access", value))


OWNER_ONLY_SUBS = {"premium", "join", "fjdel", "fjmediaoff", "fjtog", "fjcol", "fjc", "fjprev", "fjreset"}
OWNER_ONLY_ACTIONS = {"addadmin", "rmadmin", "addpremium", "setlimit", "freelimit",
                      "fj_add", "fj_text", "fj_media", "fj_lbl_join", "fj_lbl_check"}


def owner_only_action(action):
    return action in OWNER_ONLY_ACTIONS or action.startswith("premlimit:")


def get_user_file_limit(user_id):
    """How many bots (.py / .js) this user may host: admins unlimited, premium = PREMIUM_USER_LIMIT, free = 1."""
    if user_id in admin_ids or user_id == OWNER_ID:
        return OWNER_LIMIT
    if is_premium(user_id):
        return user_subscriptions[user_id].get('bot_limit') or PREMIUM_USER_LIMIT   # set by admin when adding premium
    return free_limit()


def bot_count(user_id):
    return sum(1 for _, ft in user_files.get(user_id, []) if ft in ('py', 'js'))


def limit_text(user_id):
    lim = get_user_file_limit(user_id)
    return "Unlimited ♾️" if lim == float('inf') else str(int(lim))


pending_upload = {}          # user_id -> the upload message waiting for "Replace"


def limit_reached_screen(user_id):
    lim = int(get_user_file_limit(user_id))
    bots = [n for n, ft in user_files.get(user_id, []) if ft in ('py', 'js')]
    if is_premium(user_id):
        head_txt = head("🚫", "Bot Limit Reached", "This is your limit") + \
            f"💎 Your premium plan allows <b>{lim}</b> hosted bots, and you are using all <b>{lim}</b>.\n\n"
    elif user_id in user_subscriptions:          # premium that has already ended
        d = user_subscriptions[user_id]
        head_txt = head("🚫", "Upload Not Allowed", "Your premium has ended") + \
            f"⌛ Your premium expired on <b>{d['expiry'].strftime('%d %b %Y')}</b>, so you are on the free plan.\n" \
            f"🆓 Free users can host only <b>{lim}</b> bot, and you already have {len(bots)}.\n\n" \
            f"💎 Renew premium to host up to <b>{d.get('bot_limit') or PREMIUM_USER_LIMIT}</b> bots again.\n\n"
    else:
        head_txt = head("🚫", "Bot Limit Reached", "Free plan limit") + \
            f"🆓 Free users can host only <b>{lim}</b> bot.\n" \
            "💎 Premium users can host more bots.\n\n"
    listing = "\n".join(f"{i}. {'🟢' if is_running(user_id, n) else '⚪'} <code>{e(n)}</code>"
                        for i, n in enumerate(bots[:6], 1))
    text = (head_txt + "<b>Your bots</b>\n" + listing +
            "\n\n🔁 Tap <b>Replace</b> to delete (and stop) that bot and use the new file you just sent instead.")
    rows = [[ib(f"🔁 Replace {i}", f"lim:rep:{fid_of(n)}", style=GREEN)] for i, n in enumerate(bots[:6], 1)]
    if not is_premium(user_id):
        rows.append([ib("💎 Get Premium", "nav:premium", style=BLUE)])
    rows.append([ib("❌ Cancel", "lim:cancel", style=RED)])
    return text, kb(*rows)


# ════════════════════════════════════════════════════════════════
#  FILE HELPERS
# ════════════════════════════════════════════════════════════════
def fid_of(name):
    return hashlib.md5(name.encode('utf-8')).hexdigest()[:8]


def user_dir(user_id):
    d = UPLOAD_BOTS_DIR / str(user_id)
    d.mkdir(exist_ok=True)
    return d


def find_file(user_id, fid):
    for name, ftype in user_files.get(user_id, []):
        if fid_of(name) == fid:
            return name, ftype
    return None


def icon_for(ftype):
    return "🐍" if ftype == "py" else "🟨" if ftype == "js" else "📦"


def script_key_of(user_id, name):
    return f"{user_id}_{name}"


def is_running(user_id, name):
    _reap_dead_scripts()
    return script_key_of(user_id, name) in bot_scripts


def sanitize_name(name):
    name = os.path.basename((name or "file").replace("\\", "/"))
    # keep every language (Tamil, Hindi…); only replace characters that are unsafe in file names
    name = re.sub(r'[\x00-\x1f\s\\/:*?"<>|\'`$&;()\[\]{}!#%^+=,~@]+', '_', name).strip('._') or "file"
    stem, ext = os.path.splitext(name)
    if len(stem) > 60:
        stem = stem[:60]
    return stem + ext.lower()


def prune_missing(user_id):
    """Forget files that no longer exist on disk (e.g. server was reset)."""
    folder = UPLOAD_BOTS_DIR / str(user_id)
    keep, gone = [], []
    for name, ftype in user_files.get(user_id, []):
        (keep if (folder / name).exists() else gone).append((name, ftype))
    if gone:
        user_files[user_id] = keep
        for name, _ in gone:
            try:
                db_run('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, name))
                db_run('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (user_id, name))
            except Exception:
                pass
            if name in user_favorites.get(user_id, []):
                user_favorites[user_id].remove(name)


# ════════════════════════════════════════════════════════════════
#  FORCE-JOIN  (channel membership gate)
# ════════════════════════════════════════════════════════════════
_SUB_CACHE = {}              # (user_id, chat) -> (is_member, expiry_ts)
_JOIN_PROBLEMS = {}          # chat -> last configuration problem (shown in Admin -> Force-Join)
_join_warned_chats = set()   # owner is told only once per broken channel
_CONFIG_ERRORS = ("chat not found", "member list is inaccessible", "not enough rights",
                  "forbidden", "bot is not a member", "channel_private", "chat_admin_required",
                  "bot was kicked", "need administrator")
COLOR_NAMES = {BLUE: "🔵 Blue", GREEN: "🟢 Green", RED: "🔴 Red"}
FJ_MAX_CHANNELS = 8
JOIN_RECHECK_SECONDS = 3       # membership is re-checked on (almost) every message / button tap, so leaving the channel is noticed at once
DEFAULT_LBL_JOIN = "📢 Join Channel"
DEFAULT_LBL_CHECK = "✅ I've Joined"


def fj_get(key, default=""):
    return fj_settings.get(key, default)


def fj_put(key, val):
    fj_settings[key] = val
    db_run('INSERT OR REPLACE INTO fj_settings (key, value) VALUES (?, ?)', (key, val))


def fj_del(key):
    fj_settings.pop(key, None)
    db_run('DELETE FROM fj_settings WHERE key = ?', (key,))


def fj_color(which):
    default = BLUE if which == "join" else GREEN
    c = fj_get("color_" + which, default)
    return c if c in COLOR_NAMES else default


def fj_enabled():
    return fj_get("on", "1") == "1" and bool(fj_channels)


def _chat_ref(c):
    raw = str(c['chat'])
    return int(raw) if raw.lstrip('-').isdigit() else raw


def fj_add_channel(chat, title, url):
    db_run('INSERT OR REPLACE INTO fj_channels (chat_id, title, url) VALUES (?, ?, ?)', (str(chat), title, url))
    rid = db_run('SELECT id FROM fj_channels WHERE chat_id = ?', (str(chat),))[0][0]
    fj_channels[:] = [c for c in fj_channels if c['chat'] != str(chat)] + \
        [{'id': rid, 'chat': str(chat), 'title': title, 'url': url}]
    _SUB_CACHE.clear()


def fj_remove_channel(rid):
    db_run('DELETE FROM fj_channels WHERE id = ?', (rid,))
    fj_channels[:] = [c for c in fj_channels if c['id'] != rid]
    _SUB_CACHE.clear()


def _seed_fj_from_env():
    """First start only: carry the old .env channel over, afterwards admins manage channels in the bot."""
    if fj_get("seeded") == "1":
        return
    fj_put("seeded", "1")
    if FORCE_JOIN_CHAT is not None and not fj_channels:
        fj_add_channel(str(FORCE_JOIN_CHAT), str(FORCE_JOIN_CHAT).lstrip('@'), UPDATE_CHANNEL_URL)


_seed_fj_from_env()


async def _tell_owner_join_problem(chat, err):
    if chat in _join_warned_chats:
        return
    _join_warned_chats.add(chat)
    try:
        await bot.send_message(
            OWNER_ID,
            "⚠️ <b>Force-Join is NOT working</b>\n\n"
            f"Channel checked: <code>{e(chat)}</code>\n"
            f"Telegram says: <code>{e(err)}</code>\n\n"
            "✅ Fix: add this bot as <b>Admin</b> in that channel, or remove the channel.\n"
            "Until fixed, users are NOT blocked by it. Check: 👑 Admin Panel → 🔗 Force-Join."
        )
    except Exception:
        pass


async def _is_member(user_id, c, force=False):
    key = (user_id, c['chat'])
    now = time.time()
    cached = _SUB_CACHE.get(key)
    if cached and cached[1] > now and not force:
        return cached[0]
    try:
        member = await asyncio.wait_for(bot.get_chat_member(chat_id=_chat_ref(c), user_id=user_id), timeout=10)
        status = str(getattr(member.status, "value", member.status))
        ok = status in ("creator", "administrator", "member") or \
            (status == "restricted" and bool(getattr(member, "is_member", False)))
        _JOIN_PROBLEMS.pop(c['chat'], None)
        _SUB_CACHE[key] = (ok, now + JOIN_RECHECK_SECONDS)
        return ok
    except Exception as ex:
        msg = str(ex).lower()
        if any(k in msg for k in _CONFIG_ERRORS):
            _JOIN_PROBLEMS[c['chat']] = str(ex)
            logger.warning(f"Force-join misconfigured ({c['chat']}): {ex}")
            asyncio.create_task(_tell_owner_join_problem(c['chat'], str(ex)))
            return True      # config problem is ours, don't lock every user out
        if "user not found" in msg or "participant_id_invalid" in msg:
            return False
        logger.warning(f"Subscription check failed (network?): {ex}")
        if cached:
            return cached[0]
        return True


async def missing_channels(user_id, force=False):
    """Channels the user still has to join (empty list = allowed in)."""
    if not fj_enabled():
        return []
    chans = list(fj_channels)
    res = await asyncio.gather(*(_is_member(user_id, c, force) for c in chans))
    return [c for c, ok in zip(chans, res) if not ok]


async def is_subscribed(user_id: int, force: bool = False) -> bool:
    return not await missing_channels(user_id, force)


def join_kb(chans=None):
    chans = fj_channels if chans is None else chans
    col_join, col_check = fj_color("join"), fj_color("check")
    label = fj_get("lbl_join", DEFAULT_LBL_JOIN)[:40]
    rows = []
    for c in chans:
        text = label if len(chans) == 1 else NO_TR + "📢 " + (c['title'] or "Channel")[:30]
        rows.append([ib(text, url=c['url'], style=col_join)])
    rows.append([ib(fj_get("lbl_check", DEFAULT_LBL_CHECK)[:40], "join:check", style=col_check)])
    return kb(*rows)


def media_kind(url):
    path = url.split("?")[0].lower()
    if path.endswith((".gif", ".gifv")) or "giphy.com/media" in url or "tenor.com" in url and path.endswith(".gif"):
        return "animation"
    if path.endswith((".mp4", ".mov", ".webm", ".m4v", ".mkv")):
        return "video"
    return "photo"


async def _send_media(chat_id, url, caption=None, markup=None):
    kind = media_kind(url)
    if kind == "animation":
        return await bot.send_animation(chat_id, animation=url, caption=caption, reply_markup=markup)
    if kind == "video":
        return await bot.send_video(chat_id, video=url, caption=caption, reply_markup=markup)
    return await bot.send_photo(chat_id, photo=url, caption=caption, reply_markup=markup)


async def send_lock(chat_id, chans=None):
    """The 'join first' screen. With the media switch ON: picture / GIF / video on top, text and buttons below it."""
    text, mk = lock_text(), join_kb(chans)
    url = fj_get("media_url") if fj_get("media_on") == "1" else ""
    if url:
        try:
            await _send_media(chat_id, url, caption=text, markup=mk)
            return
        except Exception as ex:
            logger.warning(f"force-join media failed: {ex}")
            if "caption" in str(ex).lower():            # text too long for a caption: media first, text below it
                try:
                    await _send_media(chat_id, url)
                except Exception:
                    pass
    await bot.send_message(chat_id, text, reply_markup=mk)


async def edit_lock(message, chans):
    mk = join_kb(chans)
    if message.photo or message.animation or message.video:
        try:
            await message.edit_caption(caption=lock_text(), reply_markup=mk)
        except TelegramBadRequest as ex:
            if "not modified" not in str(ex).lower():
                logger.warning(f"edit_lock failed: {ex}")
    else:
        await safe_edit(message, lock_text(), mk)


def default_lock_text():
    a = re.sub(r'^[^\w]+', '', fj_get("lbl_join", DEFAULT_LBL_JOIN)) or "Join"
    b = re.sub(r'^[^\w]+', '', fj_get("lbl_check", DEFAULT_LBL_CHECK)) or "Joined"
    return (head("🔒", "Access Locked", "One quick step to continue") +
            "To use this bot, please join our channel first 👇\n\n"
            f"1️⃣ Tap <b>{e(a)}</b>\n"
            f"2️⃣ Come back and tap <b>{e(b)}</b>")


def lock_text():
    custom = fj_get("text")
    return custom if custom.strip() else default_lock_text()


async def _deny(event, text):
    try:
        if isinstance(event, types.CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer(text)
    except Exception:
        pass


class GateMiddleware(BaseMiddleware):
    """ban check -> maintenance lock -> force-join, for every message and button tap"""
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user or user.is_bot:
            return await handler(event, data)
        uid = user.id
        _cur_uid.set(uid)
        if uid in banned_users and uid != OWNER_ID:
            await _deny(event, "🚫 You are banned from using this bot.\nContact the owner for help.")
            return
        register_user(uid)
        remember_profile(user)
        if uid not in admin_ids:
            if bot_locked:
                await _deny(event, "🔒 Bot is under maintenance. Please try again later.")
                return
            is_join_cb = isinstance(event, types.CallbackQuery) and event.data == "join:check"
            miss = [] if is_join_cb else await missing_channels(uid)
            if miss:
                if isinstance(event, types.Message):
                    await send_lock(event.chat.id, miss)
                elif isinstance(event, types.CallbackQuery):
                    await event.answer("🔒 Join the channel first!", show_alert=True)
                    await send_lock(uid, miss)
                return
        return await handler(event, data)


dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


# ════════════════════════════════════════════════════════════════
#  ANIMATED "SERVER LOADING" MESSAGE
# ════════════════════════════════════════════════════════════════
class LiveStatus:
    """Premium animated loader: stage checklist + smooth progress bar + percent + timer.
    The message edits itself every ~2s while a slow task runs."""
    SPIN = ["◜", "◝", "◞", "◟"]
    SPARK = ["✦", "✧", "⋆", "✧"]
    DEFAULT_STAGES = ["Connecting to server", "Preparing environment", "Working on it", "Almost ready"]

    def __init__(self, target, title, details="", new=True, delete_on_exit=True, interval=2.0,
                 stages=None, icon="🚀"):
        self.target, self.title, self.details = target, title, details
        self.new, self.delete_on_exit, self.interval = new, delete_on_exit, interval
        self.stages = stages or self.DEFAULT_STAGES
        self.icon = icon
        self.msg = None
        self._task = None
        self._start = 0.0

    def _percent(self, elapsed):
        # fast at first, slows down, never claims 100% before the task is really done
        return min(96, int(96 * (1 - math.exp(-elapsed / 14.0))))

    def _render(self, tick):
        elapsed = asyncio.get_event_loop().time() - self._start
        pct = self._percent(elapsed)
        n = len(self.stages)
        cur = min(n - 1, int(pct / (100 / n)))
        spin = self.SPIN[tick % len(self.SPIN)]
        spark = self.SPARK[tick % len(self.SPARK)]
        filled = round(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        lines = [f"{self.icon} <b>{self.title}</b>  {spark}", HR]
        if self.details:
            lines.append(self.details)
            lines.append("")
        for i, st in enumerate(self.stages):
            if i < cur:
                lines.append(f"✅ <s>{st}</s>")
            elif i == cur:
                lines.append(f"{spin} <b>{st}…</b>")
            else:
                lines.append(f"▫️ <i>{st}</i>")
        lines.append("")
        lines.append(f"<code>[{bar}] {pct:>3d}%</code>")
        lines.append(f"⏱ <code>{int(elapsed)}s</code> · please keep this chat open")
        return "\n".join(lines)

    async def _loop(self):
        tick = 0
        while True:
            await asyncio.sleep(self.interval)
            tick += 1
            try:
                await self.msg.edit_text(self._render(tick))
            except TelegramRetryAfter as ex:
                await asyncio.sleep(min(ex.retry_after, 10))
            except Exception:
                pass

    async def __aenter__(self):
        self._start = asyncio.get_event_loop().time()
        try:
            if self.new:
                self.msg = await self.target.answer(self._render(0))
            else:
                self.msg = self.target
                await self.msg.edit_text(self._render(0))
        except Exception as ex:
            logger.warning(f"LiveStatus start failed: {ex}")
            self.msg = self.msg or self.target
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if exc_type is None and self.msg:
            # quick "100% done" flash so it feels finished
            try:
                done = [f"{self.icon} <b>{self.title}</b>  ✨", HR]
                if self.details:
                    done += [self.details, ""]
                done += [f"✅ <s>{st}</s>" for st in self.stages]
                done += ["", "<code>[██████████] 100%</code>", "🎉 <b>Done!</b>"]
                await self.msg.edit_text("\n".join(done))
                await asyncio.sleep(0.6)
            except Exception:
                pass
        if self.delete_on_exit and self.new and self.msg:
            try:
                await self.msg.delete()
            except Exception:
                pass
        return False


# ════════════════════════════════════════════════════════════════
#  SCRIPT RUNNER HELPERS
# ════════════════════════════════════════════════════════════════
IMPORT_TO_PIP = {
    "telebot": "pyTelegramBotAPI", "PIL": "pillow", "bs4": "beautifulsoup4",
    "cv2": "opencv-python-headless", "yaml": "pyyaml", "dotenv": "python-dotenv",
    "telegram": "python-telegram-bot", "sklearn": "scikit-learn", "Crypto": "pycryptodome",
    "dateutil": "python-dateutil", "serial": "pyserial", "jwt": "pyjwt", "fitz": "pymupdf",
    "google": "google-api-python-client", "discord": "discord.py", "pyrogram": "pyrogram tgcrypto",
    "telethon": "telethon", "flask_cors": "flask-cors", "socks": "pysocks", "lxml": "lxml",
    "firebase_admin": "firebase-admin", "yt_dlp": "yt-dlp", "speech_recognition": "SpeechRecognition",
}


def _find_missing_packages(file_path: Path, folder: Path):
    """Read the script's imports and return pip packages that are not installed yet."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    local = {x.stem for x in folder.glob("*.py")} | {x.name for x in folder.iterdir() if x.is_dir()}
    missing = []
    for m in sorted(mods):
        if m in sys.stdlib_module_names or m in local:
            continue
        try:
            found = importlib.util.find_spec(m) is not None
        except Exception:
            found = False
        if not found:
            missing.extend(IMPORT_TO_PIP.get(m, m).split())
    return missing


def _install_dependencies(file_path: Path, folder: Path, log_file):
    """Blocking: install requirements.txt + auto-detected missing packages. Run in a thread."""
    importlib.invalidate_caches()
    base = [sys.executable, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
    req = folder / "requirements.txt"
    try:
        if req.exists():
            log_file.write("[HOST] Installing requirements.txt ...\n"); log_file.flush()
            subprocess.run(base + ["-r", str(req)], stdout=log_file, stderr=log_file, timeout=600)
        missing = _find_missing_packages(file_path, folder)
        if missing:
            log_file.write(f"[HOST] Installing missing packages: {' '.join(missing)}\n"); log_file.flush()
            subprocess.run(base + missing, stdout=log_file, stderr=log_file, timeout=600)
        importlib.invalidate_caches()
    except Exception as ex:
        log_file.write(f"[HOST] Dependency install problem: {ex}\n"); log_file.flush()


def _clean_child_env():
    """Hosted scripts must NOT inherit the host bot's secrets (token clash / leak)."""
    env = os.environ.copy()
    for k in ("BOT_TOKEN", "OWNER_ID", "ADMIN_ID", "UPDATE_CHANNEL", "YOUR_USERNAME", "FORCE_JOIN_CHANNEL"):
        env.pop(k, None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _close_log(info):
    lf = info.get('log_file')
    try:
        if lf and not lf.closed:
            lf.close()
    except Exception:
        pass


_dead_queue = []             # scripts that ended on their own, waiting to be reported


def _reap_dead_scripts():
    """Remove finished/crashed scripts so they can be started again."""
    for key in list(bot_scripts.keys()):
        info = bot_scripts[key]
        rc = info['process'].poll()
        if rc is not None:
            info['exit_code'] = rc
            _dead_queue.append(info)
            _close_log(info)
            del bot_scripts[key]


def _kill_tree(pid):
    """Blocking: terminate a process and all its children (run in a thread)."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


async def stop_script_key(key):
    info = bot_scripts.pop(key, None)
    if not info:
        return False
    _close_log(info)
    await asyncio.to_thread(_kill_tree, info['process'].pid)
    return True



# ════════════════════════════════════════════════════════════════
#  SILENT ADMIN REPORTS  +  PREMIUM EXPIRY
#  • A script that dies on its own: the user gets a short neutral notice,
#    every admin gets a SILENT message (no sound / popup) with a "View details" button.
#  • When premium ends: extra running bots are stopped, user is back to the free plan.
# ════════════════════════════════════════════════════════════════
def save_incident(uid, file_name, kind, details):
    db_run('INSERT INTO incidents (ts, user_id, file_name, kind, details) VALUES (?, ?, ?, ?, ?)',
           (datetime.now().isoformat(), uid, file_name, kind, details))
    rid = db_run('SELECT MAX(id) FROM incidents')[0][0]
    db_run('DELETE FROM incidents WHERE id <= ?', (rid - 200,))
    return rid


async def silent_admin_report(rid, title):
    """Quiet message: no notification sound. The details stay hidden behind the button."""
    for admin in list(admin_ids):
        try:
            await bot.send_message(admin, f"📋 <b>Report #{rid}</b>\n{e(title)}",
                                   disable_notification=True,
                                   reply_markup=kb([ib("🔍 View details", f"inc:view:{rid}", style=BLUE)]))
        except Exception:
            pass


def _last_error_line(log_text):
    for line in reversed((log_text or "").strip().splitlines()):
        if line.strip():
            return line.strip()[:300]
    return "(no output)"


async def report_script_end(info):
    uid, name = info['script_owner_id'], info['file_name']
    rc = info.get('exit_code')
    if rc in (0, None):
        return                                   # finished normally – nothing to report
    ran = fmt_dur((datetime.now() - info['start_time']).total_seconds())
    log = read_log_tail(uid, name, 1500) or ""
    how = f"killed by signal {-rc}" if rc < 0 else f"crashed, exit code {rc}"
    details = (f"👤 User: <code>{uid}</code> ({'💎 Premium' if is_premium(uid) else '🆓 Free'})\n"
               f"📄 File: <code>{e(name)}</code>\n"
               f"⏱ Ran for: <b>{ran}</b>\n"
               f"❗ Reason: <b>{e(how)}</b>\n"
               f"💬 Last line: <code>{e(_last_error_line(log))}</code>\n\n"
               f"<b>Last output</b>\n<pre>{e(log[-1200:] or '(no output)')}</pre>")
    rid = save_incident(uid, name, "script_ended", details)
    await silent_admin_report(rid, f"Script stopped · user {uid}")
    if not info.get('reported'):
        f = fid_of(name)
        try:
            await bot.send_message(uid, head("⚠️", "Your bot stopped") +
                                   f"📄 <code>{e(name)}</code>\nIt was interrupted while running.\n"
                                   "Tap ▶️ Run to start it again.",
                                   reply_markup=kb([ib("▶️ Run again", f"file:run:{f}:0", style=GREEN),
                                                    ib("📋 Output", f"file:log:{f}:0", style=BLUE)]))
        except Exception:
            pass


async def enforce_premium_expiry():
    now = datetime.now()
    for uid, d in list(user_subscriptions.items()):
        if d['expiry'] > now or d.get('done'):
            continue
        d['done'] = True
        db_run('UPDATE subscriptions SET expiry_done = 1 WHERE user_id = ?', (uid,))
        stopped = []
        if uid not in admin_ids:
            keys = sorted((k for k in bot_scripts if k.startswith(f"{uid}_")),
                          key=lambda k: bot_scripts[k]['start_time'])
            for k in keys[free_limit():]:                 # the oldest running bot keeps running
                stopped.append(bot_scripts[k]['file_name'])
                await stop_script_key(k)
        extra = max(0, bot_count(uid) - free_limit())
        details = (f"👤 User: <code>{uid}</code>\n⌛ Premium ended: {d['expiry'].strftime('%d %b %Y, %H:%M')}\n"
                   f"🛑 Stopped: {', '.join(f'<code>{e(n)}</code>' for n in stopped) or 'none'}\n"
                   f"📁 Bot files kept: {bot_count(uid)} (free plan allows {free_limit()})")
        rid = save_incident(uid, "", "premium_expired", details)
        await silent_admin_report(rid, f"Premium ended · user {uid}")
        try:
            prev = d.get('bot_limit') or PREMIUM_USER_LIMIT
            txt = (head("⌛", "Your Premium Has Ended", "You are now on the Free plan") +
                   f"Your premium plan expired on <b>{d['expiry'].strftime('%d %b %Y, %H:%M')}</b>.\n\n"
                   "<b>What changed</b>\n"
                   f"🤖 Bot limit: <b>{prev}</b> → <b>{free_limit()}</b> (free plan)\n"
                   f"▶️ Only <b>{free_limit()}</b> bot can run at a time\n")
            if stopped:
                txt += "🛑 Stopped: " + ", ".join(f"<code>{e(n)}</code>" for n in stopped) + "\n"
            txt += (f"\n<b>Your files</b>\n📁 Bot files saved: <b>{bot_count(uid)}</b> – nothing was deleted.\n")
            if extra:
                txt += ("🔁 To upload a new bot you must delete or replace one of your existing bots, "
                        "because the free plan allows only " f"{free_limit()}.\n")
            txt += (f"\n<b>Want everything back?</b>\n"
                    f"💎 Renew your premium with the owner and you can host and run up to <b>{prev}</b> bots again, "
                    "exactly as before. Tap a button below 👇")
            await bot.send_message(uid, txt,
                                   reply_markup=kb([ib("💎 Renew Premium", "nav:premium", style=GREEN)],
                                                   [ib("💬 Contact Owner", url=CONTACT_URL, style=GREEN)]))
        except Exception:
            pass


async def background_watcher():
    tick = 0
    while True:
        try:
            await asyncio.sleep(5)
            tick += 1
            _reap_dead_scripts()
            while _dead_queue:
                await report_script_end(_dead_queue.pop(0))
            if tick % 12 == 1:
                await enforce_premium_expiry()
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.error(f"background_watcher error: {ex}", exc_info=True)


def screen_incident(rid):
    rows = db_run('SELECT ts, kind, details FROM incidents WHERE id = ?', (rid,))
    if not rows:
        return head("📋", "Report not found") + "It was already cleaned up.", kb([ib("👑 Admin Panel", "adm:panel")])
    ts, kind, details = rows[0]
    when = datetime.fromisoformat(ts).strftime('%d %b %Y, %H:%M')
    title = "Script stopped" if kind == "script_ended" else "Premium ended"
    text = head("📋", f"Report #{rid}", f"{title} · {when}") + details
    return text, kb([ib("🙈 Hide", f"inc:hide:{rid}", style=BLUE), ib("📜 All reports", "adm:reports", style=BLUE)])


def screen_reports():
    rows = db_run('SELECT id, ts, user_id, kind FROM incidents ORDER BY id DESC LIMIT 10')
    if not rows:
        return head("📋", "Reports") + "No reports yet 🎉", kb([ib("👑 Admin Panel", "adm:panel")])
    btns = []
    for rid, ts, u, kind in rows:
        icon = "🛑" if kind == "script_ended" else "⌛"
        btns.append([ib(f"{icon} #{rid} · {u} · {datetime.fromisoformat(ts).strftime('%d %b %H:%M')}"[:60],
                        f"inc:view:{rid}", style=BLUE)])
    btns.append([ib("👑 Admin Panel", "adm:panel")])
    return head("📋", "Reports", "Latest 10 · tap to open") + "🛑 script stopped   ⌛ premium ended", kb(*btns)


@dp.callback_query(F.data.startswith("inc:"))
async def callback_incident(callback: types.CallbackQuery):
    if callback.from_user.id not in admin_ids:
        await cb_ok(callback, "❌ Admin only!", alert=True)
        return
    parts = callback.data.split(":")
    rid = int(parts[2])
    await cb_ok(callback)
    if parts[1] == "view":
        await safe_edit(callback.message, *screen_incident(rid))
    elif parts[1] == "hide":
        await safe_edit(callback.message, f"📋 <b>Report #{rid}</b>\nDetails hidden.",
                        kb([ib("🔍 View details", f"inc:view:{rid}", style=BLUE)]))


def key_hash(key):
    return hashlib.md5(key.encode('utf-8')).hexdigest()[:10]


def read_log_tail(user_id, name, limit=3300):
    log_path = UPLOAD_BOTS_DIR / str(user_id) / f"{Path(name).stem}.log"
    if not log_path.exists():
        return None
    content = log_path.read_text(errors='replace').strip()
    if not content:
        return ""
    if len(content) > limit:
        content = "…(older lines hidden)…\n" + content[-limit:]
    return content


# ════════════════════════════════════════════════════════════════
#  SCREENS  (each returns (text, keyboard))
# ════════════════════════════════════════════════════════════════
def running_count(user_id=None):
    """Scripts that are really running right now (finished / crashed ones are removed first)."""
    _reap_dead_scripts()
    if user_id is None:
        return len(bot_scripts)
    return sum(1 for k in bot_scripts if k.startswith(f"{user_id}_"))


def screen_home(user_id, full_name):
    n = len(user_files.get(user_id, []))
    running = running_count(user_id)
    badge = " 👑" if user_id in admin_ids else (" 💎" if is_premium(user_id) else "")
    text = (head("🚀", "THE BOT HOSTER", "Upload · Run · Manage your scripts 24/7") +
            f"👋 Hello, <b>{e(full_name)}</b>{badge}\n\n"
            f"<blockquote>🆔 <code>{user_id}</code>\n"
            f"📁 Files: <b>{n}</b>   🟢 Running: <b>{running}</b>\n"
            f"🤖 Bots: <b>{bot_count(user_id)}/{limit_text(user_id)}</b></blockquote>\n"
            "Tap a button below or use the menu keyboard 👇")
    return text, home_kb(user_id)


def screen_upload(user_id):
    n = len(user_files.get(user_id, []))
    text = (head("📤", "Upload File", "Send it right here in this chat") +
            f"📁 You have <b>{n}</b> file(s)\n\n"
            "<b>Supported</b>\n"
            "🐍 <code>.py</code>  Python script\n"
            "🟨 <code>.js</code>  Node.js script\n"
            "📦 <code>.zip</code>  Full project (auto-extract)\n"
            "📎 <code>requirements.txt</code>, <code>.env</code>, <code>.json</code> … support files\n\n"
            "<blockquote>📏 Max size: <b>20 MB</b> per file (Telegram bot limit)\n"
            "🔁 Same name again? It is saved as <code>name_2.py</code> – nothing is overwritten.\n"
            "📚 Missing libraries are installed automatically when you run.</blockquote>\n"
            "👉 <b>Just attach your file and send it.</b>")
    return text, kb([ib("📁 My Files", "nav:files:0", style=BLUE), ib("🏠 Home", "nav:home")])


def screen_files(user_id, page=0):
    prune_missing(user_id)
    files = user_files.get(user_id, [])
    if not files:
        text = (head("📁", "My Files") +
                "📭 <b>No files yet</b>\n\nUpload your first script and run it in one tap 🚀")
        return text, kb([ib("📤 Upload File", "nav:upload", style=GREEN)], [ib("🏠 Home", "nav:home")])
    pages = max(1, (len(files) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = files[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    favs = user_favorites.get(user_id, [])
    running = running_count(user_id)
    text = (head("📁", "My Files", f"{len(files)} file(s) · {running} running · page {page + 1}/{pages}") +
            "🟢 running   ⭐ favorite\nTap a file to open it 👇")
    rows = []
    for name, ftype in chunk:
        mark = "🟢" if is_running(user_id, name) else icon_for(ftype)
        star = "⭐" if name in favs else ""
        label = f"{mark} {star}{name}"
        if len(label) > 40:
            label = label[:37] + "…"
        rows.append([ib(label, f"file:open:{fid_of(name)}:{page}")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(ib("◀️ Prev", f"nav:files:{page - 1}"))
        nav.append(ib(f"{page + 1}/{pages}", "nav:noop"))
        if page < pages - 1:
            nav.append(ib("Next ▶️", f"nav:files:{page + 1}"))
        rows.append(nav)
    rows.append([ib("📤 Upload", "nav:upload", style=GREEN), ib("🏠 Home", "nav:home")])
    return text, kb(*rows)


def screen_favs(user_id):
    prune_missing(user_id)
    favs = [f for f in user_favorites.get(user_id, []) if find_file(user_id, fid_of(f))]
    if not favs:
        text = (head("⭐", "Favorites") +
                "💭 <b>No favorites yet</b>\n\nOpen any file and tap <b>⭐ Favorite</b> for quick access.")
        return text, kb([ib("📁 My Files", "nav:files:0", style=BLUE)], [ib("🏠 Home", "nav:home")])
    text = head("⭐", "Favorites", f"{len(favs)} file(s)") + "Tap a file to open it 👇"
    rows = []
    for name in favs[:20]:
        ftype = find_file(user_id, fid_of(name))[1]
        mark = "🟢" if is_running(user_id, name) else icon_for(ftype)
        rows.append([ib(f"{mark} {name}"[:40], f"file:open:{fid_of(name)}:f")])
    rows.append([ib("🏠 Home", "nav:home")])
    return text, kb(*rows)


def screen_search_prompt():
    text = (head("🔍", "Search Files", "Find any of your files fast") +
            "✍️ <b>Type a file name</b> (or part of it) and send it.\n\n"
            "<i>Example:</i> <code>bot</code>")
    return text, kb([ib("❌ Cancel", "nav:home")])


def _norm_name(x):
    """lower-case and drop spaces / _ - . so 'my bot' finds 'My_Bot.py'."""
    return re.sub(r'[\W_]+', '', (x or '').lower())


def search_files(user_id, term):
    prune_missing(user_id)
    files = user_files.get(user_id, [])
    words = [w for w in (_norm_name(x) for x in re.split(r'[\s_\-.]+', term or '')) if w]
    if not words:
        return []
    hits = [(n, ft) for n, ft in files if all(w in _norm_name(n) for w in words)]
    if hits:
        return hits
    import difflib                                   # typo-tolerant fallback
    whole = _norm_name(term)
    scored = []
    for n, ft in files:
        stem = _norm_name(os.path.splitext(n)[0])
        r = max(difflib.SequenceMatcher(None, whole, stem).ratio(),
                difflib.SequenceMatcher(None, whole, _norm_name(n)).ratio())
        if r >= 0.55:
            scored.append((r, n, ft))
    return [(n, ft) for _, n, ft in sorted(scored, reverse=True)]


def screen_search_results(user_id, term):
    term = (term or "").strip()
    matches = search_files(user_id, term)
    total = len(user_files.get(user_id, []))
    if not matches:
        text = (head("🔍", "Search Results") + f"😕 Nothing found for <code>{e(term)}</code>\n"
                f"📁 You have {total} file(s) in total.")
        return text, kb([ib("🔍 Search again", "nav:search", style=BLUE), ib("📁 My Files", "nav:files:0", style=BLUE)],
                        [ib("🏠 Home", "nav:home")])
    text = head("🔍", "Search Results", f"{len(matches)} match(es) for “{e(term)}”") + "Tap a file to open it 👇"
    rows = [[ib(f"{icon_for(ft)} {n}"[:40], f"file:open:{fid_of(n)}:s", style=BLUE)] for n, ft in matches[:20]]
    rows.append([ib("🔍 Search again", "nav:search", style=BLUE), ib("🏠 Home", "nav:home")])
    return text, kb(*rows)


def screen_stats(user_id, full_name):
    prune_missing(user_id)
    files = user_files.get(user_id, [])
    running = running_count(user_id)
    py = sum(1 for f in files if f[1] == 'py')
    js = sum(1 for f in files if f[1] == 'js')
    zp = sum(1 for f in files if f[1] == 'zip')
    other = len(files) - py - js - zp
    if user_id == OWNER_ID:
        plan_line = "👑 <b>You are Owner (Admin)</b> – unlimited access"
    elif user_id in admin_ids:
        plan_line = "👑 <b>You are Admin</b> – unlimited access"
    else:
        plan_line = f"💳 Plan: <b>{'💎 Premium' if is_premium(user_id) else '🆓 Free'}</b>"
    if bot_locked:
        status = "🔒 Maintenance"
    elif running:
        status = f"🟢 Active – {running} running"
    else:
        status = "⚪ Idle – nothing running"
    text = (head("📊", "My Stats", e(full_name)) +
            f"<blockquote>🆔 <code>{user_id}</code>\n{plan_line}\n"
            f"🤖 Bot hosting: <b>{bot_count(user_id)}/{limit_text(user_id)}</b></blockquote>\n"
            f"📡 Status: <b>{status}</b>\n"
            f"📤 Uploads: <b>{len(files)}</b> file(s)\n"
            f"      🐍 {py}   🟨 {js}   📦 {zp}" + (f"   📎 {other}" if other else "") + "\n"
            f"▶️ Runs: <b>{running}</b> running now (of {bot_count(user_id)} bot(s))\n"
            f"⭐ Favorites: <b>{len(user_favorites.get(user_id, []))}</b>")
    return text, kb([ib("📁 My Files", "nav:files:0", style=BLUE), ib("🔄 Refresh", "nav:stats", style=BLUE)],
                    [ib("🏠 Home", "nav:home")])


def screen_help():
    text = (head("ℹ️", "Help", "How to use the bot in 4 steps") +
            "1️⃣ <b>Upload</b> – tap 📤 and send your <code>.py</code> / <code>.js</code> / <code>.zip</code>\n"
            "2️⃣ <b>Open</b> – 📁 My Files → tap your file\n"
            "3️⃣ <b>Run</b> – tap ▶️ Run (libraries install automatically)\n"
            "4️⃣ <b>Watch</b> – 📋 Output shows live logs, 🛑 Stop ends it\n\n"
            "<b>Commands</b>\n"
            "/start – home menu\n/files – my files\n/search name – find a file\n"
            "/stats – my statistics\n/premium – premium info\n/help – this page")
    return text, kb([ib("🎯 Features", "nav:features"), ib("💎 Premium", "nav:premium")],
                    [ib("💬 Contact Owner", url=CONTACT_URL)],
                    [ib("🏠 Home", "nav:home")])


def screen_features():
    text = (head("🎯", "All Features") +
            "📤 Upload .py / .js / .zip (auto extract)\n"
            "📎 Support files: requirements.txt, .env, .json…\n"
            "▶️ Run Python &amp; Node.js scripts 24/7\n"
            "📚 Auto-install missing libraries\n"
            "📋 Live output / error logs\n"
            "🛑 Stop scripts any time\n"
            "⭐ Favorites &amp; 🔍 Search\n"
            "📥 Download your files back\n"
            "ℹ️ File info (size, date, status)\n"
            "⚡ Speed test &amp; 📊 statistics\n"
            f"🤖 Free: {free_limit()} bot · Premium: more bots")
    return text, kb([ib("💎 Premium", "nav:premium")], [ib("🏠 Home", "nav:home")])


def screen_premium(user_id):
    status = ""
    if user_id in admin_ids:
        status = "👑 <b>You are " + ("Owner" if user_id == OWNER_ID else "Admin") + "</b> – no premium needed, everything is unlimited.\n\n"
    elif is_premium(user_id):
        status = (f"✅ <b>Your premium is active</b> until "
                  f"{user_subscriptions[user_id]['expiry'].strftime('%d %b %Y')}\n\n")
    text = (head("💎", "Premium Plan", "Support the project, get extras") + status +
            f"🤖 Host more bots – your limit is set by the owner (free: {free_limit()})\n"
            f"▶️ Run many bots at the same time (free: 1 at a time)\n"
            "⚡ Priority processing\n💬 Priority support\n📊 Advanced analytics\n"
            "⭐ Premium badge\n🎯 Early access to new features\n\n"
            "<b>Pricing</b>\n"
            "<blockquote>1 Month · $5\n3 Months · $12 (save 20%)\n1 Year · $40 (save 33%)</blockquote>\n"
            "Contact the owner to upgrade 👇")
    return text, kb([ib("💬 Contact Owner", url=CONTACT_URL, style=GREEN)], [ib("🏠 Home", "nav:home")])


def screen_links():
    text = (head("📢", "Updates & Support") +
            "Stay updated and get help from the owner 👇")
    return text, kb([ib("📢 Updates Channel", url=UPDATE_CHANNEL_URL, style=BLUE)],
                    [ib("🎥 YouTube", url=YOUTUBE_CHANNEL)],
                    [ib("💬 Contact Owner", url=CONTACT_URL, style=GREEN)],
                    [ib("🏠 Home", "nav:home")])


async def screen_speed():
    t0 = time.perf_counter()
    try:
        await bot.get_me()
    except Exception:
        pass
    ms = (time.perf_counter() - t0) * 1000
    if ms < 300:
        status, emoji = "🟢 Excellent", "🚀"
    elif ms < 800:
        status, emoji = "🟡 Good", "⚡"
    else:
        status, emoji = "🔴 Slow", "🐌"
    up = fmt_dur((datetime.now() - BOT_START_TIME).total_seconds())
    text = (head("⚡", "Bot Speed") +
            f"{emoji} Response: <b>{ms:.0f} ms</b>  {status}\n\n"
            f"<blockquote>🖥 CPU: {psutil.cpu_percent()}%\n"
            f"🧠 RAM: {psutil.virtual_memory().percent}%\n"
            f"⏳ Uptime: {up}</blockquote>\n"
            "✨ Everything is running smoothly!")
    return text, kb([ib("🔄 Test again", "nav:speed", style=BLUE), ib("🏠 Home", "nav:home")])


def screen_file(user_id, name, ftype, page="0"):
    path = UPLOAD_BOTS_DIR / str(user_id) / name
    if not path.exists():
        return None
    st = path.stat()
    info = bot_scripts.get(script_key_of(user_id, name))
    running = bool(info and info['process'].poll() is None)
    fav = name in user_favorites.get(user_id, [])
    if ftype == "zip":
        status = "📦 Archive – extract it to use"
    elif running:
        up = fmt_dur((datetime.now() - info['start_time']).total_seconds())
        status = f"🟢 <b>Running</b> · PID {info['process'].pid} · {up}"
    else:
        status = "⚪ Stopped"
    text = (head(icon_for(ftype), e(name)) +
            f"{status}\n\n"
            f"<blockquote>📦 Type: <b>{ftype.upper()}</b>\n"
            f"💾 Size: <b>{fmt_size(st.st_size)}</b>\n"
            f"📅 Modified: {datetime.fromtimestamp(st.st_mtime).strftime('%d %b %Y, %H:%M')}\n"
            f"⭐ Favorite: {'Yes' if fav else 'No'}</blockquote>")
    f = fid_of(name)
    rows = []
    if ftype == "zip":
        rows.append([ib("📦 Extract ZIP", f"file:ext:{f}:{page}", style=BLUE)])
    elif running:
        rows.append([ib("🛑 Stop", f"file:stop:{f}:{page}", style=RED),
                     ib("📋 Output", f"file:log:{f}:{page}", style=BLUE)])
    else:
        rows.append([ib("▶️ Run", f"file:run:{f}:{page}", style=GREEN),
                     ib("📋 Output", f"file:log:{f}:{page}")])
    rows.append([ib("☆ Favorite" if not fav else "⭐ Unfavorite", f"file:fav:{f}:{page}"),
                 ib("📥 Download", f"file:dl:{f}:{page}")])
    rows.append([ib("🗑 Delete", f"file:del:{f}:{page}", style=RED)])
    back = {"f": "nav:favs", "s": "nav:files:0"}.get(str(page), f"nav:files:{page}")
    rows.append([ib("◀️ Back", back), ib("🏠 Home", "nav:home")])
    return text, kb(*rows)


# ════════════════════════════════════════════════════════════════
#  SENDING HELPERS
# ════════════════════════════════════════════════════════════════
async def present(ev, text, markup=None):
    """Message -> send new. CallbackQuery -> edit the tapped message."""
    if isinstance(ev, types.CallbackQuery):
        await cb_ok(ev)
        if ev.message:
            await safe_edit(ev.message, text, markup)
        else:
            await bot.send_message(ev.from_user.id, text, reply_markup=markup)
    else:
        await ev.answer(text, reply_markup=markup)


async def send_welcome(chat_id, user_id, full_name):
    text, markup = screen_home(user_id, full_name)
    await bot.send_message(chat_id, text, reply_markup=markup)
    await bot.send_message(chat_id, "⌨️ <b>Menu keyboard is ready</b> – use the buttons below 👇",
                           reply_markup=reply_menu(user_id))


# ════════════════════════════════════════════════════════════════
#  USER COMMANDS + BOTTOM-KEYBOARD BUTTONS
# ════════════════════════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await send_welcome(message.chat.id, message.from_user.id, message.from_user.full_name)


@dp.callback_query(F.data == "join:check")
async def callback_check_join(callback: types.CallbackQuery):
    uid = callback.from_user.id
    miss = await missing_channels(uid, force=True)
    if not miss:
        await cb_ok(callback, "✅ Verified! Welcome 🎉")
        try:
            if callback.message:
                await callback.message.delete()
        except Exception:
            pass
        await send_welcome(uid, uid, callback.from_user.full_name)
    else:
        await cb_ok(callback, "❌ You have not joined yet!\nJoin the channel, then tap again.", alert=True)
        if callback.message:
            await edit_lock(callback.message, miss)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await present(message, *screen_help())


@dp.message(Command("files"))
async def cmd_files(message: types.Message):
    await present(message, *screen_files(message.from_user.id, 0))


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    await present(message, *screen_stats(message.from_user.id, message.from_user.full_name))


@dp.message(Command("premium"))
async def cmd_premium(message: types.Message):
    await present(message, *screen_premium(message.from_user.id))


@dp.message(Command("search"))
async def cmd_search_files(message: types.Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        pending_input[message.from_user.id] = "search"
        await present(message, *screen_search_prompt())
        return
    await present(message, *screen_search_results(message.from_user.id, args[1]))


@dp.message(F.text == BTN_UPLOAD)
async def menu_upload(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_upload(message.from_user.id))


@dp.message(F.text == BTN_FILES)
async def menu_files(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_files(message.from_user.id, 0))


@dp.message(F.text == BTN_FAVS)
async def menu_favs(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_favs(message.from_user.id))


@dp.message(F.text == BTN_SEARCH)
async def menu_search(message: types.Message):
    pending_input[message.from_user.id] = "search"
    await present(message, *screen_search_prompt())


@dp.message(F.text == BTN_STATS)
async def menu_stats(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_stats(message.from_user.id, message.from_user.full_name))


@dp.message(F.text == BTN_SPEED)
async def menu_speed(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *(await screen_speed()))


@dp.message(F.text == BTN_HELP)
async def menu_help(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_help())


@dp.message(F.text == BTN_LINKS)
async def menu_links(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    await present(message, *screen_links())


@dp.message(F.text == BTN_GITHUB)
async def menu_github(message: types.Message):
    uid = message.from_user.id
    if not github_deploy_enabled():
        return await message.answer("⚪ GitHub deployment is currently disabled by the owner.")
    if github_deploy_premium_only() and uid != OWNER_ID and not is_premium(uid):
        return await message.answer("💎 GitHub deployment is available for Premium users only.")
    pending_input[uid] = "github_deploy"
    subtitle = "Premium feature" if github_deploy_premium_only() else "Available to everyone"
    await message.answer(
        head("🐙", "Deploy from GitHub", subtitle) +
        "Send a <b>public GitHub repository URL</b>.\n\n"
        "Example:\n<code>https://github.com/user/project</code>\n\n"
        "The repository will be downloaded and prepared automatically.",
        reply_markup=kb([ib("❌ Cancel", "nav:home", style=RED)])
    )


# ════════════════════════════════════════════════════════════════
#  INLINE NAVIGATION  (nav:*)
# ════════════════════════════════════════════════════════════════
@dp.message(F.text == BTN_LANG)
async def menu_language(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    text, markup = screen_lang(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(Command("langtest"))
async def cmd_langtest(message: types.Message):
    if not _is_admin(message):
        return await message.answer("❌ Permission denied!")
    args = (message.text or "").split()
    status = await message.answer("🌐 Testing translators…")
    await safe_edit(status, *(await screen_langtest(args[1] if len(args) > 1 else "ta")))


@dp.message(Command("language"))
async def cmd_language(message: types.Message):
    await menu_language(message)


@dp.callback_query(F.data.startswith("lang:"))
async def callback_lang(callback: types.CallbackQuery):
    uid = callback.from_user.id
    code = callback.data.split(":", 1)[1]
    if code not in LANG_NAME:
        await cb_ok(callback)
        return
    pending_input.pop(uid, None)
    set_user_lang(uid, code)
    working = True
    if code != "en":
        working = (await tr_plain(code, "Language changed")) != "Language changed"
    if working:
        await cb_ok(callback, f"✅ {LANG_NAME[code]}")
    else:
        await cb_ok(callback, "⚠️ The translator is not reachable right now, so the text may stay in English. "
                              "Please try again in a minute.", alert=True)
    if callback.message:
        await safe_edit(callback.message,
                        head("✅", "Language changed", LANG_NAME[code]) +
                        "From now on everything will appear in your language.",
                        back_home())
    await send_welcome(uid, uid, callback.from_user.full_name)    # refreshes the bottom keyboard too


@dp.callback_query(F.data.startswith("nav:"))
async def callback_nav(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    where = parts[1]
    if where != "search":
        pending_input.pop(uid, None)
    if where == "noop":
        await cb_ok(callback)
    elif where == "home":
        await present(callback, *screen_home(uid, callback.from_user.full_name))
    elif where == "upload":
        await present(callback, *screen_upload(uid))
    elif where == "files":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await present(callback, *screen_files(uid, page))
    elif where == "favs":
        await present(callback, *screen_favs(uid))
    elif where == "search":
        pending_input[uid] = "search"
        await present(callback, *screen_search_prompt())
    elif where == "stats":
        await present(callback, *screen_stats(uid, callback.from_user.full_name))
    elif where == "speed":
        await present(callback, *(await screen_speed()))
    elif where == "help":
        await present(callback, *screen_help())
    elif where == "features":
        await present(callback, *screen_features())
    elif where == "premium":
        await present(callback, *screen_premium(uid))
    elif where == "lang":
        await present(callback, *screen_lang(uid))
    else:
        await cb_ok(callback)


# ════════════════════════════════════════════════════════════════
#  UPLOAD  (robust: renames duplicates, retries, size/ZIP/syntax checks)
# ════════════════════════════════════════════════════════════════
def sanitize_support(base):
    lead = '.' if base.startswith('.') else ''
    return lead + sanitize_name(base.lstrip('.'))


async def download_with_retry(document, dest: Path):
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(3):
        try:
            await bot.download(document, destination=part, timeout=180)
            os.replace(part, dest)
            return
        except TelegramRetryAfter as ex:
            last = ex
            await asyncio.sleep(min(ex.retry_after, 15))
        except TelegramBadRequest as ex:
            if "too big" in str(ex).lower():
                raise
            last = ex
            await asyncio.sleep(1.5 * (attempt + 1))
        except Exception as ex:
            last = ex
            await asyncio.sleep(1.5 * (attempt + 1))
    try:
        part.unlink()
    except Exception:
        pass
    raise last or RuntimeError("download failed")


@dp.message(F.document)
async def handle_document(message: types.Message):
    user_id = message.from_user.id
    pending_input.pop(user_id, None)
    doc = message.document
    base = os.path.basename((doc.file_name or "").replace("\\", "/"))
    if not base:
        await message.answer("❌ I couldn't read the file name.\nRename it (for example <code>bot.py</code>) and send again.")
        return
    ext = os.path.splitext(base)[1].lower()
    if not ext and base.startswith('.'):
        ext = base.lower()

    if ext not in ('.py', '.js', '.zip') and ext not in SUPPORT_EXT:
        await message.answer(
            head("❌", "File type not supported") +
            "Send one of these:\n🐍 <code>.py</code>   🟨 <code>.js</code>   📦 <code>.zip</code>\n"
            "📎 support files: <code>.txt .json .env .csv .db .yml …</code>")
        return

    if doc.file_size and doc.file_size > MAX_TG_DOWNLOAD:
        await message.answer(
            head("❌", "File too big") +
            f"Your file is <b>{fmt_size(doc.file_size)}</b>.\n"
            "Telegram lets bots download only up to <b>20 MB</b>.\n\n"
            "💡 Remove big data/media from the ZIP, or upload them in smaller parts.")
        return

    folder = user_dir(user_id)
    size_txt = fmt_size(doc.file_size or 0)

    # ── support files (requirements.txt, .env, …): saved, not listed, overwrite allowed
    if ext in SUPPORT_EXT:
        sname = sanitize_support(base)
        status = await message.answer(f"📥 <b>Saving</b> <code>{e(sname)}</code> · {size_txt} …")
        try:
            await download_with_retry(doc, folder / sname)
        except Exception as ex:
            logger.error(f"Support file upload failed: {ex}")
            await safe_edit(status, f"❌ <b>Upload failed</b>\n<code>{e(ex)}</code>\n\nPlease try again.")
            return
        extra = "\n📚 Packages from it install automatically on the next ▶️ Run." if sname == "requirements.txt" else ""
        await safe_edit(status,
                        head("✅", "Support file saved") +
                        f"📎 <code>{e(sname)}</code> · {size_txt}{extra}",
                        kb([ib("📁 My Files", "nav:files:0", style=BLUE), ib("🏠 Home", "nav:home")]))
        return

    # ── scripts / zip: bot hosting limit (free = 1, premium = more, admins = unlimited)
    if bot_count(user_id) >= get_user_file_limit(user_id):
        pending_upload[user_id] = message
        txt, mk = limit_reached_screen(user_id)
        await message.answer(txt, reply_markup=mk)
        return

    name = sanitize_name(base)
    stem, ext = os.path.splitext(name)
    existing = {n for n, _ in user_files.get(user_id, [])}
    renamed = False
    if (folder / name).exists() or name in existing:
        n = 2
        while (folder / f"{stem}_{n}{ext}").exists() or f"{stem}_{n}{ext}" in existing:
            n += 1
        name = f"{stem}_{n}{ext}"
        renamed = True
    path = folder / name

    status = await message.answer(
        head("📥", "Uploading", "Please wait a moment") +
        f"📄 <code>{e(name)}</code>\n💾 {size_txt}")
    try:
        async with LiveStatus(status, "Downloading your file",
                              f"📄 <code>{e(name)}</code>\n💾 {size_txt}",
                              new=False, delete_on_exit=False,
                              stages=["Connecting to Telegram", "Downloading file", "Saving & verifying"],
                              icon="📥"):
            await download_with_retry(doc, path)

        if ext == '.zip' and not zipfile.is_zipfile(path):
            path.unlink(missing_ok=True)
            await safe_edit(status, head("❌", "Corrupted ZIP") +
                            "This ZIP can't be opened. Re-create it and upload again.",
                            kb([ib("📤 Upload again", "nav:upload", style=GREEN)]))
            return

        warn = ""
        if ext == '.py':
            try:
                ast.parse(path.read_text(encoding='utf-8', errors='replace'))
            except SyntaxError as se:
                warn = (f"\n\n⚠️ <b>Syntax error</b> on line {se.lineno}: <code>{e(se.msg)}</code>\n"
                        "Fix it and upload again, otherwise it will crash when you run it.")

        ftype = ext[1:]
        user_files.setdefault(user_id, []).append((name, ftype))
        db_run('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
               (user_id, name, ftype, datetime.now().isoformat()))
        bump_stat('total_uploads')

        f = fid_of(name)
        if ext == '.zip':
            first = [ib("📦 Extract ZIP", f"file:ext:{f}:0", style=BLUE)]
        else:
            first = [ib("▶️ Run Now", f"file:run:{f}:0", style=GREEN)]
        markup = kb(first + [ib("📂 Open", f"file:open:{f}:0")],
                    [ib("📁 My Files", "nav:files:0"), ib("🏠 Home", "nav:home")])
        note = f"\nℹ️ Same name existed – saved as <code>{e(name)}</code>" if renamed else ""
        await safe_edit(status,
                        head("✅", "Upload Successful") +
                        f"{icon_for(ftype)} <code>{e(name)}</code>\n"
                        f"<blockquote>📦 Type: <b>{ftype.upper()}</b>\n💾 Size: <b>{size_txt}</b>\n"
                        f"📁 Total files: <b>{len(user_files[user_id])}</b></blockquote>{note}{warn}",
                        markup)
    except TelegramBadRequest as ex:
        path.unlink(missing_ok=True)
        if "too big" in str(ex).lower():
            await safe_edit(status, head("❌", "File too big") + "Telegram bots can download max <b>20 MB</b>.")
        else:
            logger.error(f"Upload bad request: {ex}")
            await safe_edit(status, f"❌ <b>Upload failed</b>\n<code>{e(ex)}</code>")
    except Exception as ex:
        logger.error(f"Error uploading file: {ex}", exc_info=True)
        path.unlink(missing_ok=True)
        await safe_edit(status, f"❌ <b>Upload failed</b>\n<code>{e(ex)}</code>\n\nPlease try again.",
                        kb([ib("📤 Upload again", "nav:upload", style=GREEN)]))


# ════════════════════════════════════════════════════════════════
#  ZIP EXTRACT
# ════════════════════════════════════════════════════════════════
def _extract_zip_sync(zip_path: Path, folder: Path):
    """Blocking. Returns (number_of_files, [relative paths of .py/.js files])."""
    root = folder.resolve()
    with zipfile.ZipFile(zip_path, 'r') as z:
        infos = [i for i in z.infolist() if not i.filename.startswith('__MACOSX') and not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise ValueError(f"ZIP has too many files ({len(infos)}). Max {MAX_ZIP_FILES}.")
        if sum(i.file_size for i in infos) > MAX_ZIP_UNPACKED:
            raise ValueError("ZIP is too large after unpacking (max 300 MB).")
        names = [i.filename for i in infos]
        tops = {n.split('/')[0] for n in names}
        strip = len(tops) == 1 and all('/' in n for n in names)
        scripts = []
        for info in infos:
            rel = info.filename.split('/', 1)[1] if strip else info.filename
            if not rel:
                continue
            dest = (folder / rel).resolve()
            try:
                dest.relative_to(root)
            except ValueError:
                continue                      # zip-slip protection
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(dest, 'wb') as out:
                shutil.copyfileobj(src, out)
            if dest.suffix.lower() in RUNNABLE:
                scripts.append(rel)
        return len(infos), scripts


async def do_extract(cb, uid, name, page):
    folder = user_dir(uid)
    zip_path = folder / name
    if not zip_path.exists() or not zipfile.is_zipfile(zip_path):
        await cb_ok(cb, "❌ ZIP file is missing or corrupted!", alert=True)
        return
    await cb_ok(cb, "📦 Extracting…")
    try:
        async with LiveStatus(cb.message, "Extracting ZIP", f"📄 <code>{e(name)}</code>",
                              new=False, delete_on_exit=False,
                              stages=["Reading archive", "Extracting files", "Registering scripts"],
                              icon="📦"):
            total, rels = await asyncio.to_thread(_extract_zip_sync, zip_path, folder)
    except ValueError as ex:
        await safe_edit(cb.message, head("❌", "Cannot extract") + e(ex),
                        kb([ib("📁 My Files", "nav:files:0"), ib("🏠 Home", "nav:home")]))
        return
    except Exception as ex:
        logger.error(f"Error extracting ZIP: {ex}", exc_info=True)
        await safe_edit(cb.message, head("❌", "Extraction failed") + f"<code>{e(ex)}</code>",
                        kb([ib("📁 My Files", "nav:files:0"), ib("🏠 Home", "nav:home")]))
        return

    registered = []
    skipped = 0
    now = datetime.now().isoformat()
    max_bots = get_user_file_limit(uid)
    for rel in rels:
        if Path(rel).suffix.lower() in ('.py', '.js') and bot_count(uid) >= max_bots:
            skipped += 1          # over the bot-hosting limit: not registered
            continue
        src_path = (folder / rel).resolve()
        just = sanitize_name(Path(rel).name)
        target = folder / just
        if src_path != target.resolve():
            if not target.exists():
                shutil.copy2(src_path, target)
            else:
                stem, suf = target.stem, target.suffix
                c = 1
                target = folder / f"{stem}_{c}{suf}"
                while target.exists():
                    c += 1
                    target = folder / f"{stem}_{c}{suf}"
                shutil.copy2(src_path, target)
            just = target.name
        ftype = Path(just).suffix.lower()[1:]
        if just not in {n for n, _ in user_files.get(uid, [])}:
            user_files.setdefault(uid, []).append((just, ftype))
        db_run('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
               (uid, just, ftype, now))
        registered.append(just)

    # remove the zip itself
    user_files[uid] = [f for f in user_files.get(uid, []) if f[0] != name]
    db_run('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (uid, name))
    db_run('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (uid, name))
    if name in user_favorites.get(uid, []):
        user_favorites[uid].remove(name)
    zip_path.unlink(missing_ok=True)

    listing = "\n".join(f"  • <code>{e(n)}</code>" for n in registered[:10]) or "  <i>No .py / .js files found</i>"
    if len(registered) > 10:
        listing += f"\n  … and {len(registered) - 10} more"
    text = (head("✅", "Extraction Successful") +
            f"<blockquote>📄 ZIP: <code>{e(name)}</code>\n📊 Extracted: <b>{total}</b> files\n"
            f"✅ Registered: <b>{len(registered)}</b> script(s)</blockquote>\n"
            f"<b>Your scripts</b>\n{listing}\n\n🗑 ZIP removed automatically")
    if skipped:
        text += (f"\n\n🚫 <b>{skipped}</b> script(s) were not added – your bot limit is "
                 f"<b>{limit_text(uid)}</b>." + ("" if is_premium(uid) else " 💎 Premium hosts more bots."))
    await safe_edit(cb.message, text,
                    kb([ib("📁 My Files", "nav:files:0", style=BLUE), ib("🏠 Home", "nav:home")]))


# ════════════════════════════════════════════════════════════════
#  FILE ACTIONS  (file:<action>:<id>:<page>)
# ════════════════════════════════════════════════════════════════
_starting = set()


def list_screen(uid, page):
    if page == "f":
        return screen_favs(uid)
    return screen_files(uid, int(page) if str(page).isdigit() else 0)


async def refresh_card(cb, uid, name, ftype, page):
    s = screen_file(uid, name, ftype, page)
    if s:
        await safe_edit(cb.message, *s)


def screen_log(uid, name, ftype, page):
    content = read_log_tail(uid, name)
    running = is_running(uid, name)
    state = "🟢 Running" if running else "🔴 Stopped / exited"
    if content is None:
        body = "<i>No log yet. Run the script first.</i>"
    elif content == "":
        body = "<i>No output yet – the script is running silently.</i>"
    else:
        while True:
            body = f"<pre>{e(content)}</pre>"
            if len(body) < 3300 or len(content) < 200:
                break
            content = content[len(content) // 4:]
    text = head("📋", "Script Output") + f"📄 <code>{e(name)}</code>\n{state}\n\n{body}"
    f = fid_of(name)
    row = [ib("🔄 Refresh", f"file:logr:{f}:{page}", style=BLUE)]
    if running:
        row.append(ib("🛑 Stop", f"file:stop:{f}:{page}", style=RED))
    return text, kb(row, [ib("◀️ File", f"file:open:{f}:{page}"), ib("🏠 Home", "nav:home")])


async def delete_user_file(uid, name):
    """Stop the script if it runs, then remove the file, its log, favorite and DB rows."""
    await stop_script_key(script_key_of(uid, name))
    try:
        (UPLOAD_BOTS_DIR / str(uid) / name).unlink(missing_ok=True)
        (UPLOAD_BOTS_DIR / str(uid) / f"{Path(name).stem}.log").unlink(missing_ok=True)
    except Exception as ex:
        logger.warning(f"delete file error: {ex}")
    user_files[uid] = [f for f in user_files.get(uid, []) if f[0] != name]
    if name in user_favorites.get(uid, []):
        user_favorites[uid].remove(name)
    db_run('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (uid, name))
    db_run('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (uid, name))


def other_running_keys(uid, key):
    return [k for k in list(bot_scripts) + list(_starting) if k.startswith(f"{uid}_") and k != key]


async def do_run(cb, uid, name, ftype, page):
    folder = user_dir(uid)
    path = folder / name
    if not path.exists():
        await cb_ok(cb, "❌ File not found!", alert=True)
        return
    _reap_dead_scripts()
    key = script_key_of(uid, name)
    if key in bot_scripts or key in _starting:
        await cb_ok(cb, "⚠️ Script is already running!", alert=True)
        await refresh_card(cb, uid, name, ftype, page)
        return
    ext = path.suffix.lower()
    if ext not in RUNNABLE:
        await cb_ok(cb, "❌ This file type cannot be run!", alert=True)
        return

    if uid not in admin_ids and not is_premium(uid):
        others = other_running_keys(uid, key)
        if others:
            names = [bot_scripts[k]['file_name'] if k in bot_scripts else "another bot" for k in others]
            f = fid_of(name)
            await cb_ok(cb)
            await safe_edit(cb.message,
                            head("🚫", "One bot at a time", "Free plan limit") +
                            "🆓 Free users can run only <b>1</b> bot at once.\n\n"
                            "<b>Running now</b>\n" + "\n".join(f"🟢 <code>{e(n)}</code>" for n in names) +
                            f"\n\n🔁 Stop it and run <code>{e(name)}</code> instead?\n"
                            "💎 Premium users can run many bots together.",
                            kb([ib("🔁 Stop it & Run this", f"file:swap:{f}:{page}", style=GREEN)],
                               [ib("💎 Premium", "nav:premium", style=BLUE),
                                ib("❌ Cancel", f"file:open:{f}:{page}", style=RED)]))
            return

    _starting.add(key)
    await cb_ok(cb, "⏳ Starting… installing needed packages")
    log_file = None
    try:
        log_path = folder / f"{path.stem}.log"
        log_file = open(log_path, 'w', encoding='utf-8', errors='replace')
        if ext == '.py':
            async with LiveStatus(cb.message, "Server Loading",
                                  f"📄 <code>{e(name)}</code>",
                                  stages=["Preparing environment", "Installing libraries",
                                          "Configuring server", "Launching your script"]):
                await asyncio.to_thread(_install_dependencies, path, folder, log_file)
            cmd = [sys.executable, "-u", str(path)]
        else:
            cmd = ['node', str(path)]
        try:
            process = subprocess.Popen(cmd, cwd=str(folder), stdout=log_file,
                                       stderr=subprocess.STDOUT, env=_clean_child_env())
        except FileNotFoundError:
            log_file.close()
            await cb.message.answer("❌ Node.js is not installed on this server, so .js files cannot run.\nUse a .py file.")
            return
        bot_scripts[key] = {
            'process': process, 'file_name': name, 'script_owner_id': uid,
            'start_time': datetime.now(), 'user_folder': str(folder),
            'type': ext[1:], 'log_file': log_file,
        }
        bump_stat('total_runs')
        await refresh_card(cb, uid, name, ftype, page)

        await asyncio.sleep(5)           # crash check
        if process.poll() is not None:
            if key in bot_scripts:
                bot_scripts[key]['reported'] = True      # user gets the log right here, admin gets the silent report
            _reap_dead_scripts()
            tail = (read_log_tail(uid, name, 1500) or "(no output)")
            f = fid_of(name)
            await cb.message.answer(
                head("❌", "Script stopped", f"exit code {process.returncode}") +
                f"📄 <code>{e(name)}</code>\n\n<pre>{e(tail)}</pre>",
                reply_markup=kb([ib("▶️ Run again", f"file:run:{f}:{page}", style=GREEN),
                                 ib("📂 Open", f"file:open:{f}:{page}")]))
            await refresh_card(cb, uid, name, ftype, page)
    except Exception as ex:
        logger.error(f"Error running script: {ex}", exc_info=True)
        if log_file and not log_file.closed and key not in bot_scripts:
            log_file.close()
        await cb.message.answer(f"❌ <b>Error:</b> <code>{e(ex)}</code>")
    finally:
        _starting.discard(key)


@dp.callback_query(F.data.startswith("lim:"))
async def callback_limit(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "cancel":
        pending_upload.pop(uid, None)
        await present(callback, *screen_home(uid, callback.from_user.full_name))
        return
    if parts[1] != "rep":
        await cb_ok(callback)
        return
    found = find_file(uid, parts[2]) if len(parts) > 2 else None
    msg = pending_upload.pop(uid, None)
    if not found:
        await cb_ok(callback, "❌ That bot no longer exists.", alert=True)
        await safe_edit(callback.message, *screen_files(uid, 0))
        return
    if msg is None:
        await cb_ok(callback, "ℹ️ Please send your file again.", alert=True)
        await safe_edit(callback.message, *screen_upload(uid))
        return
    name = found[0]
    await cb_ok(callback, "🔁 Replacing…")
    await delete_user_file(uid, name)
    await safe_edit(callback.message, head("🔁", "Replacing", "Old bot removed") +
                    f"🗑 <code>{e(name)}</code> deleted.\n⏳ Uploading your new file…")
    await handle_document(msg)


@dp.callback_query(F.data.startswith("file:"))
async def callback_file(callback: types.CallbackQuery):
    uid = callback.from_user.id
    parts = callback.data.split(":")
    action = parts[1]
    fid = parts[2] if len(parts) > 2 else ""
    page = parts[3] if len(parts) > 3 else "0"
    found = find_file(uid, fid)
    if not found:
        await cb_ok(callback, "❌ File not found – it may have been deleted.", alert=True)
        await safe_edit(callback.message, *screen_files(uid, 0))
        return
    name, ftype = found

    if action == "open":
        s = screen_file(uid, name, ftype, page)
        if not s:
            await cb_ok(callback, "❌ File is missing on the server.", alert=True)
            await safe_edit(callback.message, *screen_files(uid, 0))
            return
        await cb_ok(callback)
        await safe_edit(callback.message, *s)

    elif action == "run":
        await do_run(callback, uid, name, ftype, page)

    elif action == "swap":
        if uid not in admin_ids and not is_premium(uid):
            for k in other_running_keys(uid, script_key_of(uid, name)):
                await stop_script_key(k)
        await do_run(callback, uid, name, ftype, page)

    elif action == "stop":
        stopped = await stop_script_key(script_key_of(uid, name))
        await cb_ok(callback, "🛑 Script stopped" if stopped else "ℹ️ Script was not running")
        if callback.message and callback.message.text and callback.message.text.startswith("📋"):
            await safe_edit(callback.message, *screen_log(uid, name, ftype, page))
        else:
            await refresh_card(callback, uid, name, ftype, page)

    elif action == "log":
        await cb_ok(callback)
        log_text, log_markup = screen_log(uid, name, ftype, page)
        await callback.message.answer(log_text, reply_markup=log_markup)

    elif action == "logr":
        await cb_ok(callback, "🔄 Refreshed")
        await safe_edit(callback.message, *screen_log(uid, name, ftype, page))

    elif action == "fav":
        favs = user_favorites.setdefault(uid, [])
        if name in favs:
            favs.remove(name)
            db_run('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (uid, name))
            await cb_ok(callback, "Removed from favorites")
        else:
            favs.append(name)
            db_run('INSERT OR IGNORE INTO favorites (user_id, file_name) VALUES (?, ?)', (uid, name))
            await cb_ok(callback, "⭐ Added to favorites")
        await refresh_card(callback, uid, name, ftype, page)

    elif action == "dl":
        path = UPLOAD_BOTS_DIR / str(uid) / name
        if not path.exists():
            await cb_ok(callback, "❌ File not found!", alert=True)
            return
        await cb_ok(callback, "📥 Sending…")
        try:
            await callback.message.answer_document(FSInputFile(path, filename=name),
                                                   caption=f"{icon_for(ftype)} <code>{e(name)}</code>")
            bump_stat('total_downloads')
        except Exception as ex:
            await callback.message.answer(f"❌ Could not send file: <code>{e(ex)}</code>")

    elif action == "del":
        running = is_running(uid, name)
        text = (head("🗑", "Delete this file?") +
                f"{icon_for(ftype)} <code>{e(name)}</code>\n\n"
                "This removes it permanently." + ("\n🛑 The running script will be stopped." if running else ""))
        await cb_ok(callback)
        await safe_edit(callback.message, text,
                        kb([ib("✅ Yes, delete", f"file:delok:{fid}:{page}", style=RED),
                            ib("❌ Cancel", f"file:open:{fid}:{page}", style=GREEN)]))

    elif action == "delok":
        await delete_user_file(uid, name)
        await cb_ok(callback, "✅ File deleted")
        await safe_edit(callback.message, *list_screen(uid, page))

    elif action == "ext":
        await do_extract(callback, uid, name, page)

    else:
        await cb_ok(callback)


# ════════════════════════════════════════════════════════════════
#  ADMIN OPERATIONS (shared by buttons and /commands)
# ════════════════════════════════════════════════════════════════
def op_add_admin(target):
    if target in admin_ids:
        return f"✅ <code>{target}</code> is already an admin."
    admin_ids.add(target)
    db_run('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (target,))
    return f"✅ <code>{target}</code> is now an <b>admin</b>."


def op_remove_admin(by, target):
    if by != OWNER_ID:
        return "❌ Only the owner can remove admins!"
    if target == OWNER_ID:
        return "❌ The owner cannot be removed!"
    if target not in admin_ids:
        return f"❌ <code>{target}</code> is not an admin."
    admin_ids.discard(target)
    db_run('DELETE FROM admins WHERE user_id = ?', (target,))
    return f"✅ <code>{target}</code> removed from admins."


def op_add_premium(target, days):
    if days <= 0:
        return "❌ Days must be greater than 0!"
    expiry = datetime.now() + timedelta(days=days)
    old_limit = user_subscriptions.get(target, {}).get('bot_limit')
    user_subscriptions[target] = {'expiry': expiry, 'bot_limit': old_limit, 'done': False}
    db_run('INSERT INTO subscriptions (user_id, expiry, expiry_done) VALUES (?, ?, 0) '
           'ON CONFLICT(user_id) DO UPDATE SET expiry = excluded.expiry, expiry_done = 0',
           (target, expiry.isoformat()))
    return (f"💎 <b>Premium added</b>\n👤 <code>{target}</code>\n⏳ {days} day(s)\n"
            f"📅 Expires: {expiry.strftime('%d %b %Y, %H:%M')}")


def op_set_bot_limit(target, n):
    d = user_subscriptions.get(target)
    if not d or d['expiry'] <= datetime.now():
        return f"❌ <code>{target}</code> has no active premium. Add premium first."
    d['bot_limit'] = n
    db_run('UPDATE subscriptions SET bot_limit = ? WHERE user_id = ?', (n, target))
    return f"✅ <code>{target}</code> can now host / run <b>{n}</b> bot(s)."


def premlimit_prompt(target):
    return (head("🤖", "Bot Limit", f"for {target}") +
            f"How many bots can <code>{target}</code> host and run?\n"
            f"Send a <b>number</b> (example: <code>5</code>).\n\n"
            f"<i>Skip = default of {PREMIUM_USER_LIMIT}.</i>",
            kb([ib(f"⏭ Skip (default {PREMIUM_USER_LIMIT})", "adm:panel", style=BLUE)]))


async def notify_premium(target):
    d = user_subscriptions.get(target)
    if not d:
        return
    try:
        await bot.send_message(target, head("💎", "Premium Activated") +
                               f"🤖 You can now host and run up to <b>{d.get('bot_limit') or PREMIUM_USER_LIMIT}</b> bots.\n"
                               f"📅 Valid until {d['expiry'].strftime('%d %b %Y')}",
                               reply_markup=kb([ib("🏠 Home", "nav:home", style=BLUE)]))
    except Exception:
        pass       # user never started the bot / blocked it


async def op_ban(target, reason):
    if target in admin_ids:
        return "❌ You cannot ban an admin!"
    banned_users.add(target)
    db_run('INSERT OR REPLACE INTO banned_users (user_id, banned_date, reason) VALUES (?, ?, ?)',
           (target, datetime.now().isoformat(), reason))
    for key in [k for k in bot_scripts if k.startswith(f"{target}_")]:
        await stop_script_key(key)
    return f"🚫 <code>{target}</code> banned.\n📝 Reason: {e(reason)}"


def op_unban(target):
    if target not in banned_users:
        return f"❌ <code>{target}</code> is not banned."
    banned_users.discard(target)
    db_run('DELETE FROM banned_users WHERE user_id = ?', (target,))
    return f"✅ <code>{target}</code> unbanned."


async def run_broadcast(status_msg, text):
    targets = [u for u in list(active_users) if u not in banned_users]
    sent = failed = 0
    body = f"📢 <b>Announcement</b>\n{HR}\n{text}"
    for i, u in enumerate(targets, 1):
        try:
            try:
                await bot.send_message(u, body)
            except TelegramBadRequest as ex:
                if "parse" in str(ex).lower():
                    await bot.send_message(u, f"📢 Announcement\n\n{text}", parse_mode=None)
                else:
                    raise
            sent += 1
        except TelegramRetryAfter as ex:
            await asyncio.sleep(min(ex.retry_after, 20))
            try:
                await bot.send_message(u, body)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
        if i % 100 == 0:
            await safe_edit(status_msg, f"📢 Broadcasting… {i}/{len(targets)}")
    return (head("✅", "Broadcast complete") +
            f"👥 Recipients: <b>{len(targets)}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>")


# ════════════════════════════════════════════════════════════════
#  ADMIN SCREENS
# ════════════════════════════════════════════════════════════════
def admin_back():
    return kb([ib("👑 Admin Panel", "adm:panel")])


def screen_admin(viewer=None):
    owner = viewer == OWNER_ID
    if not fj_channels:
        fj = "⚪ Off"
    elif fj_get("on", "1") != "1":
        fj = "⏸ Paused"
    elif _JOIN_PROBLEMS:
        fj = "⚠️ Problem – check 🔗"
    else:
        fj = f"✅ On ({len(fj_channels)})"
    _reap_dead_scripts()
    text = (head("👑", "Admin Panel", "Owner controls" if owner else "Control everything from here") +
            f"<blockquote>👥 Users: <b>{len(active_users)}</b>   🚫 Banned: <b>{len(banned_users)}</b>\n"
            f"📁 Files: <b>{sum(len(v) for v in user_files.values())}</b>   🟢 Running: <b>{running_count()}</b>\n"
            f"{'🔒 Locked' if bot_locked else '✅ Open'}" + (f"   🔗 Force-Join: {fj}" if owner else "") + "</blockquote>")
    rows = [
        [ib("📊 Analytics", "adm:analytics", style=BLUE), ib("⚙️ System", "adm:system", style=BLUE)],
        [ib("👥 Users", "adm:users"), ib("📁 All Files", "adm:files")],
        [ib("🚀 Running Scripts", "adm:running", style=GREEN)] + ([ib("💎 Premium", "adm:premium")] if owner else []),
        [ib("📢 Broadcast", "adm:ask:broadcast", style=BLUE),
         ib("🔓 Unlock Bot" if bot_locked else "🔒 Lock Bot", "adm:lock", style=GREEN if bot_locked else RED)],
        [ib("🚫 Ban", "adm:ask:ban", style=RED), ib("✅ Unban", "adm:ask:unban", style=GREEN)],
    ]
    if owner:
        rows.append([ib("➕ Add Admin", "adm:ask:addadmin"), ib("➖ Remove Admin", "adm:ask:rmadmin")])
        rows.append([ib(f"🆓 Free Limit: {free_limit()} bot(s)", "adm:ask:freelimit", style=BLUE)])
        rows.append([ib(("🐙 GitHub Deploy: ON ✅" if github_deploy_enabled() else "🐙 GitHub Deploy: OFF ❌"), "adm:gittog", style=GREEN if github_deploy_enabled() else RED)])
        rows.append([ib(("👥 GitHub Access: Premium Only 💎" if github_deploy_premium_only() else "👥 GitHub Access: Everyone 🌍"), "adm:gitaccess", style=BLUE)])
    rows.append([ib("📝 Logs", "adm:logs"), ib("💾 Backup DB", "adm:backup")])
    rows.append(([ib("🔗 Force-Join", "adm:join")] if owner else []) + [ib("🧹 Clean", "adm:clean")])
    rows.append([ib("📋 Reports", "adm:reports", style=BLUE), ib("🌐 Translator", "adm:langtest", style=BLUE)])
    if owner:
        rows.append([ib("🔄 Restart Bot", "adm:restart", style=RED)])
    rows.append([ib("🏠 Home", "nav:home")])
    return text, kb(*rows)


ASK_PROMPTS = {
    "ban": ("🚫", "Ban User", "Send:  <code>USER_ID reason</code>\nExample: <code>123456789 spam</code>"),
    "unban": ("✅", "Unban User", "Send the <code>USER_ID</code> to unban."),
    "addadmin": ("➕", "Add Admin", "Send the <code>USER_ID</code> to make admin."),
    "rmadmin": ("➖", "Remove Admin", "Send the <code>USER_ID</code> to remove (owner only)."),
    "addpremium": ("💎", "Add Premium", "Send:  <code>USER_ID DAYS</code>\nExample: <code>123456789 30</code>"),
    "fj_add": ("➕", "Add Channel",
               "Send the channel <code>@username</code> or its link.\n"
               "Private channel? Send <code>CHANNEL_ID https://t.me/+invite_link</code>\n\n"
               "⚠️ The bot must already be <b>Admin</b> in that channel."),
    "fj_text": ("✏️", "Edit Lock Message",
                "Send the new message users see when they must join.\n"
                "HTML works: <code>&lt;b&gt;bold&lt;/b&gt;</code>, <code>&lt;i&gt;italic&lt;/i&gt;</code>. "
                "Formatting from Telegram also works.\nSend <code>default</code> to restore the original."),
    "fj_media": ("🖼", "Lock Screen Media",
                 "Send ONE direct link:\n🖼 photo URL (.jpg .png)\n🎞 GIF URL (.gif)\n🎬 video URL (.mp4)\n\n"
                 "It will appear on top of the join message that new users see.\n"
                 "<i>It must be a direct file link that opens the picture itself.</i>"),
    "fj_lbl_join": ("✏️", "Join Button Text", "Send the new text for the <b>join</b> button.\nExample: <code>📢 Join Our Channel</code>"),
    "fj_lbl_check": ("✏️", "Check Button Text", "Send the new text for the <b>I've joined</b> button.\nExample: <code>✅ Done</code>"),
    "freelimit": ("🆓", "Free User Bot Limit",
                  "Send a <b>number</b>: how many bots a normal (free) user can host and run.\nExample: <code>1</code>"),
    "setlimit": ("🤖", "Change Bot Limit", "Send:  <code>USER_ID NUMBER</code>\nExample: <code>123456789 5</code>"),
    "broadcast": ("📢", "Broadcast", "Send the message you want to deliver to <b>all users</b>.\nHTML like <code>&lt;b&gt;bold&lt;/b&gt;</code> works."),
}


def screen_ask(action):
    icon, title, hint = ASK_PROMPTS[action]
    if action == "freelimit":
        hint += f"\n\nCurrent: <b>{free_limit()}</b>"
    text = head(icon, title) + hint + "\n\n✍️ <i>Type it now…</i>"
    return text, kb([ib("❌ Cancel", "adm:join" if action.startswith("fj_") else "adm:panel", style=RED)])


def screen_admin_analytics():
    now = datetime.now()
    active_p = len([u for u in user_subscriptions if user_subscriptions[u]['expiry'] > now])
    exp_p = len(user_subscriptions) - active_p
    text = (head("📊", "Analytics") +
            f"<b>Usage</b>\n📤 Uploads: {sum(len(v) for v in user_files.values())} files stored\n"
            f"📥 Downloads: {bot_stats.get('total_downloads', 0)}\n"
            f"▶️ Runs: {running_count()} running now · {bot_stats.get('total_runs', 0)} started in total\n\n"
            f"<b>Now</b>\n👥 Users: {len(active_users)}\n📁 Files: {sum(len(v) for v in user_files.values())}\n"
            f"🚀 Running: {running_count()}\n⭐ Favorites: {sum(len(v) for v in user_favorites.values())}\n\n"
            f"<b>Premium</b>\n💎 Active: {active_p}   ⌛ Expired: {exp_p}\n\n"
            f"<b>Security</b>\n🚫 Banned: {len(banned_users)}   👑 Admins: {len(admin_ids)}\n"
            f"Status: {'🔒 Locked' if bot_locked else '✅ Open'}")
    return text, kb([ib("🔄 Refresh", "adm:analytics", style=BLUE)], [ib("👑 Admin Panel", "adm:panel")])


def screen_admin_system():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage('/')
        disk_txt = f"💾 Disk: {disk.percent}% · free {disk.free / 1024**3:.1f} GB"
    except Exception:
        disk_txt = "💾 Disk: n/a"
    up = fmt_dur((datetime.now() - BOT_START_TIME).total_seconds())
    lvl = '🟢 Normal' if cpu < 70 else '🟡 High' if cpu < 90 else '🔴 Critical'
    text = (head("⚙️", "System Status") +
            f"<blockquote>💻 CPU: {cpu}%  {lvl}\n"
            f"🧠 RAM: {mem.percent}% · free {mem.available / 1024**3:.1f} GB / {mem.total / 1024**3:.1f} GB\n"
            f"{disk_txt}</blockquote>\n"
            f"🤖 Bot: {'🔒 Locked' if bot_locked else '✅ Running'}\n"
            f"🚀 Scripts: {running_count()} active\n⏳ Uptime: {up}\n"
            f"🕐 Started: {BOT_START_TIME.strftime('%d %b %Y, %I:%M %p')}")
    return text, kb([ib("🔄 Refresh", "adm:system", style=BLUE)], [ib("👑 Admin Panel", "adm:panel")])


USERS_PER_PAGE = 8


def user_link(uid):
    name, uname = user_profiles.get(uid, ("", ""))
    return f"https://t.me/{uname}" if uname else f"tg://user?id={uid}"


async def screen_admin_users(page=0):
    rows = db_run('SELECT user_id, join_date FROM active_users ORDER BY join_date DESC')
    total = len(rows)
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]
    for uid, _ in chunk:                              # users seen before names were stored: ask Telegram once
        if not user_profiles.get(uid, ("", ""))[0]:
            try:
                chat = await bot.get_chat(uid)
                full = " ".join(x for x in (chat.first_name, chat.last_name) if x) or str(uid)
                user_profiles[uid] = (full, chat.username or "")
                db_run('UPDATE active_users SET full_name = ?, username = ? WHERE user_id = ?',
                       (full, chat.username or "", uid))
            except Exception:
                pass
    now = datetime.now()
    lines, btns = [], []
    for n, (uid, joined) in enumerate(chunk, page * USERS_PER_PAGE + 1):
        name, uname = user_profiles.get(uid, ("", ""))
        name = name or f"User {uid}"
        role = role_label(uid)
        if uid in banned_users:
            role = "🚫 Banned"
        elif role == "💎 Premium":
            role += f" until {user_subscriptions[uid]['expiry'].strftime('%d %b')}"
        try:
            jd = datetime.fromisoformat(joined).strftime('%d %b %Y')
        except Exception:
            jd = "–"
        who = f'<a href="https://t.me/{uname}">@{e(uname)}</a>' if uname else "no username"
        lines.append(f'<b>{n}.</b> <a href="{user_link(uid)}">{e(name)}</a>  {role}\n'
                     f'     {who} · 🆔 <code>{uid}</code>\n'
                     f'     📁 {len(user_files.get(uid, []))} file(s) · 🟢 {running_count(uid)} running · 📅 {jd}')
        if uname:
            btns.append(ib(NO_TR + "💬 " + name[:22], url=f"https://t.me/{uname}", style=BLUE))
    text = (head("👥", "Users", f"Page {page + 1}/{pages}") +
            f"👥 Total: <b>{total}</b>   🚫 Banned: <b>{len(banned_users)}</b>   "
            f"🟢 Running now: <b>{running_count()}</b>\n"
            "Tap a name to open that user's chat 👇\n\n" + ("\n\n".join(lines) or "No users yet."))
    rows_kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page > 0:
        nav.append(ib("◀️ Prev", f"adm:users:{page - 1}", style=BLUE))
    if page < pages - 1:
        nav.append(ib("Next ▶️", f"adm:users:{page + 1}", style=BLUE))
    if nav:
        rows_kb.append(nav)
    rows_kb.append([ib("🚫 Ban", "adm:ask:ban", style=RED), ib("✅ Unban", "adm:ask:unban", style=GREEN)])
    rows_kb.append([ib("🔄 Refresh", f"adm:users:{page}", style=BLUE), ib("👑 Admin Panel", "adm:panel")])
    return text, kb(*rows_kb)


def screen_admin_files():
    allf = [f for v in user_files.values() for f in v]
    top = sorted(user_files.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    tl = "\n".join(f"• <code>{u}</code> – {len(f)} files" for u, f in top) or "–"
    text = (head("📁", "All Files") +
            f"📊 Total: <b>{len(allf)}</b>\n🐍 Python: {sum(1 for f in allf if f[1] == 'py')}\n"
            f"🟨 JavaScript: {sum(1 for f in allf if f[1] == 'js')}\n📦 ZIP: {sum(1 for f in allf if f[1] == 'zip')}\n\n"
            f"<b>Top users</b>\n{tl}")
    return text, admin_back()


def screen_admin_running():
    _reap_dead_scripts()
    if not bot_scripts:
        return head("🚀", "Running Scripts") + "💤 No scripts running right now.", \
            kb([ib("🔄 Refresh", "adm:running")], [ib("👑 Admin Panel", "adm:panel")])
    text = head("🚀", "Running Scripts", f"{running_count()} active")
    rows = []
    for key, info in bot_scripts.items():
        rt = fmt_dur((datetime.now() - info['start_time']).total_seconds())
        text += (f"🔸 <code>{e(info['file_name'])}</code>\n"
                 f"   PID {info['process'].pid} · user <code>{info['script_owner_id']}</code> · {rt}\n")
        rows.append([ib(f"🛑 Stop {info['file_name']}"[:38], f"adm:stop:{key_hash(key)}", style=RED)])
    rows += [[ib("🔄 Refresh", "adm:running", style=BLUE)], [ib("👑 Admin Panel", "adm:panel")]]
    return text, kb(*rows)


def screen_admin_premium():
    now = datetime.now()
    act = [(u, d) for u, d in user_subscriptions.items() if d['expiry'] > now]
    text = head("💎", "Premium Users", f"{len(act)} active")
    if not act:
        text += "No active premium subscriptions."
    for u, d in act[:25]:
        text += (f"💎 <code>{u}</code> · until {d['expiry'].strftime('%d %b %Y')} · "
                 f"🤖 {d.get('bot_limit') or PREMIUM_USER_LIMIT}\n")
    return text, kb([ib("➕ Add Premium", "adm:ask:addpremium", style=GREEN),
                     ib("🤖 Change Limit", "adm:ask:setlimit", style=BLUE)],
                    [ib("👑 Admin Panel", "adm:panel")])


def screen_admin_logs():
    try:
        with open(LOG_PATH, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 2800))
            tail = fh.read().decode('utf-8', errors='replace')
    except Exception as ex:
        tail = f"(cannot read log: {ex})"
    tail = tail.strip() or "(log is empty)"
    text = head("📝", "System Logs", "latest lines") + f"<pre>{e(tail)}</pre>"
    while len(text) > 3900:
        tail = tail[len(tail) // 4:]
        text = head("📝", "System Logs", "latest lines") + f"<pre>{e(tail)}</pre>"
    return text, kb([ib("🔄 Refresh", "adm:logs", style=BLUE), ib("📥 Download", "adm:logdl")],
                    [ib("👑 Admin Panel", "adm:panel")])


async def screen_admin_join():
    _join_warned_chats.clear()
    _JOIN_PROBLEMS.clear()
    on = fj_get("on", "1") == "1"
    state = "🟢 <b>ON</b>" if on else "⏸ <b>PAUSED</b>"
    media_on = fj_get("media_on") == "1" and bool(fj_get("media_url"))
    body = f"Status: {state}   📢 Channels: <b>{len(fj_channels)}</b>/{FJ_MAX_CHANNELS}\n"
    if media_on:
        body += f"🖼 Photo: 🟢 <b>ON</b> ({media_kind(fj_get('media_url'))})\n\n"
    else:
        body += "🖼 Photo: ⚪ OFF\n\n"
    if not fj_channels:
        body += ("⚪ No channel added yet – users are not blocked.\n"
                 "Tap <b>Add Channel</b> to start. (Make the bot <b>Admin</b> in the channel first.)")
    else:
        try:
            me = await bot.get_me()
        except Exception:
            me = None
        for i, c in enumerate(fj_channels, 1):
            try:
                m = await bot.get_chat_member(_chat_ref(c), me.id)
                st = str(getattr(m.status, "value", m.status))
                ok = st in ("administrator", "creator")
                line = f"{i}. {'✅' if ok else '❌ bot is not admin'} <code>{e(c['title'])}</code>\n    🆔 <code>{e(c['chat'])}</code>"
            except Exception as ex:
                line = f"{i}. ❌ <code>{e(c['title'])}</code>\n    <code>{e(str(ex)[:100])}</code>"
            body += line + "\n"
        body += "\nUsers must join <b>all</b> channels above."
    rows = [[ib("➕ Add Channel", "adm:ask:fj_add", style=GREEN)]]
    for i, c in enumerate(fj_channels, 1):
        rows.append([ib(f"🗑 Remove {i}", f"adm:fjdel:{c['id']}", style=RED)])
    rows += [
        [ib("✏️ Edit Message", "adm:ask:fj_text", style=BLUE), ib("🎨 Button Colors", "adm:fjcol", style=BLUE)],
        [ib("✏️ Join Text", "adm:ask:fj_lbl_join"), ib("✏️ Check Text", "adm:ask:fj_lbl_check")],
        ([ib("🖼 Photo: ON ✅ (tap = OFF)", "adm:fjmediaoff", style=GREEN), ib("✏️ Change", "adm:ask:fj_media", style=BLUE)]
         if media_on else [ib("🖼 Photo: OFF (tap = ON)", "adm:ask:fj_media", style=RED)]),
        [ib("👁 Preview", "adm:fjprev", style=BLUE),
         ib("⏸ Turn OFF" if on else "▶️ Turn ON", "adm:fjtog", style=RED if on else GREEN)],
        [ib("♻️ Reset Look", "adm:fjreset", style=RED), ib("🔄 Re-check", "adm:join", style=BLUE)],
        [ib("👑 Admin Panel", "adm:panel")],
    ]
    return head("🔗", "Force-Join") + body, kb(*rows)


def screen_fj_colors():
    j, c = fj_color("join"), fj_color("check")

    def row(which, cur):
        return [ib(("✅ " if col == cur else "") + nm, f"adm:fjc:{which}:{col}", style=col)
                for col, nm in COLOR_NAMES.items()]
    text = (head("🎨", "Button Colors", "Pick a color for each button") +
            "The two big buttons below show the current colors.\nTap a color to change it 👇")
    rows = [[ib(fj_get("lbl_join", DEFAULT_LBL_JOIN)[:40], "nav:noop", style=j)], row("join", j),
            [ib(fj_get("lbl_check", DEFAULT_LBL_CHECK)[:40], "nav:noop", style=c)], row("check", c),
            [ib("◀️ Back", "adm:join", style=BLUE)]]
    return text, kb(*rows)


def _admin_html(message):
    """Text the admin typed. Uses Telegram's own formatting when it was used, otherwise the raw text (so typed HTML works)."""
    fmt = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link", "blockquote"}
    ents = message.entities or []
    if any(str(getattr(x.type, "value", x.type)) in fmt for x in ents):
        return message.html_text.strip()
    return (message.text or "").strip()


async def handle_fj_input(message, action, text, bad):
    done = kb([ib("🔗 Force-Join", "adm:join", style=BLUE)])
    if action == "fj_add":
        tokens = text.split()
        if not tokens:
            return await bad("Send a channel @username, link or id.")
        ref, link = tokens[0], (tokens[1] if len(tokens) > 1 else None)
        if ref.startswith(("http://", "https://", "t.me/", "telegram.me/")):
            tail = ref.split("?")[0].rstrip("/").split("/")[-1]
            if tail.startswith("+") or "joinchat" in ref:
                return await bad("A private invite link does not contain the channel id.\n"
                                 "Send: <code>-100xxxxxxxxxx https://t.me/+invite_link</code>")
            ref = "@" + tail
        elif ref.lstrip("-").isdigit():
            ref = int(ref)
        elif not ref.startswith("@"):
            ref = "@" + ref
        try:
            chat = await bot.get_chat(ref)
            me = await bot.get_me()
            m = await bot.get_chat_member(chat.id, me.id)
        except Exception as ex:
            return await bad(f"Cannot open that channel: <code>{e(ex)}</code>\nCheck the username / id and add the bot to the channel.")
        st = str(getattr(m.status, "value", m.status))
        if st not in ("administrator", "creator"):
            return await bad(f"The bot is not <b>Admin</b> in <code>{e(chat.title)}</code>.\nMake it admin, then send again.")
        if str(chat.id) in {c['chat'] for c in fj_channels}:
            return await bad("That channel is already added.")
        if len(fj_channels) >= FJ_MAX_CHANNELS:
            return await bad(f"Maximum {FJ_MAX_CHANNELS} channels allowed. Remove one first.")
        url = None
        if link:
            url = _normalize_url(link)
        elif getattr(chat, "username", None):
            url = f"https://t.me/{chat.username}"
        else:
            try:
                url = await bot.export_chat_invite_link(chat.id)
            except Exception:
                url = None
        if not url or not url.startswith("https://"):
            return await bad("This channel is private, so I need its invite link.\n"
                             "Send: <code>CHANNEL_ID https://t.me/+invite_link</code>")
        title = chat.title or str(chat.id)
        fj_add_channel(chat.id, title, url)
        await message.answer(head("✅", "Channel added") +
                             f"📢 <code>{e(title)}</code>\n🆔 <code>{chat.id}</code>\n🔗 {e(url)}\n\n"
                             "Users must now join every channel in the list to use the bot.", reply_markup=done)
    elif action == "fj_text":
        val = _admin_html(message)
        if val.lower() == "default":
            fj_del("text")
            await message.answer("✅ Original message restored.", reply_markup=done)
            return
        if len(val) > 3500:
            return await bad("Message is too long (max 3500 characters).")
        try:                                   # preview doubles as an HTML check
            await message.answer(val, reply_markup=join_kb(fj_channels or
                                 [{'title': 'Sample Channel', 'url': UPDATE_CHANNEL_URL, 'chat': 'x', 'id': 0}]))
        except Exception as ex:
            return await bad(f"That formatting is not valid: <code>{e(ex)}</code>")
        fj_put("text", val)
        await message.answer("✅ Saved! This is how users will see it (preview above).", reply_markup=done)
    elif action == "fj_media":
        url = text.split()[0] if text.split() else ""
        if not re.match(r'https?://\S+$', url):
            return await bad("Send a link that starts with <code>https://</code>")
        try:                                             # the preview proves Telegram can load it
            await _send_media(message.chat.id, url, caption="🖼 Preview – this is what new users will see on top.")
        except Exception as ex:
            return await bad("Telegram could not load that link:\n"
                             f"<code>{e(str(ex)[:150])}</code>\n"
                             "Use a <b>direct</b> link to the file (ends with .jpg .png .gif .mp4).")
        fj_put("media_url", url)
        fj_put("media_on", "1")
        await message.answer(f"✅ Photo is <b>ON</b> ({media_kind(url)}). New users will see it above the join message.",
                             reply_markup=done)
    elif action in ("fj_lbl_join", "fj_lbl_check"):
        val = text[:40].strip()
        if not val:
            return await bad("Button text cannot be empty.")
        fj_put("lbl_join" if action == "fj_lbl_join" else "lbl_check", val)
        await message.answer(f"✅ Button text saved: <code>{e(val)}</code>", reply_markup=done)


CLEAN_INFO = {
    "logs": ("📝", "Delete old log files of stopped scripts"),
    "banned": ("🚫", "Delete all files of banned users"),
    "old": ("📅", "Delete files older than 30 days (admins' files are kept)"),
}


def screen_admin_clean():
    text = head("🧹", "Clean Files", "Free up disk space") + "Pick what to clean 👇"
    rows = [[ib(f"{ic} {desc}"[:45], f"adm:clean:{k}")] for k, (ic, desc) in CLEAN_INFO.items()]
    rows.append([ib("👑 Admin Panel", "adm:panel")])
    return text, kb(*rows)


async def run_clean(kind):
    count = 0
    running_stems = {(str(i['script_owner_id']), Path(i['file_name']).stem) for i in bot_scripts.values()}
    if kind == "logs":
        for folder in UPLOAD_BOTS_DIR.iterdir():
            if folder.is_dir():
                for lg in folder.glob("*.log"):
                    if (folder.name, lg.stem) not in running_stems:
                        lg.unlink(missing_ok=True)
                        count += 1
    elif kind == "banned":
        for uid in list(banned_users):
            for key in [k for k in bot_scripts if k.startswith(f"{uid}_")]:
                await stop_script_key(key)
            folder = UPLOAD_BOTS_DIR / str(uid)
            if folder.exists():
                count += sum(1 for p in folder.iterdir() if p.is_file())
                shutil.rmtree(folder, ignore_errors=True)
            user_files.pop(uid, None)
            user_favorites.pop(uid, None)
            db_run('DELETE FROM user_files WHERE user_id = ?', (uid,))
            db_run('DELETE FROM favorites WHERE user_id = ?', (uid,))
    elif kind == "old":
        limit = datetime.now() - timedelta(days=30)
        for uid, fname, up in db_run('SELECT user_id, file_name, upload_date FROM user_files WHERE upload_date IS NOT NULL'):
            if uid in admin_ids or script_key_of(uid, fname) in bot_scripts:
                continue
            try:
                if datetime.fromisoformat(up) >= limit:
                    continue
            except (ValueError, TypeError):
                continue
            (UPLOAD_BOTS_DIR / str(uid) / fname).unlink(missing_ok=True)
            user_files[uid] = [f for f in user_files.get(uid, []) if f[0] != fname]
            if fname in user_favorites.get(uid, []):
                user_favorites[uid].remove(fname)
            db_run('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (uid, fname))
            db_run('DELETE FROM favorites WHERE user_id = ? AND file_name = ?', (uid, fname))
            count += 1
    return count


# ════════════════════════════════════════════════════════════════
#  ADMIN HANDLERS
# ════════════════════════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Admin only!")
        return
    await present(message, *screen_admin(message.from_user.id))


@dp.message(F.text == BTN_ADMIN)
async def menu_admin(message: types.Message):
    pending_input.pop(message.from_user.id, None)
    if message.from_user.id not in admin_ids:
        await message.answer("❌ Admin only!")
        return
    await present(message, *screen_admin(message.from_user.id))


@dp.callback_query(F.data.startswith("adm:"))
async def callback_admin(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if uid not in admin_ids:
        await cb_ok(callback, "❌ Admin only!", alert=True)
        return
    global bot_locked
    parts = callback.data.split(":")
    sub = parts[1]
    if sub != "ask":
        pending_input.pop(uid, None)

    if uid != OWNER_ID and (sub in OWNER_ONLY_SUBS or (sub == "ask" and len(parts) > 2 and parts[2] in OWNER_ONLY_ACTIONS)):
        await cb_ok(callback, "👑 Owner only!", alert=True)
        return

    if sub == "panel":
        await present(callback, *screen_admin(callback.from_user.id))
    elif sub == "analytics":
        await present(callback, *screen_admin_analytics())
    elif sub == "system":
        await present(callback, *screen_admin_system())
    elif sub == "users":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await cb_ok(callback)
        await safe_edit(callback.message, *(await screen_admin_users(page)))
    elif sub == "files":
        await present(callback, *screen_admin_files())
    elif sub == "running":
        await present(callback, *screen_admin_running())
    elif sub == "premium":
        await present(callback, *screen_admin_premium())
    elif sub == "gittog":
        if uid != OWNER_ID:
            await cb_ok(callback, "👑 Owner only!", alert=True)
            return
        set_github_deploy(not github_deploy_enabled())
        await cb_ok(callback, "🐙 GitHub Deploy " + ("enabled" if github_deploy_enabled() else "disabled"))
        await safe_edit(callback.message, *(screen_admin(uid)))
    elif sub == "gitaccess":
        if uid != OWNER_ID:
            await cb_ok(callback, "👑 Owner only!", alert=True)
            return
        set_github_deploy_access(not github_deploy_premium_only())
        mode = "Premium users only" if github_deploy_premium_only() else "Everyone"
        await cb_ok(callback, "👥 GitHub Access: " + mode)
        await safe_edit(callback.message, *(screen_admin(uid)))
    elif sub == "logs":
        await present(callback, *screen_admin_logs())
    elif sub == "logdl":
        await cb_ok(callback, "📥 Sending…")
        try:
            await callback.message.answer_document(FSInputFile(LOG_PATH, filename="bot.log"),
                                                   caption="📝 <b>Bot log</b>")
        except Exception as ex:
            await callback.message.answer(f"❌ Cannot send log: <code>{e(ex)}</code>")
    elif sub == "join":
        await cb_ok(callback, "🔍 Checking…")
        await safe_edit(callback.message, *(await screen_admin_join()))
    elif sub == "langtest":
        await cb_ok(callback, "🌐 Testing…")
        await safe_edit(callback.message, *(await screen_langtest("ta")))
    elif sub == "reports":
        await present(callback, *screen_reports())
    elif sub == "fjdel":
        fj_remove_channel(int(parts[2]))
        await cb_ok(callback, "🗑 Channel removed")
        await safe_edit(callback.message, *(await screen_admin_join()))
    elif sub == "fjmediaoff":
        fj_put("media_on", "0")
        await cb_ok(callback, "🖼 Photo turned OFF")
        await safe_edit(callback.message, *(await screen_admin_join()))
    elif sub == "fjtog":
        fj_put("on", "0" if fj_get("on", "1") == "1" else "1")
        _SUB_CACHE.clear()
        await cb_ok(callback, "✅ Done")
        await safe_edit(callback.message, *(await screen_admin_join()))
    elif sub == "fjcol":
        await present(callback, *screen_fj_colors())
    elif sub == "fjc":
        if parts[2] in ("join", "check") and parts[3] in COLOR_NAMES:
            fj_put("color_" + parts[2], parts[3])
            await cb_ok(callback, "🎨 Color saved")
        await safe_edit(callback.message, *screen_fj_colors())
    elif sub == "fjprev":
        await cb_ok(callback, "👁 Preview sent")
        demo = fj_channels or [{'title': 'Sample Channel', 'url': UPDATE_CHANNEL_URL, 'chat': 'x', 'id': 0}]
        await send_lock(callback.message.chat.id, demo)
    elif sub == "fjreset":
        for k in ("text", "lbl_join", "lbl_check", "color_join", "color_check", "media_on", "media_url"):
            fj_del(k)
        await cb_ok(callback, "♻️ Look reset to default")
        await safe_edit(callback.message, *(await screen_admin_join()))
    elif sub == "ask":
        action = parts[2]
        if action == "rmadmin" and uid != OWNER_ID:
            await cb_ok(callback, "❌ Only the owner can remove admins!", alert=True)
            return
        pending_input[uid] = action
        await present(callback, *screen_ask(action))
    elif sub == "stop":
        key = next((k for k in bot_scripts if key_hash(k) == parts[2]), None)
        stopped = await stop_script_key(key) if key else False
        await cb_ok(callback, "🛑 Stopped" if stopped else "ℹ️ Already stopped")
        await safe_edit(callback.message, *screen_admin_running())
    elif sub == "lock":
        bot_locked = not bot_locked
        await cb_ok(callback, "🔒 Bot is now LOCKED" if bot_locked else "🔓 Bot is now UNLOCKED", alert=True)
        await safe_edit(callback.message, *screen_admin(callback.from_user.id))
    elif sub == "backup":
        try:
            backup_path = IROTECH_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            conn = sqlite3.connect(DATABASE_PATH)
            bconn = sqlite3.connect(backup_path)
            conn.backup(bconn)
            bconn.close()
            conn.close()
            await cb_ok(callback, "✅ Database backed up!")
            await callback.message.answer_document(
                FSInputFile(backup_path),
                caption="💾 <b>Database Backup</b>\n" + datetime.now().strftime('%d %b %Y, %H:%M:%S'))
            backup_path.unlink(missing_ok=True)
        except Exception as ex:
            logger.error(f"Backup error: {ex}")
            await cb_ok(callback, f"❌ Backup failed: {ex}", alert=True)
    elif sub == "clean":
        if len(parts) == 2:
            await present(callback, *screen_admin_clean())
        else:
            kind = parts[2]
            ic, desc = CLEAN_INFO[kind]
            await present(callback,
                          head("⚠️", "Are you sure?") + f"{ic} {desc}\n\n<b>This cannot be undone.</b>",
                          kb([ib("✅ Yes, clean", f"adm:cleanok:{kind}", style=RED),
                              ib("❌ Cancel", "adm:clean", style=GREEN)]))
    elif sub == "cleanok":
        n = await run_clean(parts[2])
        await cb_ok(callback, f"✅ Done · {n} item(s)")
        await safe_edit(callback.message, head("✅", "Cleaned") + f"🧹 Removed <b>{n}</b> item(s).",
                        kb([ib("🧹 Clean more", "adm:clean"), ib("👑 Admin Panel", "adm:panel")]))
    elif sub == "restart":
        if uid != OWNER_ID:
            await cb_ok(callback, "❌ Owner only!", alert=True)
            return
        await present(callback,
                      head("🔄", "Restart bot?") +
                      "⚠️ All running scripts will be stopped.\nUsers may see a short downtime.",
                      kb([ib("✅ Restart now", "adm:restartok", style=RED),
                          ib("❌ Cancel", "adm:panel", style=GREEN)]))
    elif sub == "restartok":
        if uid != OWNER_ID:
            await cb_ok(callback, "❌ Owner only!", alert=True)
            return
        await cb_ok(callback, "🔄 Restarting…")
        await safe_edit(callback.message, "🔄 <b>Restarting…</b>\nBack in a few seconds.")
        await do_restart()
    else:
        await cb_ok(callback)


async def do_restart():
    for key in list(bot_scripts.keys()):
        await stop_script_key(key)
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@dp.message(Command("restart"))
async def cmd_restart(message: types.Message):
    if message.from_user.id != OWNER_ID:
        await message.answer("❌ Owner only!")
        return
    await present(message, head("🔄", "Restart bot?") +
                  "⚠️ All running scripts will be stopped.",
                  kb([ib("✅ Restart now", "adm:restartok", style=RED), ib("❌ Cancel", "adm:panel", style=GREEN)]))


# ── admin /commands (still work, same logic as the buttons)
def _is_admin(m):
    return m.from_user.id in admin_ids


@dp.message(Command("addadmin"))
async def cmd_add_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("👑 Only the owner can do this.")
    args = (message.text or "").split()
    if len(args) != 2 or not args[1].lstrip('-').isdigit():
        return await message.answer("Usage: <code>/addadmin USER_ID</code>")
    await message.answer(op_add_admin(int(args[1])))


@dp.message(Command("removeadmin"))
async def cmd_remove_admin(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("👑 Only the owner can do this.")
    args = (message.text or "").split()
    if len(args) != 2 or not args[1].lstrip('-').isdigit():
        return await message.answer("Usage: <code>/removeadmin USER_ID</code>")
    await message.answer(op_remove_admin(message.from_user.id, int(args[1])))


@dp.message(Command("addpremium"))
async def cmd_add_premium(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("👑 Only the owner can do this.")
    args = (message.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit():
        return await message.answer("Usage: <code>/addpremium USER_ID DAYS</code>")
    target = int(args[1])
    await message.answer(op_add_premium(target, int(args[2])))
    pending_input[message.from_user.id] = f"premlimit:{target}"
    txt, mk = premlimit_prompt(target)
    await message.answer(txt, reply_markup=mk)


@dp.message(Command("freelimit"))
async def cmd_free_limit(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("👑 Only the owner can do this.")
    args = (message.text or "").split()
    if len(args) != 2 or not args[1].isdigit() or not (1 <= int(args[1]) <= 1000):
        return await message.answer(f"Free users can host <b>{free_limit()}</b> bot(s) now.\n"
                                    "Change it: <code>/freelimit NUMBER</code>")
    set_free_limit(int(args[1]))
    await message.answer(f"✅ Free users can now host and run <b>{free_limit()}</b> bot(s).")


@dp.message(Command("setlimit"))
async def cmd_set_limit(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("👑 Only the owner can do this.")
    args = (message.text or "").split()
    if len(args) != 3 or not args[1].isdigit() or not args[2].isdigit() or int(args[2]) < 1:
        return await message.answer("Usage: <code>/setlimit USER_ID NUMBER</code>")
    await message.answer(op_set_bot_limit(int(args[1]), int(args[2])))
    await notify_premium(int(args[1]))


@dp.message(Command("ban"))
async def cmd_ban_user(message: types.Message):
    if not _is_admin(message):
        return await message.answer("❌ Permission denied!")
    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: <code>/ban USER_ID [reason]</code>")
    await message.answer(await op_ban(int(args[1]), args[2] if len(args) > 2 else "No reason provided"))


@dp.message(Command("unban"))
async def cmd_unban_user(message: types.Message):
    if not _is_admin(message):
        return await message.answer("❌ Permission denied!")
    args = (message.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("Usage: <code>/unban USER_ID</code>")
    await message.answer(op_unban(int(args[1])))


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if not _is_admin(message):
        return await message.answer("❌ Permission denied!")
    text = (message.text or "").replace("/broadcast", "", 1).strip()
    if not text:
        pending_input[message.from_user.id] = "broadcast"
        return await present(message, *screen_ask("broadcast"))
    status = await message.answer("📢 Broadcasting…")
    await safe_edit(status, await run_broadcast(status, text), admin_back())


# ════════════════════════════════════════════════════════════════
#  TEXT INPUT (search / admin prompts) + FALLBACKS  – must stay LAST
# ════════════════════════════════════════════════════════════════
def _download_github_zip(url: str, dest: Path):
    """Download a public GitHub repository with user-friendly, precise errors."""
    raw = (url or "").strip()
    parsed = urlparse(raw)

    if not raw:
        raise ValueError(
            "GitHub URL is empty.\n\n"
            "Please send a public repository URL, for example:\n"
            "https://github.com/username/project"
        )

    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError(
            "Invalid GitHub URL.\n\n"
            "Please send a GitHub repository URL in this format:\n"
            "https://github.com/username/project"
        )

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            "Invalid GitHub repository URL.\n\n"
            "A repository URL must include both the username and repository name.\n"
            "Example: https://github.com/username/project"
        )

    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub owner or repository name.")

    # Ask GitHub for the repository metadata first. This gives us the real
    # default branch instead of guessing main/master and makes errors precise.
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        req = Request(api_url, headers={
            "User-Agent": "Telegram-Bot-Hoster",
            "Accept": "application/vnd.github+json",
        })
        with urlopen(req, timeout=20) as r:
            meta = json.loads(r.read().decode("utf-8"))
        branch = meta.get("default_branch")
        if not branch:
            raise ValueError("GitHub did not return a default branch for this repository.")
    except HTTPError as ex:
        if ex.code == 404:
            raise ValueError(
                f"Repository not found: {owner}/{repo}.\n\n"
                "The repository may not exist, the URL may be incorrect, or it may be private.\n"
                "Only public GitHub repositories are supported."
            ) from ex
        if ex.code == 403:
            raise ValueError(
                "GitHub denied access to this repository.\n\n"
                "This can happen because of GitHub rate limits or repository access restrictions.\n"
                "Please try again later or use a public repository."
            ) from ex
        raise ValueError(f"GitHub returned HTTP {ex.code} while checking the repository.") from ex
    except URLError as ex:
        raise ValueError(
            "Could not connect to GitHub.\n\n"
            "Please check the server's internet connection and try again."
        ) from ex
    except TimeoutError as ex:
        raise ValueError("GitHub connection timed out while checking the repository. Please try again.") from ex
    except Exception as ex:
        raise ValueError(f"Could not read GitHub repository information: {ex}") from ex

    download_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{quote(branch, safe='')}.zip"
    try:
        req = Request(download_url, headers={"User-Agent": "Telegram-Bot-Hoster"})
        with urlopen(req, timeout=60) as r, open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
    except HTTPError as ex:
        if ex.code == 404:
            raise ValueError(
                f"GitHub found {owner}/{repo}, but its default branch could not be downloaded.\n\n"
                f"Default branch: {branch}"
            ) from ex
        if ex.code == 403:
            raise ValueError("GitHub refused the repository download (HTTP 403). The repository may be private or rate-limited.") from ex
        raise ValueError(f"GitHub download failed with HTTP {ex.code}.") from ex
    except URLError as ex:
        raise ValueError("The repository download could not connect to GitHub. Please try again.") from ex
    except TimeoutError as ex:
        raise ValueError("The GitHub download timed out after 60 seconds. Please try again.") from ex
    except OSError as ex:
        raise ValueError(f"The server could not save the downloaded repository: {ex}") from ex

    if not dest.exists() or dest.stat().st_size == 0:
        raise ValueError("GitHub returned an empty download. The repository could not be imported.")
    if not zipfile.is_zipfile(dest):
        raise ValueError("GitHub download was not a valid ZIP archive. The repository could not be imported.")

    return owner, repo, branch


async def deploy_github_repo(message: types.Message, url: str):
    uid = message.from_user.id
    status = await message.answer(head("🐙", "GitHub Deploy", "Downloading repository…"))
    folder = user_dir(uid)
    temp_zip = folder / ".github_deploy.zip"
    try:
        owner, repo, branch = await asyncio.to_thread(_download_github_zip, url.strip(), temp_zip)
        await safe_edit(status, head("📦", "GitHub Repository Downloaded") +
                        f"👤 <code>{e(owner)}</code> / <code>{e(repo)}</code>\n🌿 Branch: <code>{e(branch)}</code>\n\n🔍 Extracting and finding the main Python file…")
        total, rels = await asyncio.to_thread(_extract_zip_sync, temp_zip, folder)
        temp_zip.unlink(missing_ok=True)
        pyrels = [r for r in rels if str(r).lower().endswith(".py")]
        if not pyrels:
            raise ValueError("No Python file was found in this repository.")
        preferred = [r for r in pyrels if Path(r).name.lower() in ("main.py", "bot.py", "app.py")]
        main_rel = preferred[0] if preferred else pyrels[0]
        main_path = folder / main_rel
        # Register extracted top-level files/scripts so they appear in My Files.
        registered = set(n for n, _ in user_files.get(uid, []))
        for rel in pyrels:
            p = folder / rel
            if p.exists() and p.name not in registered:
                user_files.setdefault(uid, []).append((p.name, p.suffix.lower().lstrip(".")))
                db_run('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, upload_date) VALUES (?, ?, ?, ?)',
                       (uid, p.name, p.suffix.lower().lstrip("."), datetime.now().isoformat()))
        await safe_edit(status, head("⚙️", "Preparing GitHub Bot", "Installing dependencies…") +
                        f"📄 Main file: <code>{e(main_path.name)}</code>\n📦 Files: <b>{total}</b>")
        log_path = folder / f"{main_path.stem}.log"
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            await asyncio.to_thread(_install_dependencies, main_path, main_path.parent, lf)
        log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        key = script_key_of(uid, main_path.name)
        process = subprocess.Popen([sys.executable, "-u", str(main_path)], cwd=str(main_path.parent),
                                   stdout=log_file, stderr=subprocess.STDOUT, env=_clean_child_env())
        bot_scripts[key] = {'process': process, 'file_name': main_path.name, 'script_owner_id': uid,
                            'start_time': datetime.now(), 'user_folder': str(main_path.parent),
                            'type': 'py', 'log_file': log_file}
        bump_stat('total_runs')
        await safe_edit(status, head("🚀", "GitHub Bot Started", "Deployment complete") +
                        f"📄 <code>{e(main_path.name)}</code>\n👤 <code>{e(owner)}/{e(repo)}</code>\n🌿 Branch: <code>{e(branch)}</code>\n\n🟢 Status: <b>Running</b>",
                        kb([ib("📋 Output", f"file:log:{fid_of(main_path.name)}:0", style=BLUE),
                            ib("📁 My Files", "nav:files:0")], [ib("🏠 Home", "nav:home")]))
    except Exception as ex:
        temp_zip.unlink(missing_ok=True)
        logger.error(f"GitHub deploy failed for {uid}: {ex}", exc_info=True)
        await safe_edit(status, head("❌", "GitHub Deploy Failed") +
                        "<b>What went wrong:</b>\n" + f"<code>{e(ex)}</code>\n\n"
                        "<b>What to check:</b>\n"
                        "• Make sure the repository URL is correct.\n"
                        "• Make sure the repository is public.\n"
                        "• If GitHub is rate-limiting requests, try again later.\n"
                        "• If the download succeeded but no Python file was found, add a .py file to the repository.",
                        kb([ib("🐙 Try Again", "nav:home", style=BLUE), ib("🏠 Home", "nav:home")]))


async def handle_pending(message: types.Message, action: str):
    uid = message.from_user.id
    text = (message.text or "").strip()

    if action == "search":
        await present(message, *screen_search_results(uid, text))
        return

    if action == "github_deploy":
        if not github_deploy_enabled():
            await message.answer("⚪ GitHub deployment is currently disabled by the owner.")
            return
        if github_deploy_premium_only() and uid != OWNER_ID and not is_premium(uid):
            await message.answer("💎 GitHub deployment is available for Premium users only.")
            return
        await deploy_github_repo(message, text)
        return

    if uid not in admin_ids:
        return
    if uid != OWNER_ID and owner_only_action(action):
        await message.answer("👑 Only the owner can do this.")
        return

    async def bad(msg):
        pending_input[uid] = action          # keep waiting for a valid answer
        back = "adm:join" if action.startswith("fj_") else "adm:panel"
        await message.answer(f"❌ {msg}\nTry again or tap Cancel.", reply_markup=kb([ib("❌ Cancel", back, style=RED)]))

    if action.startswith("fj_"):
        try:
            await handle_fj_input(message, action, text, bad)
        except Exception as ex:
            logger.error(f"force-join admin action {action} failed: {ex}", exc_info=True)
            await message.answer(f"❌ Error: <code>{e(ex)}</code>", reply_markup=admin_back())
        return

    if action == "freelimit":
        if not text.isdigit() or not (1 <= int(text) <= 1000):
            return await bad("Send a number between 1 and 1000.")
        set_free_limit(int(text))
        await message.answer(f"✅ Free users can now host and run <b>{free_limit()}</b> bot(s).\n"
                             "Users who already have more keep their files, but cannot upload more.",
                             reply_markup=kb([ib("👑 Admin Panel", "adm:panel", style=BLUE)]))
        return

    if action.startswith("premlimit:"):
        target = int(action.split(":", 1)[1])
        if not text.isdigit() or not (1 <= int(text) <= 1000):
            return await bad("Send a number between 1 and 1000.")
        await message.answer(op_set_bot_limit(target, int(text)), reply_markup=admin_back())
        await notify_premium(target)
        return

    parts = text.split(maxsplit=1)
    ask_limit = None
    try:
        if action == "broadcast":
            status = await message.answer("📢 Broadcasting…")
            await safe_edit(status, await run_broadcast(status, text), admin_back())
            return
        if not parts or not parts[0].lstrip('-').isdigit():
            return await bad("Please send a numeric USER_ID.")
        target = int(parts[0])
        if action == "ban":
            res = await op_ban(target, parts[1] if len(parts) > 1 else "No reason provided")
        elif action == "unban":
            res = op_unban(target)
        elif action == "addadmin":
            res = op_add_admin(target)
        elif action == "rmadmin":
            res = op_remove_admin(uid, target)
        elif action == "addpremium":
            if len(parts) < 2 or not parts[1].strip().isdigit():
                return await bad("Send  USER_ID DAYS  (example: 123456789 30).")
            res = op_add_premium(target, int(parts[1].strip()))
            ask_limit = target
        elif action == "setlimit":
            if len(parts) < 2 or not parts[1].strip().isdigit() or int(parts[1].strip()) < 1:
                return await bad("Send  USER_ID NUMBER  (example: 123456789 5).")
            res = op_set_bot_limit(target, int(parts[1].strip()))
            if res.startswith("✅"):
                await notify_premium(target)
        else:
            return
        if ask_limit:
            await message.answer(res)
            pending_input[uid] = f"premlimit:{ask_limit}"
            txt, mk = premlimit_prompt(ask_limit)
            await message.answer(txt, reply_markup=mk)
            return
        await message.answer(res, reply_markup=admin_back())
    except Exception as ex:
        logger.error(f"admin action {action} failed: {ex}", exc_info=True)
        await message.answer(f"❌ Error: <code>{e(ex)}</code>", reply_markup=admin_back())


@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(message: types.Message):
    uid = message.from_user.id
    action = pending_input.pop(uid, None)
    if action:
        await handle_pending(message, action)
        return
    await message.answer(
        "🤔 I didn't understand that.\n\nUse the <b>menu buttons</b> below, or send a "
        "<code>.py</code> / <code>.js</code> / <code>.zip</code> file to upload.",
        reply_markup=reply_menu(uid))


@dp.message(F.text.startswith("/"))
async def on_unknown_command(message: types.Message):
    await message.answer("❓ Unknown command. Use /help or the menu buttons below.",
                         reply_markup=reply_menu(message.from_user.id))


@dp.message()
async def on_other(message: types.Message):
    await message.answer(
        head("📎", "Send it as a File") +
        "To upload code, attach it as a <b>file</b> (📎 → File), not as a photo or video.\n"
        "Supported: <code>.py</code> <code>.js</code> <code>.zip</code>",
        reply_markup=reply_menu(message.from_user.id))


async def on_error(event: types.ErrorEvent):
    logger.error(f"Unhandled error: {event.exception}", exc_info=event.exception)
    cq = event.update.callback_query
    if cq:
        try:
            await cq.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass
    return True


dp.errors.register(on_error)


# ════════════════════════════════════════════════════════════════
#  WEB SERVER + MAIN
# ════════════════════════════════════════════════════════════════
async def web_server():
    app = web.Application()

    async def handle(request):
        return web.Response(text="🚀 Advanced File Host Bot - Powered by Aiogram & Aiohttp!")

    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv('SERVER_PORT') or os.getenv('PORT') or 5000)
    try:
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"🌐 Web server started on port {port}")
    except OSError as ex:
        logger.warning(f"Web server could not start on port {port}: {ex}")


async def main():
    logger.info("🚀 Starting Advanced File Host Bot...")
    # Start the Render health/web server before any Telegram network call.
    # This guarantees that Render detects the PORT even if Telegram is slow to respond.
    web_task = asyncio.create_task(web_server())
    await asyncio.sleep(0)
    try:
        me = await bot.get_me()
        logger.info(f"✅ Telegram accepted the token – running as @{me.username}")
    except Exception as ex:
        if "unauthorized" in str(ex).lower():
            logger.error("❌ BOT_TOKEN REJECTED by Telegram (Unauthorized). The token in .env is wrong, revoked "
                         "or belongs to a deleted bot. Open @BotFather -> /mybots -> your bot -> API Token "
                         "(Revoke current token if needed), paste the NEW token into .env as BOT_TOKEN=..., "
                         "save, and start again.")
            raise SystemExit(1)
        logger.warning(f"get_me failed (will keep trying): {ex}")
    watch_task = asyncio.create_task(background_watcher())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as ex:
        logger.warning(f"delete_webhook failed: {ex}")
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Home menu"),
            BotCommand(command="files", description="📁 My files"),
            BotCommand(command="search", description="🔍 Search my files"),
            BotCommand(command="stats", description="📊 My statistics"),
            BotCommand(command="premium", description="💎 Premium info"),
            BotCommand(command="help", description="ℹ️ Help"),
            BotCommand(command="language", description="🌐 Language"),
        ])
    except Exception as ex:
        logger.warning(f"set_my_commands failed: {ex}")
    logger.info(f"Force-join channel: {FORCE_JOIN_CHAT}")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        web_task.cancel()
        watch_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

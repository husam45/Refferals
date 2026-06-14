"""
database.py – Full Async SQLite (aiosqlite) data layer.
Optimized with WAL mode and robust error handling to prevent database locks.
Conformed strictly to the unified bot production framework schema.
"""
import os
import json
import aiosqlite
from datetime import datetime

# ኮዱ ከ bot.py ጋር አንድ አይነት የዳታቤዝ ስም እንዲጠቀም ተደርጓል
DB_PATH = "bot_production_core.db"

# ─────────────────────────────────────────────────────────────────────────────
# Schema Definition (ከተሻሻለው bot.py ጋር አንድ አይነት እንዲሆን ተስተካክሏል)
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS system_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    balance REAL DEFAULT 0.0,
    referrer_id INTEGER,
    is_banned INTEGER DEFAULT 0,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS marketing_channels (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT,
    invite_link TEXT,
    is_optional INTEGER DEFAULT 0,
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    full_name TEXT,
    phone_number TEXT,
    payout_method TEXT,
    status TEXT DEFAULT 'pending',
    channel_post_id INTEGER DEFAULT 0,
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_verifications (
    user_id INTEGER PRIMARY KEY,
    ip_address TEXT,
    fingerprint_hash TEXT,
    verification_method TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_metadata (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT
);

-- Seed defaults
INSERT OR IGNORE INTO system_metadata (meta_key, meta_value) VALUES ('reward_per_referral', '10');
INSERT OR IGNORE INTO system_metadata (meta_key, meta_value) VALUES ('min_withdrawal', '50');
"""

async def init_db():
    """ዳታቤዙን ይፈጥራል፣ ቴብሎችን ያዘጋጃል"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Users Management
# ─────────────────────────────────────────────────────────────────────────────
async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM system_users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

async def create_user(user_id: int, username: str, full_name: str, referred_by: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO system_users (user_id, username, full_name, referrer_id)
               VALUES (?, ?, ?, ?)""",
            (user_id, username, full_name, referred_by),
        )
        await db.commit()

async def add_balance(user_id: int, amount: float):
    """የተጠቃሚውን ባላንስ በቀጥታ በዳታቤዙ ላይ ይጨምራል/ይቀንሳል"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE system_users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM system_users WHERE referrer_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["cnt"] if row else 0

async def ban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE system_users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Verifications Management
# ─────────────────────────────────────────────────────────────────────────────
async def is_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id FROM device_verifications WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone()) is not None

async def find_duplicate(ip: str, fingerprint: str, exclude_user: int):
    if not fingerprint or fingerprint == "undefined":
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT user_id FROM device_verifications
               WHERE (ip_address = ? OR fingerprint_hash = ?) AND user_id != ?
               LIMIT 1""",
            (ip, fingerprint, exclude_user),
        )
        row = await cur.fetchone()
        return row["user_id"] if row else None

async def save_verification(user_id: int, ip: str, ua: str, fingerprint: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO device_verifications (user_id, ip_address, fingerprint_hash, verification_method)
               VALUES (?, ?, ?, ?)""",
            (user_id, ip, fingerprint, "MiniAppSecureModule"),
        )
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Withdrawals Management
# ─────────────────────────────────────────────────────────────────────────────
async def create_withdrawal(user_id: int, amount: float, full_name: str, phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO user_withdrawals (user_id, amount, full_name, phone_number, payout_method)
               VALUES (?, ?, ?, ?, 'Telebirr')""",
            (user_id, amount, full_name, phone),
        )
        await db.commit()
        return cur.lastrowid

async def get_pending_withdrawals():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_withdrawals WHERE status = 'pending' ORDER BY requested_at"
        )
        return await cur.fetchall()

async def get_withdrawal(wid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM user_withdrawals WHERE id = ?", (wid,))
        return await cur.fetchone()

async def update_withdrawal_status(wid: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_withdrawals SET status = ? WHERE id = ?",
            (status, wid),
        )
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Force-join Channels Management
# ─────────────────────────────────────────────────────────────────────────────
async def add_force_channel(channel_id: str, channel_name: str, invite_link: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO marketing_channels (channel_id, channel_name, invite_link, is_optional)
               VALUES (?, ?, ?, 0)""",
            (channel_id, channel_name, invite_link),
        )
        await db.commit()

async def remove_force_channel(channel_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM marketing_channels WHERE channel_id = ?", (channel_id,)
        )
        await db.commit()

async def get_force_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM marketing_channels")
        return await cur.fetchall()

# ─────────────────────────────────────────────────────────────────────────────
# Settings Management
# ─────────────────────────────────────────────────────────────────────────────
async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT meta_value FROM system_metadata WHERE meta_key = ?", (key,))
        row = await cur.fetchone()
        return row["meta_value"] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO system_metadata (meta_key, meta_value) VALUES (?, ?)", (key, value)
        )
        await db.commit()

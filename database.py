"""
database.py – async SQLite (aiosqlite) data layer.
Swap for asyncpg + PostgreSQL by changing the adapter calls below.
"""
import os
import json
import aiosqlite
from datetime import datetime

DB_PATH = os.getenv("DATABASE_URL", "referral_bot.db")
# If a real postgres:// URL is provided we fall back gracefully to SQLite path name
if DB_PATH.startswith("postgres"):
    DB_PATH = "referral_bot.db"

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    full_name      TEXT,
    referred_by    INTEGER,
    balance        REAL    DEFAULT 0,
    is_banned      INTEGER DEFAULT 0,
    joined_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER UNIQUE,
    ip_address     TEXT,
    user_agent     TEXT,
    fingerprint    TEXT,
    verified_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER,
    amount         REAL,
    full_name      TEXT,
    phone          TEXT,
    status         TEXT    DEFAULT 'pending',   -- pending | approved | rejected
    created_at     TEXT    DEFAULT (datetime('now')),
    resolved_at    TEXT
);

CREATE TABLE IF NOT EXISTS force_channels (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id     TEXT UNIQUE,
    channel_name   TEXT,
    invite_link    TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key            TEXT PRIMARY KEY,
    value          TEXT
);

-- Seed defaults (ignored if rows already exist)
INSERT OR IGNORE INTO settings (key, value) VALUES ('reward_per_referral', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal', '50');
"""

# ─────────────────────────────────────────────────────────────────────────────
# Connection helper
# ─────────────────────────────────────────────────────────────────────────────
async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────
async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

async def create_user(user_id: int, username: str, full_name: str, referred_by: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by)
               VALUES (?, ?, ?, ?)""",
            (user_id, username, full_name, referred_by),
        )
        await db.commit()

async def add_balance(user_id: int, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await db.commit()

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE referred_by = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["cnt"] if row else 0

async def ban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Verifications
# ─────────────────────────────────────────────────────────────────────────────
async def is_verified(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id FROM verifications WHERE user_id = ?", (user_id,)
        )
        return (await cur.fetchone()) is not None

async def find_duplicate(ip: str, fingerprint: str, exclude_user: int):
    """Return the first existing user_id that shares this IP or fingerprint."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT user_id FROM verifications
               WHERE (ip_address = ? OR fingerprint = ?) AND user_id != ?
               LIMIT 1""",
            (ip, fingerprint, exclude_user),
        )
        row = await cur.fetchone()
        return row["user_id"] if row else None

async def save_verification(user_id: int, ip: str, ua: str, fingerprint: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO verifications (user_id, ip_address, user_agent, fingerprint)
               VALUES (?, ?, ?, ?)""",
            (user_id, ip, ua, fingerprint),
        )
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Withdrawals
# ─────────────────────────────────────────────────────────────────────────────
async def create_withdrawal(user_id: int, amount: float, full_name: str, phone: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO withdrawals (user_id, amount, full_name, phone)
               VALUES (?, ?, ?, ?)""",
            (user_id, amount, full_name, phone),
        )
        await db.commit()
        return cur.lastrowid

async def get_pending_withdrawals():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY created_at"
        )
        return await cur.fetchall()

async def get_withdrawal(wid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,))
        return await cur.fetchone()

async def update_withdrawal_status(wid: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE withdrawals SET status = ?, resolved_at = datetime('now') WHERE id = ?",
            (status, wid),
        )
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Force-join channels
# ─────────────────────────────────────────────────────────────────────────────
async def add_force_channel(channel_id: str, channel_name: str, invite_link: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO force_channels (channel_id, channel_name, invite_link)
               VALUES (?, ?, ?)""",
            (channel_id, channel_name, invite_link),
        )
        await db.commit()

async def remove_force_channel(channel_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM force_channels WHERE channel_id = ?", (channel_id,)
        )
        await db.commit()

async def get_force_channels():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM force_channels")
        return await cur.fetchall()

# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────
async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()

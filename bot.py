"""
================================================================================
                    TELEGRAM ADVANCED REFERRAL BOT SYSTEM
         [ Complete Production Engine - All-In-One Unified Architecture ]
================================================================================
"""

import os
import sys
import json
import hmac
import asyncio
import hashlib
import logging
import urllib.parse
from datetime import datetime

import httpx
import uvicorn
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ReferralBotSystem")

BOT_TOKEN            = os.getenv("BOT_TOKEN", "")
ADMIN_IDS            = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
PAYMENT_LOG_CHANNEL  = os.getenv("PAYMENT_LOG_CHANNEL", "").strip()
WEBAPP_URL           = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
PROXYCHECK_API_KEY   = os.getenv("PROXYCHECK_API_KEY", "")
DB_PATH              = "referral_bot.db"

TELEBIRR_PROOF_IMAGE = "AgACAgQAAxkBAAOYai38ooud5iofBd3aDGuCiX273t8AAj4PaxsYl3BR78MpfA_cDpkBAAMCAAN4AAM8BA"

if not WEBAPP_URL.startswith(("http://", "https://")):
    WEBAPP_URL = f"https://{WEBAPP_URL}"

BOT_RULES_CAPTION = (
    "📜 <b>System Terms of Service & Anti-Fraud Policy</b>\n\n"
    "1. <b>Strict Integrity:</b> Self-referrals, coordinated multi-accounting schemes, or creating fake profiles are strictly prohibited.\n"
    "2. <b>Security Protocols:</b> The use of VPNs, proxy networks, or automated emulators is heavily banned.\n"
    "3. <b>Reward Settlement:</b> Invite rewards are only credited once the referee opens the Mini App and clears the validation scan.\n"
    "4. <b>Withdrawal Review:</b> All payouts are processed by our financial desk within 24 hours.\n\n"
    "⚠️ <i>Note: Violations will result in a permanent ban.</i>"
)

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SCHEMA
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    referred_by INTEGER,
    balance     REAL    DEFAULT 0,
    is_banned   INTEGER DEFAULT 0,
    joined_at   TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER UNIQUE,
    ip_address  TEXT,
    user_agent  TEXT,
    fingerprint TEXT,
    verified_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER,
    amount          REAL,
    full_name       TEXT,
    phone           TEXT,
    status          TEXT    DEFAULT 'pending',
    channel_post_id INTEGER DEFAULT 0,
    created_at      TEXT    DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE TABLE IF NOT EXISTS force_channels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id   TEXT UNIQUE,
    channel_name TEXT,
    invite_link  TEXT,
    -- NEW: bot_added = 1 means bot joined without admin rights (monitoring only)
    bot_added    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('reward_per_referral', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal', '50');
"""

# ─────────────────────────────────────────────────────────────────────────────
# DATA ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class DataEngine:

    @staticmethod
    async def init_database():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    @staticmethod
    async def get_user(user_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return await cur.fetchone()

    @staticmethod
    async def create_user(user_id: int, username: str, full_name: str, referred_by: int = None):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
                (user_id, username, full_name, referred_by),
            )
            await db.commit()

    @staticmethod
    async def add_balance(user_id: int, amount: float):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id)
            )
            await db.commit()

    # ── FIX: was returning Row["cnt"] which sometimes fails; use index ────────
    @staticmethod
    async def get_referral_count(user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    @staticmethod
    async def ban_user(user_id: int, status: int = 1):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET is_banned = ? WHERE user_id = ?", (status, user_id)
            )
            await db.commit()

    @staticmethod
    async def is_verified(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id FROM verifications WHERE user_id = ?", (user_id,)
            )
            return (await cur.fetchone()) is not None

    @staticmethod
    async def find_duplicate(ip: str, fingerprint: str, exclude_user: int):
        if not fingerprint or fingerprint == "undefined":
            return None
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT user_id FROM verifications "
                "WHERE (ip_address = ? OR fingerprint = ?) AND user_id != ? LIMIT 1",
                (ip, fingerprint, exclude_user),
            )
            row = await cur.fetchone()
            return row["user_id"] if row else None

    @staticmethod
    async def save_verification(user_id: int, ip: str, ua: str, fingerprint: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO verifications "
                "(user_id, ip_address, user_agent, fingerprint) VALUES (?,?,?,?)",
                (user_id, ip, ua, fingerprint),
            )
            await db.commit()

    @staticmethod
    async def create_withdrawal(user_id: int, amount: float,
                                full_name: str, phone: str) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO withdrawals (user_id, amount, full_name, phone) VALUES (?,?,?,?)",
                (user_id, amount, full_name, phone),
            )
            await db.commit()
            return cur.lastrowid

    @staticmethod
    async def get_withdrawal(wid: int):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (wid,))
            return await cur.fetchone()

    @staticmethod
    async def update_withdrawal_status(wid: int, status: str, post_id: int = 0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE withdrawals SET status=?, channel_post_id=?, "
                "resolved_at=datetime('now') WHERE id=?",
                (status, post_id, wid),
            )
            await db.commit()

    @staticmethod
    async def get_pending_withdrawals():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at"
            )
            return await cur.fetchall()

    # ── Force channels (with bot_added flag) ──────────────────────────────────
    @staticmethod
    async def add_force_channel(channel_id: str, channel_name: str,
                                invite_link: str, bot_added: int = 0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_channels "
                "(channel_id, channel_name, invite_link, bot_added) VALUES (?,?,?,?)",
                (channel_id, channel_name, invite_link, bot_added),
            )
            await db.commit()

    @staticmethod
    async def remove_force_channel(channel_id: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM force_channels WHERE channel_id = ?", (channel_id,)
            )
            await db.commit()

    @staticmethod
    async def get_force_channels():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM force_channels")
            return await cur.fetchall()

    @staticmethod
    async def get_setting(key: str, default=None):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
            return row["value"] if row else default

    @staticmethod
    async def set_setting(key: str, value: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
            )
            await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# FSM STATES
# ─────────────────────────────────────────────────────────────────────────────
class UserWithdrawalWorkflow(StatesGroup):
    select_payout_gateway = State()
    input_cash_volume     = State()
    provide_mobile_digits = State()
    provide_account_title = State()
    payout_final_approval = State()

class AdminConsoleWorkflow(StatesGroup):
    modify_referral_bounty   = State()
    modify_minimum_cashout   = State()
    # Force channel WITH admin (classic)
    append_mandatory_id      = State()
    append_mandatory_title   = State()
    append_mandatory_url     = State()
    # Force channel WITHOUT admin (new)
    append_noadmin_link      = State()
    append_noadmin_title     = State()
    # Shared admin states
    direct_balance_target_id = State()
    direct_balance_volume    = State()
    broadcast_intel_payload  = State()
    lookup_individual_id     = State()
    banish_individual_id     = State()
    pardon_individual_id     = State()

# ─────────────────────────────────────────────────────────────────────────────
# BOT + DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────
bot        = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp         = Dispatcher(storage=MemoryStorage())
core_router = Router()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_admin_access(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def parse_telegram_webapp_handshake(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        vh = parsed.pop("hash", "")
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        sig = hmac.new(key, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, vh):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

async def inspect_compulsory_memberships(user_id: int) -> list:
    channels = await DataEngine.get_force_channels()
    unjoined = []
    for ch in channels:
        # bot_added channels: we can't call get_chat_member without admin rights
        # so we skip the API check — user must prove via button
        if ch["bot_added"]:
            # We trust the invite link; mark as "unverifiable, always show"
            # This means the join button is always shown until user clicks verify
            # A smarter approach: track per-user join via a separate table (optional upgrade)
            pass
        else:
            try:
                m = await bot.get_chat_member(chat_id=ch["channel_id"], user_id=user_id)
                if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED,
                                ChatMemberStatus.RESTRICTED):
                    unjoined.append(dict(ch))
                continue
            except Exception:
                pass
        unjoined.append(dict(ch))
    return unjoined

async def enforce_membership_gate(event, user_id: int) -> bool:
    unjoined = await inspect_compulsory_memberships(user_id)
    if not unjoined:
        return True
    buttons = [
        [InlineKeyboardButton(text=f"➕ {ch['channel_name']}", url=ch["invite_link"])]
        for ch in unjoined
    ]
    buttons.append([InlineKeyboardButton(
        text="✅ Joined — Verify Status", callback_data="ui_revalidate_channels"
    )])
    txt = ("⚠️ <b>Action Required:</b> Please join our mandatory channel(s) "
           "to continue:")
    if isinstance(event, Message):
        await event.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif isinstance(event, CallbackQuery):
        await event.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await event.answer()
    return False

async def execute_network_vpn_lookup(client_ip: str) -> bool:
    if not client_ip or client_ip in ("127.0.0.1", "::1", "unknown"):
        return False
    try:
        param = f"&key={PROXYCHECK_API_KEY}" if PROXYCHECK_API_KEY else ""
        url   = f"https://proxycheck.io/v2/{client_ip}?vpn=1{param}"
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(url)
            return r.json().get(client_ip, {}).get("proxy") == "yes"
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# UI KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────
def generate_verification_widget(user_id: int, ref: int, msg_id: int = 0):
    url = f"{WEBAPP_URL}/verify?uid={user_id}&ref={ref}&msg_id={msg_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔐 Open Mini App & Verify",
                             web_app=WebAppInfo(url=url))
    ]])

def generate_dashboard_matrix(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💰 Balance / ሒሳብ",    callback_data="ui_fetch_balance"),
            InlineKeyboardButton(text="👥 Referrals / ጋባዦች", callback_data="ui_fetch_referrals"),
        ],
        [
            InlineKeyboardButton(text="🔗 My Link / ሊንኬ",         callback_data="ui_fetch_link"),
            InlineKeyboardButton(text="💸 Withdraw / ብር ማውጫ", callback_data="ui_initiate_withdrawal"),
        ],
    ]
    if evaluate_admin_access(user_id):
        rows.append([InlineKeyboardButton(
            text="⚙️ Admin Control Center", callback_data="ui_admin_core"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def generate_admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 Set Referral Reward",  callback_data="adm_cmd_reward"),
            InlineKeyboardButton(text="💵 Set Min Withdrawal",   callback_data="adm_cmd_min_wd"),
        ],
        [
            InlineKeyboardButton(text="✍️ Edit User Balance",  callback_data="adm_cmd_edit_bal"),
            InlineKeyboardButton(text="📊 Bot Statistics",     callback_data="adm_cmd_stats"),
        ],
        [
            # ── Force channels section ──────────────────────────────────────
            InlineKeyboardButton(text="🔴 Force Join (With Admin)",    callback_data="adm_cmd_add_mand"),
            InlineKeyboardButton(text="🟡 Force Join (No Admin)",      callback_data="adm_cmd_add_noadmin"),
        ],
        [
            InlineKeyboardButton(text="🗑 Remove Force Channel",       callback_data="adm_cmd_rm_node"),
            InlineKeyboardButton(text="📋 List Force Channels",        callback_data="adm_cmd_list_channels"),
        ],
        [
            InlineKeyboardButton(text="📥 Pending Withdrawals",  callback_data="adm_cmd_pending_tickets"),
            InlineKeyboardButton(text="📢 Broadcast Message",    callback_data="adm_cmd_broadcast"),
        ],
        [
            InlineKeyboardButton(text="🔍 Search User",   callback_data="adm_cmd_search"),
        ],
        [
            InlineKeyboardButton(text="🚫 Ban User",    callback_data="adm_cmd_ban"),
            InlineKeyboardButton(text="✅ Unban User",  callback_data="adm_cmd_unban"),
        ],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="ui_return_home")],
    ])

def generate_fallback_navigation(target="ui_return_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back / ተመለስ", callback_data=target)
    ]])

# ─────────────────────────────────────────────────────────────────────────────
# USER HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.message(CommandStart())
async def process_start_command(message: Message, state: FSMContext):
    await state.clear()
    uid  = message.from_user.id
    args = message.text.split()
    arg  = args[1] if len(args) > 1 else ""
    ref  = int(arg) if arg.isdigit() and int(arg) != uid else 0

    acc = await DataEngine.get_user(uid)
    if acc and acc["is_banned"]:
        return await message.answer("🚫 <b>Access Denied:</b> Your profile has been blacklisted.")

    if not await enforce_membership_gate(message, uid):
        if ref:
            await state.update_data(stashed_referrer_id=ref)
        return

    if await DataEngine.is_verified(uid):
        return await message.answer(
            "✅ <b>Welcome back!</b> Access granted.",
            reply_markup=generate_dashboard_matrix(uid)
        )

    sent = await message.answer(
        f"{BOT_RULES_CAPTION}\n\n🔐 <b>Next Step:</b> Verify your identity via Mini App:",
        reply_markup=generate_verification_widget(uid, ref, 0)
    )
    await sent.edit_reply_markup(
        reply_markup=generate_verification_widget(uid, ref, sent.message_id)
    )

@core_router.callback_query(F.data == "ui_revalidate_channels")
async def process_channel_revalidation(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    unjoined = await inspect_compulsory_memberships(uid)
    if unjoined:
        return await callback.answer(
            "❌ Membership verification failed. Join all channels first.", show_alert=True
        )
    try:
        await callback.message.delete()
    except Exception:
        pass
    s = await state.get_data()
    ref = s.get("stashed_referrer_id", 0)
    await state.clear()

    if await DataEngine.is_verified(uid):
        await callback.message.answer(
            "✅ Identity clear!", reply_markup=generate_dashboard_matrix(uid)
        )
    else:
        sent = await callback.message.answer(
            f"{BOT_RULES_CAPTION}\n\n🔐 <b>Attestation Step:</b> Launch Mini App verification:",
            reply_markup=generate_verification_widget(uid, ref, 0)
        )
        await sent.edit_reply_markup(
            reply_markup=generate_verification_widget(uid, ref, sent.message_id)
        )

@core_router.callback_query(F.data == "ui_return_home")
async def process_navigation_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await enforce_membership_gate(callback, callback.from_user.id):
        return
    await callback.message.edit_text(
        "🏠 <b>Main Dashboard Menu / ዋና ማውጫ</b>",
        reply_markup=generate_dashboard_matrix(callback.from_user.id)
    )

@core_router.callback_query(F.data == "ui_fetch_balance")
async def process_balance_query(callback: CallbackQuery):
    if not await enforce_membership_gate(callback, callback.from_user.id):
        return
    acc   = await DataEngine.get_user(callback.from_user.id)
    min_w = await DataEngine.get_setting("min_withdrawal", "50")
    await callback.message.edit_text(
        f"💰 <b>Your Available Balance:</b>\n\n"
        f"• Assets: <code>{acc['balance']:.2f} Birr</code>\n"
        f"• Minimum Withdrawal: <code>{min_w} Birr</code>",
        reply_markup=generate_fallback_navigation()
    )

# ── FIX: referrals button ─────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_fetch_referrals")
async def process_referral_query(callback: CallbackQuery):
    if not await enforce_membership_gate(callback, callback.from_user.id):
        return
    uid  = callback.from_user.id
    cnt  = await DataEngine.get_referral_count(uid)           # fixed: returns int directly
    rate = float(await DataEngine.get_setting("reward_per_referral", "10"))
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    await callback.message.edit_text(
        f"👥 <b>Your Referral Network:</b>\n\n"
        f"• Total Referrals: <b>{cnt} users</b>\n"
        f"• Earnings per Referral: <b>{rate:.2f} Birr</b>\n"
        f"• Total Earned: <b>{cnt * rate:.2f} Birr</b>\n\n"
        f"🔗 Your link:\n<code>{link}</code>",
        reply_markup=generate_fallback_navigation()
    )

@core_router.callback_query(F.data == "ui_fetch_link")
async def process_link_generation(callback: CallbackQuery):
    if not await enforce_membership_gate(callback, callback.from_user.id):
        return
    me = await bot.get_me()
    await callback.message.edit_text(
        f"🔗 <b>Your Invite Link:</b>\n\n"
        f"<code>https://t.me/{me.username}?start={callback.from_user.id}</code>",
        reply_markup=generate_fallback_navigation()
    )

# ─────────────────────────────────────────────────────────────────────────────
# WITHDRAWAL FLOW
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_initiate_withdrawal")
async def process_withdrawal_start(callback: CallbackQuery, state: FSMContext):
    if not await enforce_membership_gate(callback, callback.from_user.id):
        return
    user  = await DataEngine.get_user(callback.from_user.id)
    min_w = float(await DataEngine.get_setting("min_withdrawal", "50"))
    if user["balance"] < min_w:
        return await callback.answer(
            f"❌ Minimum payout baseline is {min_w} Birr.", show_alert=True
        )
    await state.set_state(UserWithdrawalWorkflow.select_payout_gateway)
    await state.update_data(cached_balance=user["balance"], cached_minimum=min_w)
    await callback.message.edit_text(
        "💸 <b>Select Payout Endpoint:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📲 Telebirr / ቴሌብር", callback_data="gateway_telebirr")],
            [InlineKeyboardButton(text="❌ Cancel / ሰርዝ",      callback_data="ui_return_home")],
        ])
    )

@core_router.callback_query(F.data == "gateway_telebirr",
                             UserWithdrawalWorkflow.select_payout_gateway)
async def process_telebirr_selection(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserWithdrawalWorkflow.input_cash_volume)
    await callback.message.edit_text(
        "<b>Specify the amount you wish to withdraw:</b>",
        reply_markup=generate_fallback_navigation()
    )

@core_router.message(UserWithdrawalWorkflow.input_cash_volume)
async def process_cashout_volume(message: Message, state: FSMContext):
    s = await state.get_data()
    try:
        val = float(message.text.strip())
        assert s["cached_minimum"] <= val <= s["cached_balance"]
    except Exception:
        return await message.answer("❌ Invalid amount. Check your balance and minimum limits.")
    await state.update_data(validated_volume=val)
    await state.set_state(UserWithdrawalWorkflow.provide_mobile_digits)
    await message.answer("📱 <b>Provide Destination Account Mobile Number:</b>")

@core_router.message(UserWithdrawalWorkflow.provide_mobile_digits)
async def process_mobile_digits(message: Message, state: FSMContext):
    phone = message.text.strip()
    if len(phone) < 9:
        return await message.answer("❌ Provide a valid mobile number.")
    await state.update_data(validated_phone=phone)
    await state.set_state(UserWithdrawalWorkflow.provide_account_title)
    await message.answer("📝 <b>Enter Full Name of Account Holder:</b>")

@core_router.message(UserWithdrawalWorkflow.provide_account_title)
async def process_account_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if len(title) < 3:
        return await message.answer("❌ Name is too short.")
    await state.update_data(validated_title=title)
    s = await state.get_data()
    await message.answer(
        f"⚠️ <b>Review Settlement Details</b>\n\n"
        f"• Platform: <code>Telebirr</code>\n"
        f"• Amount: <code>{s['validated_volume']:.2f} ETB</code>\n"
        f"• Holder: <code>{title}</code>\n"
        f"• Number: <code>{s['validated_phone']}</code>\n\n"
        f"Authorization requested.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Transact Payout",  callback_data="action_payout_dispatch"),
            InlineKeyboardButton(text="❌ Abort / ሰርዝ",     callback_data="ui_return_home"),
        ]])
    )
    await state.set_state(UserWithdrawalWorkflow.payout_final_approval)

@core_router.callback_query(F.data == "action_payout_dispatch",
                             UserWithdrawalWorkflow.payout_final_approval)
async def process_payout_dispatch(callback: CallbackQuery, state: FSMContext):
    s    = await state.get_data()
    uid  = callback.from_user.id
    user = await DataEngine.get_user(uid)
    if user["balance"] < s["validated_volume"]:
        return await callback.answer("❌ Insufficient funds.", show_alert=True)

    tid = await DataEngine.create_withdrawal(
        uid, s["validated_volume"], s["validated_title"], s["validated_phone"]
    )
    await DataEngine.add_balance(uid, -s["validated_volume"])
    await state.clear()

    post_id = 0
    if PAYMENT_LOG_CHANNEL:
        try:
            alias = f"@{user['username']}" if user["username"] else "Private Profile"
            txt   = (
                f"⏳ <b>NEW WITHDRAWAL REQUEST</b>\n\n"
                f"👤 {s['validated_title']} ({alias})\n"
                f"💰 <code>ETB {s['validated_volume']:.2f}</code>\n"
                f"📱 Telebirr\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            receipt = await bot.send_message(PAYMENT_LOG_CHANNEL, txt)
            post_id = receipt.message_id
            await DataEngine.update_withdrawal_status(tid, "pending", post_id)
        except Exception as e:
            logger.error(f"Channel error: {e}")

    alias_str = f"@{user['username']}" if user["username"] else "None"
    admin_txt = (
        f"📥 <b>Incoming Ticket #{tid}</b>\n\n"
        f"👤 {s['validated_title']}\n"
        f"🆔 <code>{uid}</code>  🏷 {alias_str}\n"
        f"💰 <b>{s['validated_volume']:.2f} Birr</b>\n"
        f"📱 <code>{s['validated_phone']}</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve",  callback_data=f"adm_payout_ap_{tid}"),
        InlineKeyboardButton(text="❌ Deny",     callback_data=f"adm_payout_rj_{tid}"),
    ]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, admin_txt, reply_markup=markup)
        except Exception as e:
            logger.error(f"Admin notify failed {aid}: {e}")

    await callback.message.edit_text(
        "📨 <b>Withdrawal Submitted!</b> Processing within 2-48 hours.",
        reply_markup=generate_dashboard_matrix(uid)
    )

@core_router.callback_query(F.data.startswith("adm_payout_ap_"))
async def process_admin_approval(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    tid    = int(callback.data.split("_")[3])
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket or ticket["status"] != "pending":
        return await callback.answer("Already processed.")

    await DataEngine.update_withdrawal_status(tid, "approved", ticket["channel_post_id"])
    if PAYMENT_LOG_CHANNEL and ticket["channel_post_id"]:
        try:
            await bot.send_photo(
                PAYMENT_LOG_CHANNEL, TELEBIRR_PROOF_IMAGE,
                caption=(
                    f"✅ <b>PAYOUT COMPLETED</b>\n\n"
                    f"👤 {ticket['full_name']}\n"
                    f"💰 <code>ETB {ticket['amount']:.2f}</code> ✅"
                ),
                reply_to_message_id=ticket["channel_post_id"]
            )
        except Exception as e:
            logger.error(f"Photo confirm error: {e}")
    try:
        await bot.send_message(
            ticket["user_id"],
            f"🎉 Your cashout of {ticket['amount']:.2f} Birr has been processed!"
        )
    except Exception:
        pass
    await callback.message.edit_text(callback.message.text + "\n\n✅ Approved.")

@core_router.callback_query(F.data.startswith("adm_payout_rj_"))
async def process_admin_rejection(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    tid    = int(callback.data.split("_")[3])
    ticket = await DataEngine.get_withdrawal(tid)
    if not ticket or ticket["status"] != "pending":
        return await callback.answer("Already evaluated.")
    await DataEngine.update_withdrawal_status(tid, "rejected")
    await DataEngine.add_balance(ticket["user_id"], ticket["amount"])
    try:
        await bot.send_message(
            ticket["user_id"], "❌ Withdrawal rejected. Assets returned to your balance."
        )
    except Exception:
        pass
    await callback.message.edit_text(callback.message.text + "\n\n❌ Rejected.")

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_admin_core")
async def process_admin_panel(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    await callback.message.edit_text(
        "⚙️ <b>Operational Admin Master Engine</b>",
        reply_markup=generate_admin_dashboard()
    )

# ── Force Channel WITH Admin ───────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_add_mand")
async def process_add_channel_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.append_mandatory_id)
    await callback.message.edit_text(
        "🔴 <b>Force Join — With Admin Rights</b>\n\n"
        "Bot must be admin in this channel.\n"
        "Enter Channel ID (e.g. <code>-1001234567890</code>):",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.append_mandatory_id)
async def process_add_channel_id(message: Message, state: FSMContext):
    await state.update_data(ch_id=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_title)
    await message.answer("📝 <b>Enter Channel Title:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_title)
async def process_add_channel_title(message: Message, state: FSMContext):
    await state.update_data(ch_title=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_url)
    await message.answer("🔗 <b>Enter Public/Private Invite Link:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_url)
async def process_add_channel_finalize(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    await DataEngine.add_force_channel(s["ch_id"], s["ch_title"],
                                       message.text.strip(), bot_added=0)
    await message.answer(
        f"✅ <b>Force channel added (With Admin)</b>\n📌 {s['ch_title']}",
        reply_markup=generate_admin_dashboard()
    )

# ── Force Channel WITHOUT Admin (NEW) ─────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_add_noadmin")
async def process_add_noadmin_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.append_noadmin_link)
    await callback.message.edit_text(
        "🟡 <b>Force Join — Without Admin Rights</b>\n\n"
        "Bot does NOT need to be admin.\n"
        "User will see a join button. After clicking, they press verify.\n\n"
        "⚠️ <i>Note: Membership cannot be verified automatically — "
        "users self-report via the Verify button.</i>\n\n"
        "Enter the channel/group <b>invite link</b> (t.me/... or t.me/+...):",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.append_noadmin_link)
async def process_noadmin_link(message: Message, state: FSMContext):
    link = message.text.strip()
    await state.update_data(na_link=link)
    await state.set_state(AdminConsoleWorkflow.append_noadmin_title)
    await message.answer("📝 <b>Enter Channel/Group Display Name:</b>")

@core_router.message(AdminConsoleWorkflow.append_noadmin_title)
async def process_noadmin_title(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    # Use link as channel_id key since we have no numeric ID
    channel_key = s["na_link"].replace("https://", "").replace("http://", "")
    await DataEngine.add_force_channel(
        channel_id=channel_key,
        channel_name=message.text.strip(),
        invite_link=s["na_link"],
        bot_added=1          # flag: no admin verification possible
    )
    await message.answer(
        f"✅ <b>Force channel added (No Admin)</b>\n"
        f"📌 {message.text.strip()}\n"
        f"🔗 {s['na_link']}\n\n"
        f"⚠️ Users self-verify after joining.",
        reply_markup=generate_admin_dashboard()
    )

# ── List Force Channels ────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_list_channels")
async def process_list_channels(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    channels = await DataEngine.get_force_channels()
    if not channels:
        return await callback.message.edit_text(
            "📭 No force channels configured.",
            reply_markup=generate_fallback_navigation("ui_admin_core")
        )
    lines = []
    for ch in channels:
        mode = "🟡 No-Admin" if ch["bot_added"] else "🔴 With-Admin"
        lines.append(f"{mode} — <b>{ch['channel_name']}</b>\n🔗 {ch['invite_link']}")
    await callback.message.edit_text(
        f"📋 <b>Force Channels ({len(channels)})</b>\n\n" + "\n\n".join(lines),
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

# ── Remove Force Channel ───────────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_rm_node")
async def process_rm_channel_menu(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    channels = await DataEngine.get_force_channels()
    if not channels:
        return await callback.message.edit_text(
            "📭 No channels to remove.",
            reply_markup=generate_fallback_navigation("ui_admin_core")
        )
    buttons = []
    for ch in channels:
        mode = "🟡" if ch["bot_added"] else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {mode} {ch['channel_name']}",
            callback_data=f"execute_rm_node_{ch['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")])
    await callback.message.edit_text(
        "<b>Select channel to remove:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@core_router.callback_query(F.data.startswith("execute_rm_node_"))
async def process_rm_channel_action(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    row_id = int(callback.data.replace("execute_rm_node_", ""))
    # Delete by primary key id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM force_channels WHERE id = ?", (row_id,))
        await db.commit()
    await callback.message.edit_text(
        "✅ Channel removed.", reply_markup=generate_admin_dashboard()
    )

# ── Other admin commands ───────────────────────────────────────────────────────
@core_router.callback_query(F.data == "adm_cmd_edit_bal")
async def process_edit_balance_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.direct_balance_target_id)
    await callback.message.edit_text(
        "<b>Enter Targeted Telegram User ID:</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.direct_balance_target_id)
async def process_edit_balance_id(message: Message, state: FSMContext):
    await state.update_data(target_uid=int(message.text.strip()))
    await state.set_state(AdminConsoleWorkflow.direct_balance_volume)
    await message.answer("<b>Enter Adjustment Volume (e.g. 50 or -20):</b>")

@core_router.message(AdminConsoleWorkflow.direct_balance_volume)
async def process_edit_balance_final(message: Message, state: FSMContext):
    s = await state.get_data()
    await state.clear()
    await DataEngine.add_balance(s["target_uid"], float(message.text.strip()))
    await message.answer("✅ Balance adjusted.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_stats")
async def process_stats(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*), SUM(balance) FROM users")
        row = await cur.fetchone()
    pop = row[0] or 0
    cap = row[1] or 0.0
    await callback.message.edit_text(
        f"📊 <b>Analytics:</b>\n\n• Total Users: <b>{pop}</b>\n• Total Balance: <b>{cap:.2f} ETB</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.callback_query(F.data == "adm_cmd_broadcast")
async def process_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.broadcast_intel_payload)
    await callback.message.edit_text(
        "📢 <b>Enter Broadcast Message:</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.broadcast_intel_payload)
async def process_broadcast_execute(message: Message, state: FSMContext):
    text = message.text
    await state.clear()
    progress = await message.answer("⏳ Sending...")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        nodes = await cur.fetchall()
    sc = 0
    for (uid,) in nodes:
        try:
            await bot.send_message(uid, text)
            sc += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await progress.delete()
    await message.answer(
        f"✅ Sent to {sc} users.", reply_markup=generate_admin_dashboard()
    )

@core_router.callback_query(F.data == "adm_cmd_search")
async def process_search_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.lookup_individual_id)
    await callback.message.edit_text(
        "🔍 <b>Enter User Telegram ID:</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.lookup_individual_id)
async def process_search_execute(message: Message, state: FSMContext):
    await state.clear()
    user = await DataEngine.get_user(int(message.text.strip()))
    if not user:
        return await message.answer("❌ User not found.", reply_markup=generate_admin_dashboard())
    cnt = await DataEngine.get_referral_count(user["user_id"])
    await message.answer(
        f"👤 <b>User Profile:</b>\n\n"
        f"• Name: {user['full_name']}\n"
        f"• Username: @{user['username'] or 'N/A'}\n"
        f"• Balance: <b>{user['balance']:.2f} Birr</b>\n"
        f"• Referrals: <b>{cnt}</b>\n"
        f"• Banned: <b>{'Yes' if user['is_banned'] else 'No'}</b>\n"
        f"• Joined: {user['joined_at']}",
        reply_markup=generate_admin_dashboard()
    )

@core_router.callback_query(F.data == "adm_cmd_ban")
async def process_ban_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.banish_individual_id)
    await callback.message.edit_text(
        "🚫 <b>Enter User ID to Ban:</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.banish_individual_id)
async def process_ban_execute(message: Message, state: FSMContext):
    await DataEngine.ban_user(int(message.text.strip()), 1)
    await state.clear()
    await message.answer("✅ User banned.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_unban")
async def process_unban_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.pardon_individual_id)
    await callback.message.edit_text(
        "✅ <b>Enter User ID to Unban:</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.pardon_individual_id)
async def process_unban_execute(message: Message, state: FSMContext):
    await DataEngine.ban_user(int(message.text.strip()), 0)
    await state.clear()
    await message.answer("✅ User unbanned.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_reward")
async def process_reward_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.modify_referral_bounty)
    await callback.message.edit_text(
        "<b>Enter New Reward Per Referral (Birr):</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.modify_referral_bounty)
async def process_reward_execute(message: Message, state: FSMContext):
    await DataEngine.set_setting("reward_per_referral", message.text.strip())
    await state.clear()
    await message.answer("✅ Reward updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_min_wd")
async def process_min_wd_start(callback: CallbackQuery, state: FSMContext):
    if not evaluate_admin_access(callback.from_user.id): return
    await state.set_state(AdminConsoleWorkflow.modify_minimum_cashout)
    await callback.message.edit_text(
        "<b>Enter New Minimum Cashout Threshold (Birr):</b>",
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

@core_router.message(AdminConsoleWorkflow.modify_minimum_cashout)
async def process_min_wd_execute(message: Message, state: FSMContext):
    await DataEngine.set_setting("min_withdrawal", message.text.strip())
    await state.clear()
    await message.answer("✅ Minimum withdrawal updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_pending_tickets")
async def process_pending_inventory(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    pending = await DataEngine.get_pending_withdrawals()
    if not pending:
        return await callback.message.edit_text(
            "📭 No pending withdrawals.", reply_markup=generate_fallback_navigation("ui_admin_core")
        )
    lines = []
    for t in pending:
        lines.append(
            f"• <b>#{t['id']}</b> — {t['full_name']} — "
            f"<code>{t['amount']:.2f} ETB</code> — {t['phone']}"
        )
    await callback.message.edit_text(
        f"📥 <b>Pending Withdrawals ({len(pending)})</b>\n\n" + "\n".join(lines),
        reply_markup=generate_fallback_navigation("ui_admin_core")
    )

# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def application_lifespan(app: FastAPI):
    await DataEngine.init_database()
    asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    yield

api_platform = FastAPI(lifespan=application_lifespan)
dp.include_router(core_router)

api_platform.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api_platform.get("/verify", response_class=HTMLResponse)
async def serve_frontend(uid: int = 0, ref: int = 0, msg_id: int = 0):
    try:
        with open("index.html") as f:
            html = f.read()
        return HTMLResponse(content=html.replace("__BACKEND_URL__", WEBAPP_URL))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Frontend missing: {e}")

@api_platform.post("/api/verify")
async def execute_verification(request: Request):
    data    = await request.json()
    tg_user = parse_telegram_webapp_handshake(data.get("initData", ""))
    if not tg_user:
        raise HTTPException(status_code=403, detail="Signature breach.")

    uid    = int(tg_user["id"])
    ref_id = int(data.get("refId") or 0)
    msg_id = int(data.get("msgId") or 0)

    if await DataEngine.is_verified(uid):
        return JSONResponse({"status": "already_verified"})

    is_clone = await DataEngine.find_duplicate(
        data.get("ip", ""), data.get("fingerprint", ""), uid
    )
    is_vpn   = data.get("isVpn") or await execute_network_vpn_lookup(data.get("ip", ""))

    if is_clone or is_vpn:
        await DataEngine.create_user(uid, tg_user.get("username", ""),
                                     tg_user.get("first_name", ""))
        await DataEngine.ban_user(uid, 1)
        return JSONResponse({"status": "blocked"})

    if msg_id > 0:
        try:
            await bot.delete_message(chat_id=uid, message_id=msg_id)
        except Exception as e:
            logger.error(f"Delete msg error: {e}")

    await DataEngine.create_user(
        uid, tg_user.get("username", ""), tg_user.get("first_name", ""),
        ref_id or None
    )
    await DataEngine.save_verification(
        uid, data.get("ip", ""), data.get("ua", ""), data.get("fingerprint", "")
    )

    if ref_id and ref_id != uid:
        bounty = float(await DataEngine.get_setting("reward_per_referral", "10"))
        await DataEngine.add_balance(ref_id, bounty)
        try:
            await bot.send_message(
                ref_id,
                f"🎉 <b>Referral verified!</b> <code>+{bounty} Birr</code> credited."
            )
        except Exception:
            pass

    try:
        await bot.send_message(
            uid, "✅ <b>Verification Confirmed!</b>",
            reply_markup=generate_dashboard_matrix(uid)
        )
    except Exception:
        pass

    return JSONResponse({"status": "verified"})


if __name__ == "__main__":
    uvicorn.run("bot:api_platform", host="0.0.0.0", port=8000, log_level="info")

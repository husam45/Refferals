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

# Third-Party Dependencies
import httpx
import uvicorn
import aiosqlite
from dotenv import load_dotenv

# Aiogram Framework Imports
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

# FastAPI Engine Imports
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# Load Environment Config
load_dotenv()

# Logging System Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ReferralBotSystem")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION VARIABLES & CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
PAYMENT_LOG_CHANNEL = os.getenv("PAYMENT_LOG_CHANNEL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
PROXYCHECK_API_KEY = os.getenv("PROXYCHECK_API_KEY", "")
DB_PATH = "referral_bot.db"

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
# DATA SEED LAYER & UNIFIED DB INTEGRATION
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    referred_by INTEGER,
    balance REAL DEFAULT 0,
    is_banned INTEGER DEFAULT 0,
    joined_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE,
    ip_address TEXT,
    user_agent TEXT,
    fingerprint TEXT,
    verified_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount REAL,
    full_name TEXT,
    phone TEXT,
    status TEXT DEFAULT 'pending',
    channel_post_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS force_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT UNIQUE,
    channel_name TEXT,
    invite_link TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES ('reward_per_referral', '10');
INSERT OR IGNORE INTO settings (key, value) VALUES ('min_withdrawal', '50');
"""

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
                "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?, ?, ?, ?)",
                (user_id, username, full_name, referred_by),
            )
            await db.commit()

    @staticmethod
    async def add_balance(user_id: int, amount: float):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
            await db.commit()

    @staticmethod
    async def get_referral_count(user_id: int) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) as cnt FROM users WHERE referred_by = ?", (user_id,))
            row = await cur.fetchone()
            return row["cnt"] if row else 0

    @staticmethod
    async def ban_user(user_id: int, status: int = 1):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (status, user_id))
            await db.commit()

    @staticmethod
    async def is_verified(user_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id FROM verifications WHERE user_id = ?", (user_id,))
            return (await cur.fetchone()) is not None

    @staticmethod
    async def find_duplicate(ip: str, fingerprint: str, exclude_user: int):
        if not fingerprint or fingerprint == "undefined":
            return None
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT user_id FROM verifications WHERE (ip_address = ? OR fingerprint = ?) AND user_id != ? LIMIT 1",
                (ip, fingerprint, exclude_user),
            )
            row = await cur.fetchone()
            return row["user_id"] if row else None

    @staticmethod
    async def save_verification(user_id: int, ip: str, ua: str, fingerprint: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO verifications (user_id, ip_address, user_agent, fingerprint) VALUES (?, ?, ?, ?)",
                (user_id, ip, ua, fingerprint),
            )
            await db.commit()

    @staticmethod
    async def create_withdrawal(user_id: int, amount: float, full_name: str, phone: str) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "INSERT INTO withdrawals (user_id, amount, full_name, phone) VALUES (?, ?, ?, ?)",
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
                "UPDATE withdrawals SET status = ?, channel_post_id = ?, resolved_at = datetime('now') WHERE id = ?",
                (status, post_id, wid),
            )
            await db.commit()

    @staticmethod
    async def get_pending_withdrawals():
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY created_at")
            return await cur.fetchall()

    @staticmethod
    async def add_force_channel(channel_id: str, channel_name: str, invite_link: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO force_channels (channel_id, channel_name, invite_link) VALUES (?, ?, ?)",
                (channel_id, channel_name, invite_link),
            )
            await db.commit()

    @staticmethod
    async def remove_force_channel(channel_id: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM force_channels WHERE channel_id = ?", (channel_id,))
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
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row["value"] if row else default

    @staticmethod
    async def set_setting(key: str, value: str):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# RUNTIME STATE ENGINE & DECORATORS
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
    append_mandatory_id      = State()
    append_mandatory_title   = State()
    append_mandatory_url     = State()
    direct_balance_target_id = State()
    direct_balance_volume    = State()
    broadcast_intel_payload  = State()
    lookup_individual_id     = State()
    banish_individual_id     = State()
    pardon_individual_id     = State()

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage_memory = MemoryStorage()
dp = Dispatcher(storage=storage_memory)
core_router = Router()

def evaluate_admin_access(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def parse_telegram_webapp_handshake(init_data: str) -> dict | None:
    try:
        parsed_matrix = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        verification_hash = parsed_matrix.pop("hash", "")
        sorted_payload_strings = [f"{k}={v}" for k, v in sorted(parsed_matrix.items())]
        compiled_data_check_string = "\n".join(sorted_payload_strings)
        hmac_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_signature = hmac.new(hmac_key, compiled_data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_signature, verification_hash):
            return None
        return json.loads(parsed_matrix.get("user", "{}"))
    except Exception:
        return None

async def inspect_compulsory_memberships(user_id: int) -> list:
    mandatory_nodes = await DataEngine.get_force_channels()
    unmatched_nodes = []
    for node in mandatory_nodes:
        try:
            member_receipt = await bot.get_chat_member(chat_id=node["channel_id"], user_id=user_id)
            if member_receipt.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED]:
                unmatched_nodes.append(node)
        except Exception:
            unmatched_nodes.append(node)
    return unmatched_nodes

async def execute_network_vpn_lookup(client_ip: str) -> bool:
    if not client_ip or client_ip in ("127.0.0.1", "::1", "unknown"):
        return False
    try:
        api_param = f"&key={PROXYCHECK_API_KEY}" if PROXYCHECK_API_KEY else ""
        query_endpoint = f"https://proxycheck.io/v2/{client_ip}?vpn=1{api_param}"
        async with httpx.AsyncClient(timeout=4) as client:
            res = await client.get(query_endpoint)
            return res.json().get(client_ip, {}).get("proxy") == "yes"
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# UI INTERACTIVE INTERFACES
# ─────────────────────────────────────────────────────────────────────────────
def generate_verification_widget(user_id: int, target_referrer: int) -> InlineKeyboardMarkup:
    url = f"{WEBAPP_URL}/verify?uid={user_id}&ref={target_referrer}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔐 Open Mini App & Verify", web_app=WebAppInfo(url=url))]])

def generate_dashboard_matrix(user_id: int) -> InlineKeyboardMarkup:
    markup = [
        [InlineKeyboardButton(text="💰 Balance / ሒሳብ", callback_data="ui_fetch_balance"), InlineKeyboardButton(text="👥 Referrals / ጋባዦች", callback_data="ui_fetch_referrals")],
        [InlineKeyboardButton(text="🔗 My Link / ሊንኬ", callback_data="ui_fetch_link"), InlineKeyboardButton(text="💸 Withdraw / ብር ማውጫ", callback_data="ui_initiate_withdrawal")],
    ]
    if evaluate_admin_access(user_id):
        markup.append([InlineKeyboardButton(text="⚙️ Admin Control Center", callback_data="ui_admin_core")])
    return InlineKeyboardMarkup(inline_keyboard=markup)

def generate_admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Set Referral Reward", callback_data="adm_cmd_reward"), InlineKeyboardButton(text="💵 Set Min Withdrawal", callback_data="adm_cmd_min_wd")],
        [InlineKeyboardButton(text="✍️ Edit User Balance", callback_data="adm_cmd_edit_bal"), InlineKeyboardButton(text="📊 Bot Statistics", callback_data="adm_cmd_stats")],
        [InlineKeyboardButton(text="🔴 Add Force Channel", callback_data="adm_cmd_add_mand"), InlineKeyboardButton(text="🗑 Remove Channel", callback_data="adm_cmd_rm_node")],
        [InlineKeyboardButton(text="📥 Pending Withdrawals", callback_data="adm_cmd_pending_tickets"), InlineKeyboardButton(text="📢 Broadcast Message", callback_data="adm_cmd_broadcast")],
        [InlineKeyboardButton(text="🔍 Search User Info", callback_data="adm_cmd_search")],
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="adm_cmd_ban"), InlineKeyboardButton(text="✅ Unban User", callback_data="adm_cmd_unban")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="ui_return_home")],
    ])

def generate_fallback_navigation(target_callback="ui_return_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back / ተመለስ", callback_data=target_callback)]])

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM USER CONTROLLERS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.message(CommandStart())
async def process_start_command(message: Message, state: FSMContext):
    await state.clear()
    caller_id = message.from_user.id
    segments = message.text.split()
    argument = segments[1] if len(segments) > 1 else ""
    validated_referrer = int(argument) if argument.isdigit() and int(argument) != caller_id else 0

    account = await DataEngine.get_user(caller_id)
    if account and account["is_banned"]:
        return await message.answer("🚫 <b>Access Denied:</b> Your profile has been blacklisted.")

    unjoined = await inspect_compulsory_memberships(caller_id)
    if unjoined:
        if validated_referrer:
            await state.update_data(stashed_referrer_id=validated_referrer)
        keyboard = []
        for ch in await DataEngine.get_force_channels():
            keyboard.append([InlineKeyboardButton(text=f"➕ {ch['channel_name']}", url=ch['invite_link'])])
        keyboard.append([InlineKeyboardButton(text="✅ Joined — Verify Status", callback_data="ui_revalidate_channels")])
        return await message.answer("👋 <b>Welcome!</b> Please join our channels below to unlock the bot system:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

    if await DataEngine.is_verified(caller_id):
        return await message.answer("✅ <b>Welcome back!</b> Access granted.", reply_markup=generate_dashboard_matrix(caller_id))
    
    await message.answer(f"{BOT_RULES_CAPTION}\n\n🔐 <b>Next Step:</b> Verify your identity via Mini App:", reply_markup=generate_verification_widget(caller_id, validated_referrer))

@core_router.callback_query(F.data == "ui_revalidate_channels")
async def process_channel_revalidation(callback: CallbackQuery, state: FSMContext):
    caller_id = callback.from_user.id
    unjoined = await inspect_compulsory_memberships(caller_id)
    if unjoined:
        await callback.answer("❌ Membership verification failed. Join all channels first.", show_alert=True)
    else:
        await callback.message.delete()
        s_data = await state.get_data()
        ref = s_data.get("stashed_referrer_id", 0)
        await state.clear()
        if await DataEngine.is_verified(caller_id):
            await callback.message.answer("✅ Identity clear!", reply_markup=generate_dashboard_matrix(caller_id))
        else:
            await callback.message.answer(f"{BOT_RULES_CAPTION}\n\n🔐 <b>Attestation Step:</b> launch Mini App verification:", reply_markup=generate_verification_widget(caller_id, ref))

@core_router.callback_query(F.data == "ui_return_home")
async def process_navigation_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 <b>Main Dashboard Menu / ዋና ማውጫ</b>", reply_markup=generate_dashboard_matrix(callback.from_user.id))

@core_router.callback_query(F.data == "ui_fetch_balance")
async def process_balance_query(callback: CallbackQuery):
    acc = await DataEngine.get_user(callback.from_user.id)
    min_l = await DataEngine.get_setting("min_withdrawal", "50")
    text = f"💰 <b>Your Available Balance:</b>\n\n• Assets: <code>{acc['balance']:.2f} Birr</code>\n• Minimum Withdrawal: <code>{min_l} Birr</code>"
    await callback.message.edit_text(text, reply_markup=generate_fallback_navigation())

@core_router.callback_query(F.data == "ui_fetch_referrals")
async def process_referral_query(callback: CallbackQuery):
    cnt = await DataEngine.get_referral_count(callback.from_user.id)
    rate = float(await DataEngine.get_setting("reward_per_referral", "10"))
    await callback.message.edit_text(f"👥 <b>Your Referral Network:</b>\n\n• Total Referrals: <b>{cnt} users</b>\n• Net Profits: <b>{cnt*rate:.2f} Birr</b>", reply_markup=generate_fallback_navigation())

@core_router.callback_query(F.data == "ui_fetch_link")
async def process_link_generation(callback: CallbackQuery):
    me = await bot.get_me()
    await callback.message.edit_text(f"🔗 <b>Your Invite Link:</b>\n\n<code>https://t.me/{me.username}?start={callback.from_user.id}</code>", reply_markup=generate_fallback_navigation())

# ─────────────────────────────────────────────────────────────────────────────
# WITHDRAWALS ENGINE & AUTOMATED CHANNEL LOGGING
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_initiate_withdrawal")
async def process_withdrawal_start(callback: CallbackQuery, state: FSMContext):
    user = await DataEngine.get_user(callback.from_user.id)
    min_w = float(await DataEngine.get_setting("min_withdrawal", "50"))
    if user["balance"] < min_w:
        return await callback.answer(f"❌ Minimum payout baseline is {min_w} Birr.", show_alert=True)
    await state.set_state(UserWithdrawalWorkflow.select_payout_gateway)
    await state.update_data(cached_balance=user["balance"], cached_minimum=min_w)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Telebirr / ቴሌብር", callback_data="gateway_telebirr")],
        [InlineKeyboardButton(text="❌ Cancel / ሰርዝ", callback_data="ui_return_home")]
    ])
    await callback.message.edit_text("💸 <b>Select Payout Endpoint:</b>", reply_markup=markup)

@core_router.callback_query(F.data == "gateway_telebirr", UserWithdrawalWorkflow.select_payout_gateway)
async def process_telebirr_selection(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserWithdrawalWorkflow.input_cash_volume)
    await callback.message.edit_text("<b>Specify the amount you wish to withdraw:</b>", reply_markup=generate_fallback_navigation())

@core_router.message(UserWithdrawalWorkflow.input_cash_volume)
async def process_cashout_volume(message: Message, state: FSMContext):
    s_data = await state.get_data()
    try:
        val = float(message.text.strip())
        assert s_data["cached_minimum"] <= val <= s_data["cached_balance"]
    except Exception:
        return await message.answer("❌ Invalid amount matching your limits.")
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
        return await message.answer("❌ Provided context name is too short.")
    await state.update_data(validated_title=title)
    s_data = await state.get_data()
    
    confirm_text = (
        f"⚠️ <b>Review Settlement Details</b>\n\n"
        f"• Platform: <code>Telebirr</code>\n"
        f"• Amount: <code>{s_data['validated_volume']:.2f} ETB</code>\n"
        f"• Holder: <code>{title}</code>\n"
        f"• Number: <code>{s_data['validated_phone']}</code>\n\nAuthorization requested."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Transact Payout", callback_data="action_payout_dispatch"),
        InlineKeyboardButton(text="❌ Abort / ሰርዝ", callback_data="ui_return_home")
    ]])
    await message.answer(confirm_text, reply_markup=markup)
    await state.set_state(UserWithdrawalWorkflow.payout_final_approval)

@core_router.callback_query(F.data == "action_payout_dispatch", UserWithdrawalWorkflow.payout_final_approval)
async def process_payout_dispatch(callback: CallbackQuery, state: FSMContext):
    s_data = await state.get_data()
    caller_id = callback.from_user.id
    user = await DataEngine.get_user(caller_id)
    if user["balance"] < s_data["validated_volume"]:
        return await callback.answer("❌ Settlement Error: Insufficient funds.", show_alert=True)

    ticket_id = await DataEngine.create_withdrawal(caller_id, s_data["validated_volume"], s_data["validated_title"], s_data["validated_phone"])
    await DataEngine.add_balance(caller_id, -s_data["validated_volume"])
    await state.clear()

    channeled_post_id = 0
    if PAYMENT_LOG_CHANNEL:
        try:
            alias = f"@{user['username']}" if user['username'] else "Private Profile"
            post_text = (
                f"⏳ <b>NEW WITHDRAWAL REQUESTED </b>\n\n"
                f"👤 <b>User Node:</b> {s_data['validated_title']} ({alias})\n"
                f"💰 <b>Requested Amount:</b> <code>ETB {s_data['validated_volume']:.2f}</code>\n"
                f"📱 <b>Method:</b> <code>Telebirr Portal</code>\n"
                f"📊 <b>Status:</b> <code>Pending Verification ⏳</code>\n\n"
                f"⏰ <b>Timestamp:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
            )
            broadcast_receipt = await bot.send_message(chat_id=PAYMENT_LOG_CHANNEL, text=post_text)
            channeled_post_id = broadcast_receipt.message_id
            await DataEngine.update_withdrawal_status(ticket_id, "pending", channeled_post_id)
        except Exception as e:
            logger.error(f"Channel Broadcast Error: {e}")

    admin_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve Ticket", callback_data=f"adm_payout_ap_{ticket_id}"),
        InlineKeyboardButton(text="❌ Deny Ticket", callback_data=f"adm_payout_rj_{ticket_id}")
    ]])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=f"📥 <b>Incoming Ticket #{ticket_id}</b>\nVolume: <b>{s_data['validated_volume']:.2f} Birr</b>", reply_markup=admin_markup)
        except Exception:
            pass

    await callback.message.edit_text("📨 <b>Withdrawal Submitted!</b> Processing inside 2-48 hours. Updates are sent to our log channel.", reply_markup=generate_dashboard_matrix(caller_id))

@core_router.callback_query(F.data.startswith("adm_payout_ap_"))
async def process_admin_approval(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    ticket_id = int(callback.data.split("_")[3])
    ticket = await DataEngine.get_withdrawal(ticket_id)
    if not ticket or ticket["status"] != "pending": return await callback.answer("Processed already.")
        
    await DataEngine.update_withdrawal_status(ticket_id, "approved", ticket["channel_post_id"])

    if PAYMENT_LOG_CHANNEL and ticket["channel_post_id"] != 0:
        try:
            channel_text = (
                f"✅ <b>PAYOUT SETTLEMENT COMPLETED SUCCESSFULLY</b>\n\n"
                f"👤 <b>Recipient:</b> {ticket['full_name']}\n"
                f"💰 <b>Amount :</b> <code>ETB {ticket['amount']:.2f}</code>\n"
                f"🚀 <b>Operational Registry:</b> Verified Success ✅"
            )
            await bot.send_photo(chat_id=PAYMENT_LOG_CHANNEL, photo=TELEBIRR_PROOF_IMAGE, caption=channel_text, reply_to_message_id=ticket["channel_post_id"])
        except Exception as e:
            logger.error(f"Channel photo confirmation failed: {e}")

    try:
        await bot.send_message(ticket["user_id"], f"🎉 Your cashout request of {ticket['amount']:.2f} Birr has been successfully processed!")
    except Exception: pass
    await callback.message.edit_text(callback.message.text + "\n\n✅ Approved.")

@core_router.callback_query(F.data.startswith("adm_payout_rj_"))
async def process_admin_rejection(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id): return
    ticket_id = int(callback.data.split("_")[3])
    ticket = await DataEngine.get_withdrawal(ticket_id)
    if not ticket or ticket["status"] != "pending": return await callback.answer("Evaluated prior.")
        
    await DataEngine.update_withdrawal_status(ticket_id, "rejected")
    await DataEngine.add_balance(ticket["user_id"], ticket["amount"])
    try:
        await bot.send_message(ticket["user_id"], "❌ Your withdrawal request was rejected. Assets returned.")
    except Exception: pass
    await callback.message.edit_text(callback.message.text + "\n\n❌ Rejected.")

# ─────────────────────────────────────────────────────────────────────────────
# OPERATION TERMINALS (ADMIN CONTROL STRAT)
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_admin_core")
async def process_admin_panel(callback: CallbackQuery):
    if evaluate_admin_access(callback.from_user.id):
        await callback.message.edit_text("⚙️ <b>Operational Admin Master Engine</b>", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_add_mand")
async def process_add_channel_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.append_mandatory_id)
    await callback.message.edit_text("🔴 <b>Enter Mandatory Channel ID (-100...):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.append_mandatory_id)
async def process_add_channel_id(message: Message, state: FSMContext):
    await state.update_data(ch_id=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_title)
    await message.answer("<b>Enter Channel Title:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_title)
async def process_add_channel_title(message: Message, state: FSMContext):
    await state.update_data(ch_title=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_url)
    await message.answer("<b>Enter Public/Private Invite Link:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_url)
async def process_add_channel_finalize(message: Message, state: FSMContext):
    s_data = await state.get_data()
    await state.clear()
    await DataEngine.add_force_channel(s_data["ch_id"], s_data["ch_title"], message.text.strip())
    await message.answer("✅ Channel Locked Successfully.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_rm_node")
async def process_rm_channel_menu(callback: CallbackQuery):
    active = await DataEngine.get_force_channels()
    buttons = []
    for node in active:
        buttons.append([InlineKeyboardButton(text=f"🗑 Delete {node['channel_name']}", callback_data=f"execute_rm_node_{node['channel_id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")])
    await callback.message.edit_text("<b>Select channel to decouple:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@core_router.callback_query(F.data.startswith("execute_rm_node_"))
async def process_rm_channel_action(callback: CallbackQuery):
    target = callback.data.replace("execute_rm_node_", "")
    await DataEngine.remove_force_channel(target)
    await callback.message.edit_text("✅ Decoupled successfully.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_edit_bal")
async def process_edit_balance_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.direct_balance_target_id)
    await callback.message.edit_text("<b>Enter Targeted Telegram User ID:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.direct_balance_target_id)
async def process_edit_balance_id(message: Message, state: FSMContext):
    await state.update_data(target_uid=int(message.text.strip()))
    await state.set_state(AdminConsoleWorkflow.direct_balance_volume)
    await message.answer("<b>Enter Adjustment Volume (e.g. 50 or -20):</b>")

@core_router.message(AdminConsoleWorkflow.direct_balance_volume)
async def process_edit_balance_final(message: Message, state: FSMContext):
    s_data = await state.get_data()
    await state.clear()
    await DataEngine.add_balance(s_data["target_uid"], float(message.text.strip()))
    await message.answer("✅ Ledger Adjusted.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_stats")
async def process_stats(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT COUNT(*) as pop, SUM(balance) as cap FROM users")
        u_row = await cur.fetchone()
    await callback.message.edit_text(f"📊 <b>Analytics Matrix:</b>\n\n• Profiles: <b>{u_row['pop'] or 0} users</b>\n• Capital Vol: <b>{u_row['cap'] or 0:.2f} ETB</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.callback_query(F.data == "adm_cmd_broadcast")
async def process_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.broadcast_intel_payload)
    await callback.message.edit_text("📢 <b>Enter Broadcast Payload Text:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.broadcast_intel_payload)
async def process_broadcast_execute(message: Message, state: FSMContext):
    text = message.text
    await state.clear()
    progress = await message.answer("⏳ Sending...")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id FROM users")
        nodes = await cur.fetchall()
    sc = 0
    for node in nodes:
        try:
            await bot.send_message(chat_id=node["user_id"], text=text)
            sc += 1
            await asyncio.sleep(0.04)
        except Exception: pass
    await progress.delete()
    await message.answer(f"✅ Dispatched to {sc} nodes.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_search")
async def process_search_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.lookup_individual_id)
    await callback.message.edit_text("🔍 <b>Enter User Telegram ID:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.lookup_individual_id)
async def process_search_execute(message: Message, state: FSMContext):
    await state.clear()
    user = await DataEngine.get_user(int(message.text.strip()))
    if not user: return await message.answer("Record not found.")
    await message.answer(f"👤 <b>Metrics Profile:</b>\n\n• Name: {user['full_name']}\n• Balance: <b>{user['balance']:.2f} Birr</b>\n• Banned: <b>{bool(user['is_banned'])}</b>", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_ban")
async def process_ban_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.banish_individual_id)
    await callback.message.edit_text("🚫 <b>Enter ID to Ban:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.banish_individual_id)
async def process_ban_execute(message: Message, state: FSMContext):
    await DataEngine.ban_user(int(message.text.strip()), 1)
    await state.clear()
    await message.answer("✅ Targeted profile blocked.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_unban")
async def process_unban_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.pardon_individual_id)
    await callback.message.edit_text("✅ <b>Enter ID to Pardon:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.pardon_individual_id)
async def process_unban_execute(message: Message, state: FSMContext):
    await DataEngine.ban_user(int(message.text.strip()), 0)
    await state.clear()
    await message.answer("✅ Pardoned successfully.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_reward")
async def process_reward_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.modify_referral_bounty)
    await callback.message.edit_text("<b>Enter New Reward Allocation Value:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_referral_bounty)
async def process_reward_execute(message: Message, state: FSMContext):
    await DataEngine.set_setting("reward_per_referral", message.text.strip())
    await state.clear()
    await message.answer("✅ Reward system settings updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_min_wd")
async def process_min_wd_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.modify_minimum_cashout)
    await callback.message.edit_text("<b>Enter New Minimum Cashout Threshold:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_minimum_cashout)
async def process_min_wd_execute(message: Message, state: FSMContext):
    await DataEngine.set_setting("min_withdrawal", message.text.strip())
    await state.clear()
    await message.answer("✅ Minimum baseline configuration updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_pending_tickets")
async def process_pending_inventory(callback: CallbackQuery):
    pending = await DataEngine.get_pending_withdrawals()
    await callback.message.edit_text(f"📥 Found <b>{len(pending)} active queue ticket(s)</b>.", reply_markup=generate_admin_dashboard())

# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI BACKEND SERVER MODULES
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
async def serve_frontend(uid: int = 0, ref: int = 0):
    try:
        with open("index.html", "r") as storage_file:
            loaded_html = storage_file.read()
        return HTMLResponse(content=loaded_html.replace("__BACKEND_URL__", WEBAPP_URL))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Frontend Template Missing: {e}")

@api_platform.post("/api/verify")
async def execute_verification(request: Request):
    incoming = await request.json()
    tg_user = parse_telegram_webapp_handshake(incoming.get("initData", ""))
    if not tg_user: raise HTTPException(status_code=403, detail="Signature breach.")
        
    client_id = int(tg_user["id"])
    ref_id = int(incoming.get("refId") or 0)

    if await DataEngine.is_verified(client_id):
        return JSONResponse({"status": "already_verified"})

    is_cloned = await DataEngine.find_duplicate(incoming.get("ip", ""), incoming.get("fingerprint", ""), client_id)
    is_vpn = incoming.get("isVpn") or await execute_network_vpn_lookup(incoming.get("ip", ""))
    
    if is_cloned or is_vpn:
        await DataEngine.create_user(client_id, tg_user.get("username", ""), tg_user.get("first_name", ""))
        await DataEngine.ban_user(client_id, 1)
        return JSONResponse({"status": "blocked"})

    await DataEngine.create_user(client_id, tg_user.get("username", ""), tg_user.get("first_name", ""), ref_id or None)
    await DataEngine.save_verification(client_id, incoming.get("ip", ""), incoming.get("ua", ""), incoming.get("fingerprint", ""))

    if ref_id and ref_id != client_id:
        bounty = float(await DataEngine.get_setting("reward_per_referral", "10"))
        await DataEngine.add_balance(ref_id, bounty)
        try:
            await bot.send_message(chat_id=ref_id, text=f"🎉 <b>Network Bounty!</b> Referral verified. <code>+{bounty} Birr</code> credited.")
        except Exception: pass

    try:
        await bot.send_message(chat_id=client_id, text="✅ <b>Verification Confirmed!</b> Access granted.", reply_markup=generate_dashboard_matrix(client_id))
    except Exception: pass

    return JSONResponse({"status": "verified"})

if __name__ == "__main__":
    uvicorn.run("bot:api_platform", host="0.0.0.0", port=8000, log_level="info")

"""
================================================================================
                    TELEGRAM ADVANCED REFERRAL BOT SYSTEM
         [ Complete Production Engine - All-In-One Unified Architecture ]
================================================================================
This script contains the entire bot framework, embedded production sqlite database
handlers, robust security modules against multi-accounts/VPNs, sneaky channel 
advertising engine, and automated Telebirr payout confirmation logs via channels.
================================================================================
"""

import os
import sys
import json
import hmac
import uuid
import logging
import asyncio
import hashlib
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
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)

# FastAPI Engine Imports
from fastapi import FastAPI, Request, HTTPException, Depends
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
PAYMENT_LOG_CHANNEL = os.getenv("PAYMENT_LOG_CHANNEL", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
PROXYCHECK_API_KEY = os.getenv("PROXYCHECK_API_KEY", "")
DATABASE_FILENAME = "bot_production_core.db"

# 📸 Telebirr Verification Target Banner FileID
TELEBIRR_PROOF_IMAGE = "AgACAgQAAxkBAAOYai38ooud5iofBd3aDGuCiX273t8AAj4PaxsYl3BR78MpfA_cDpkBAAMCAAN4AAM8BA"

# Ensure Protocol Prefix For WebApp
if not WEBAPP_URL.startswith(("http://", "https://")):
    WEBAPP_URL = f"https://{WEBAPP_URL}"

# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENT STORAGE DATA ENGINE (Embedded Database Layer)
# ─────────────────────────────────────────────────────────────────────────────
class DataEngine:
    @staticmethod
    async def init_database():
        """Initializes the SQLite tables inside the unified runtime."""
        logger.info("Initializing relational database tables...")
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            # Core Users Profile Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS system_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    balance REAL DEFAULT 0.0,
                    referrer_id INTEGER,
                    is_banned INTEGER DEFAULT 0,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Unified Traffic Force Join / Sneaky Channels Table
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS marketing_channels (
                    channel_id TEXT PRIMARY KEY,
                    channel_name TEXT,
                    invite_link TEXT,
                    is_optional INTEGER DEFAULT 0,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Ledger Cashouts Table
            await conn.execute("""
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
                )
            """)
            # Multi-Account & Node IP Ledger
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS device_verifications (
                    user_id INTEGER PRIMARY KEY,
                    ip_address TEXT,
                    fingerprint_hash TEXT,
                    verification_method TEXT,
                    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Fast System Metadata / KV Registry
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS system_metadata (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT
                )
            """)
            await conn.commit()
        logger.info("Database validation framework executed successfully.")

    @staticmethod
    async def fetch_user(user_id: int):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM system_users WHERE user_id = ?", (user_id,)) as cursor:
                return await cursor.fetchone()

    @staticmethod
    async def record_user(user_id: int, username: str, full_name: str, referrer_id: int = None):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            try:
                await conn.execute(
                    "INSERT INTO system_users (user_id, username, full_name, referrer_id) VALUES (?, ?, ?, ?)",
                    (user_id, username, full_name, referrer_id)
                )
                await conn.commit()
            except aiosqlite.IntegrityError:
                pass

    @staticmethod
    async def modify_balance(user_id: int, volume: float):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute(
                "UPDATE system_users SET balance = balance + ? WHERE user_id = ?", 
                (volume, user_id)
            )
            await conn.commit()

    @staticmethod
    async def check_device_status(user_id: int) -> bool:
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            async with conn.execute("SELECT 1 FROM device_verifications WHERE user_id = ?", (user_id,)) as cursor:
                res = await cursor.fetchone()
                return res is not None

    @staticmethod
    async def register_verification(user_id: int, ip: str, fingerprint: str, method: str):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO device_verifications (user_id, ip_address, fingerprint_hash, verification_method) VALUES (?, ?, ?, ?)",
                (user_id, ip, fingerprint, method)
            )
            await conn.commit()

    @staticmethod
    async def discover_clones(ip: str, fingerprint: str, current_user: int) -> bool:
        if not fingerprint or fingerprint == "undefined":
            return False
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            async with conn.execute(
                "SELECT 1 FROM device_verifications WHERE (ip_address = ? OR fingerprint_hash = ?) AND user_id != ?",
                (ip, fingerprint, current_user)
            ) as cursor:
                res = await cursor.fetchone()
                return res is not None

    @staticmethod
    async def banish_user(user_id: int, status: int = 1):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute("UPDATE system_users SET is_banned = ? WHERE user_id = ?", (status, user_id))
            await conn.commit()

    @staticmethod
    async def compute_referrals(user_id: int) -> int:
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            async with conn.execute("SELECT COUNT(*) FROM system_users WHERE referrer_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    @staticmethod
    async def schedule_withdrawal(user_id: int, amount: float, name: str, phone: str, method: str) -> int:
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            cursor = await conn.execute(
                "INSERT INTO user_withdrawals (user_id, amount, full_name, phone_number, payout_method) VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, name, phone, method)
            )
            await conn.commit()
            return cursor.lastrowid

    @staticmethod
    async def get_withdrawal_ticket(w_id: int):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM user_withdrawals WHERE id = ?", (w_id,)) as cursor:
                return await cursor.fetchone()

    @staticmethod
    async def update_withdrawal_log(w_id: int, status: str, post_id: int = 0):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute(
                "UPDATE user_withdrawals SET status = ?, channel_post_id = ? WHERE id = ?",
                (status, post_id, w_id)
            )
            await conn.commit()

    @staticmethod
    async def pull_pending_tickets():
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM user_withdrawals WHERE status = 'pending'") as cursor:
                return await cursor.fetchall()

    @staticmethod
    async def write_config(key: str, value: str):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute("INSERT OR REPLACE INTO system_metadata (meta_key, meta_value) VALUES (?, ?)", (key, value))
            await conn.commit()

    @staticmethod
    async def read_config(key: str, fallback: str) -> str:
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            async with conn.execute("SELECT meta_value FROM system_metadata WHERE meta_key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else fallback

    @staticmethod
    async def unlink_channel(channel_id: str):
        async with aiosqlite.connect(DATABASE_FILENAME) as conn:
            await conn.execute("DELETE FROM marketing_channels WHERE channel_id = ?", (channel_id,))
            await conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM RUNTIME FSM CONFIGURATION
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
    append_sneaky_id         = State()
    append_sneaky_title      = State()
    append_sneaky_url        = State()
    direct_balance_target_id = State()
    direct_balance_volume    = State()
    broadcast_intel_payload  = State()
    lookup_individual_id     = State()
    banish_individual_id     = State()
    pardon_individual_id     = State()

# ─────────────────────────────────────────────────────────────────────────────
# CORE TELEGRAM BOT CONTROLLERS & SECURE ROUTERS
# ─────────────────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
storage_memory = MemoryStorage()
dp = Dispatcher(storage=storage_memory)
core_router = Router()

# Security Validation Logic
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
    except Exception as exc:
        logger.error(f"Error evaluating WebApp handshake signature: {exc}")
        return None

async def inspect_compulsory_memberships(user_id: int) -> list:
    """Verifies mandatory target systems strictly."""
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM marketing_channels WHERE is_optional = 0") as cursor:
            mandatory_nodes = await cursor.fetchall()
            
    unmatched_nodes = []
    for node in mandatory_nodes:
        try:
            member_receipt = await bot.get_chat_member(chat_id=node["channel_id"], user_id=user_id)
            if member_receipt.status in [ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED]:
                unmatched_nodes.append(node)
        except Exception as chat_err:
            logger.warning(f"Unable to read status properties for user {user_id} on {node['channel_id']}: {chat_err}")
            unmatched_nodes.append(node)
    return unmatched_nodes

async def extract_all_channels() -> list:
    """Pulls all tracking records (Mandatory mixed with Sneaky Optionals)."""
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM marketing_channels") as cursor:
            return await cursor.fetchall()

async def execute_network_vpn_lookup(client_ip: str) -> bool:
    if not client_ip or client_ip in ("127.0.0.1", "::1", "unknown"):
        return False
    try:
        api_authentication_parameter = f"&key={PROXYCHECK_API_KEY}" if PROXYCHECK_API_KEY else ""
        query_endpoint = f"https://proxycheck.io/v2/{client_ip}?vpn=1{api_authentication_parameter}"
        async with httpx.AsyncClient(timeout=4) as network_client:
            response_payload = await network_client.get(query_endpoint)
            parsed_json = response_payload.json()
            return parsed_json.get(client_ip, {}).get("proxy") == "yes"
    except Exception as network_error:
        logger.error(f"Proxycheck node experienced a connection bottleneck: {network_error}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# UI/UX INTERACTIVE KEYBOARD FACTORIES
# ─────────────────────────────────────────────────────────────────────────────
def generate_verification_widget(user_id: int, target_referrer: int) -> InlineKeyboardMarkup:
    target_destination_url = f"{WEBAPP_URL}/verify?uid={user_id}&ref={target_referrer}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔐 Open Mini App & Verify", web_app=WebAppInfo(url=target_destination_url))
    ]])

def generate_dashboard_matrix(user_id: int) -> InlineKeyboardMarkup:
    markup_blueprint = [
        [InlineKeyboardButton(text="💰 Balance / ሒሳብ", callback_data="ui_fetch_balance"), InlineKeyboardButton(text="👥 Referrals / ጋባዦች", callback_data="ui_fetch_referrals")],
        [InlineKeyboardButton(text="🔗 My Link / ሊንኬ", callback_data="ui_fetch_link"), InlineKeyboardButton(text="💸 Withdraw / ብር ማውጫ", callback_data="ui_initiate_withdrawal")],
    ]
    if evaluate_admin_access(user_id):
        markup_blueprint.append([InlineKeyboardButton(text="⚙️ Admin Control Center", callback_data="ui_admin_core")])
    return InlineKeyboardMarkup(inline_keyboard=markup_blueprint)

def generate_admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Set Referral Reward", callback_data="adm_cmd_reward"), InlineKeyboardButton(text="💵 Set Min Withdrawal", callback_data="adm_cmd_min_wd")],
        [InlineKeyboardButton(text="✍️ Edit User Balance", callback_data="adm_cmd_edit_bal"), InlineKeyboardButton(text="📊 Bot Statistics", callback_data="adm_cmd_stats")],
        [InlineKeyboardButton(text="🔴 Add Force Channel", callback_data="adm_cmd_add_mand"), InlineKeyboardButton(text="🟢 Add Optional Channel", callback_data="adm_cmd_add_sneaky")],
        [InlineKeyboardButton(text="🗑 Remove Channel", callback_data="adm_cmd_rm_node"), InlineKeyboardButton(text="📋 List Channels", callback_data="adm_cmd_list_nodes")],
        [InlineKeyboardButton(text="📥 Pending Withdrawals", callback_data="adm_cmd_pending_tickets"), InlineKeyboardButton(text="📢 Broadcast Message", callback_data="adm_cmd_broadcast")],
        [InlineKeyboardButton(text="🔍 Search User Info", callback_data="adm_cmd_search")],
        [InlineKeyboardButton(text="🚫 Ban User", callback_data="adm_cmd_ban"), InlineKeyboardButton(text="✅ Unban User", callback_data="adm_cmd_unban")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="ui_return_home")],
    ])

def generate_fallback_navigation(target_callback="ui_return_home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Back", callback_data=target_callback)]])

# ─────────────────────────────────────────────────────────────────────────────
# USER EXPERIENCE OVERVIEW HANDLERS (Telegram Endpoint Logic)
# ─────────────────────────────────────────────────────────────────────────────
@core_router.message(CommandStart())
async def process_start_command(message: Message, state: FSMContext):
    await state.clear()
    caller_id = message.from_user.id
    command_segments = message.text.split()
    
    extracted_argument = command_segments[1] if len(command_segments) > 1 else ""
    validated_referrer = int(extracted_argument) if extracted_argument.isdigit() and int(extracted_argument) != caller_id else 0

    account_profile = await DataEngine.fetch_user(caller_id)
    if account_profile and account_profile["is_banned"]:
        return await message.answer("🚫 <b>Access Denied:</b> Your account profile has been blacklisted from our networks.")

    unjoined_dependencies = await inspect_compulsory_memberships(caller_id)
    if unjoined_dependencies:
        mixed_advertising_flow = await extract_all_channels()
        if validated_referrer:
            await state.update_data(stashed_referrer_id=validated_referrer)
            
        keyboard_assembler = []
        for channel in mixed_advertising_flow:
            keyboard_assembler.append([InlineKeyboardButton(text=f"➕ {channel['channel_name']}", url=channel["invite_link"])])
            
        keyboard_assembler.append([InlineKeyboardButton(text="✅ Joined — Check System Status", callback_data="ui_revalidate_channels")])
        inline_response_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_assembler)
        return await message.answer(
            "⚠️ <b>System Access Blocked:</b>\nTo communicate with our infrastructure, you are requested to join our networks below:", 
            reply_markup=inline_response_markup
        )

    if await DataEngine.check_device_status(caller_id):
        return await message.answer("👋 <b>Welcome Back!</b> Access granted to your decentralized control terminal.", reply_markup=generate_dashboard_matrix(caller_id))
    
    await message.answer(
        "⚠️ <b>Advanced Anti-Bot Verification Demanded:</b>\nOur network detects unverified telemetry footprints. Press the module below to verify.", 
        reply_markup=generate_verification_widget(caller_id, validated_referrer)
    )

@core_router.callback_query(F.data == "ui_revalidate_channels")
async def process_channel_revalidation(callback: CallbackQuery, state: FSMContext):
    caller_id = callback.from_user.id
    unjoined_dependencies = await inspect_compulsory_memberships(caller_id)
    
    if unjoined_dependencies:
        await callback.answer("❌ Verification parameters failed. You haven't joined all required channels.", show_alert=True)
    else:
        await callback.message.delete()
        session_variables = await state.get_data()
        saved_referrer = session_variables.get("stashed_referrer_id", 0)
        await state.clear()
        
        if await DataEngine.check_device_status(caller_id):
            await callback.message.answer("✅ Device identity clear. Welcome!", reply_markup=generate_dashboard_matrix(caller_id))
        else:
            await callback.message.answer("✅ Gateway confirmed. Final step: Complete Mini App device authentication.", reply_markup=generate_verification_widget(caller_id, saved_referrer))

@core_router.callback_query(F.data == "ui_return_home")
async def process_navigation_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 <b>Main Core Dashboard / ዋና ማውጫ</b>", reply_markup=generate_dashboard_matrix(callback.from_user.id))

@core_router.callback_query(F.data == "ui_fetch_balance")
async def process_balance_query(callback: CallbackQuery):
    account_data = await DataEngine.fetch_user(callback.from_user.id)
    minimum_limit = await DataEngine.read_config("min_withdrawal", "50")
    response_text = (
        f"💰 <b>Your Available Assets Ledger:</b>\n\n"
        f"• Current Balance: <code>{account_data['balance']:.2f} Birr</code>\n"
        f"• Network Minimum Threshold: <code>{minimum_limit} Birr</code>"
    )
    await callback.message.edit_text(response_text, reply_markup=generate_fallback_navigation())

@core_router.callback_query(F.data == "ui_fetch_referrals")
async def process_referral_query(callback: CallbackQuery):
    referrals_count = await DataEngine.compute_referrals(callback.from_user.id)
    bounty_rate = float(await DataEngine.read_config("reward_per_referral", "10"))
    total_revenue = referrals_count * bounty_rate
    response_text = (
        f"👥 <b>Your Referral Network Matrix:</b>\n\n"
        f"• Total Direct Connections: <b>{referrals_count} Users</b>\n"
        f"• Total Network Profits: <b>{total_revenue:.2f} Birr</b>"
    )
    await callback.message.edit_text(response_text, reply_markup=generate_fallback_navigation())

@core_router.callback_query(F.data == "ui_fetch_link")
async def process_link_generation(callback: CallbackQuery):
    identity_profile = await bot.get_me()
    response_text = (
        f"🔗 <b>Your Monetized Referral Pipeline:</b>\n\n"
        f"Share this encrypted invite link to receive passive network allocations:\n"
        f"<code>https://t.me/{identity_profile.username}?start={callback.from_user.id}</code>"
    )
    await callback.message.edit_text(response_text, reply_markup=generate_fallback_navigation())

# ─────────────────────────────────────────────────────────────────────────────
# TRANSACTION MANAGEMENT & CHANNEL OVERWRITE PIPELINE (Advanced Channel Payout Layer)
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_initiate_withdrawal")
async def process_withdrawal_pipeline_start(callback: CallbackQuery, state: FSMContext):
    user_profile = await DataEngine.fetch_user(callback.from_user.id)
    minimum_allowed_cashout = float(await DataEngine.read_config("min_withdrawal", "50"))
    
    if user_profile["balance"] < minimum_allowed_cashout:
        return await callback.answer(f"❌ Transaction Blocked: You must accumulate at least {minimum_allowed_cashout} Birr to cashout.", show_alert=True)
        
    await state.set_state(UserWithdrawalWorkflow.select_payout_gateway)
    await state.update_data(cached_balance=user_profile["balance"], cached_minimum=minimum_allowed_cashout)
    
    navigation_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Telebirr / ቴሌብር", callback_data="gateway_telebirr")],
        [InlineKeyboardButton(text="❌ Cancel Operation", callback_data="ui_return_home")]
    ])
    await callback.message.edit_text("💸 <b>Select Payout Endpoint / የማውጫ መንገድ ይምረጡ፦</b>", reply_markup=navigation_markup)

@core_router.callback_query(F.data == "gateway_telebirr", UserWithdrawalWorkflow.select_payout_gateway)
async def process_telebirr_selection(callback: CallbackQuery, state: FSMContext):
    await state.update_data(selected_gateway="Telebirr")
    await state.set_state(UserWithdrawalWorkflow.input_cash_volume)
    await callback.message.edit_text("<b>Enter Payout Volume / የብር መጠን ያስገቡ፦</b>", reply_markup=generate_fallback_navigation())

@core_router.message(UserWithdrawalWorkflow.input_cash_volume)
async def process_cashout_volume_input(message: Message, state: FSMContext):
    session_parameters = await state.get_data()
    try:
        user_input_volume = float(message.text.strip())
        assert session_parameters["cached_minimum"] <= user_input_volume <= session_parameters["cached_balance"]
    except Exception:
        return await message.answer("❌ <b>Numeric Conflict:</b> Enter a valid amount matching your wallet limitations.")
        
    await state.update_data(validated_volume=user_input_volume)
    await state.set_state(UserWithdrawalWorkflow.provide_mobile_digits)
    await message.answer("📱 <b>Provide Destination Account Number / የቴሌብር ስልክ ቁጥር ያስገቡ፦</b>")

@core_router.message(UserWithdrawalWorkflow.provide_mobile_digits)
async def process_mobile_number_input(message: Message, state: FSMContext):
    cleaned_input = message.text.strip()
    if len(cleaned_input) < 9:
        return await message.answer("❌ <b>Format Deviation:</b> Provide a functional mobile endpoint.")
        
    await state.update_data(validated_phone=cleaned_input)
    await state.set_state(UserWithdrawalWorkflow.provide_account_title)
    await message.answer("📝 <b>Enter Legitimate Account Holder Title / ሙሉ ስም ያስገቡ፦</b>")

@core_router.message(UserWithdrawalWorkflow.provide_account_title)
async def process_account_title_input(message: Message, state: FSMContext):
    cleaned_title = message.text.strip()
    if len(cleaned_title) < 3:
        return await message.answer("❌ <b>Input Defect:</b> Name field string length too short.")
        
    await state.update_data(validated_title=cleaned_title)
    session_data = await state.get_data()
    
    confirmation_template = (
        f"⚠️ <b>Review Asset Settlement Parameters</b>\n\n"
        f"• Settlement Platform: <code>{session_data['selected_gateway']}</code>\n"
        f"• Payout Weight: <code>{session_data['validated_volume']:.2f} ETB</code>\n"
        f"• Holder Identity: <code>{session_data['validated_title']}</code>\n"
        f"• Endpoint Channel: <code>{session_data['validated_phone']}</code>\n\n"
        f"Are you authorized to dispatch this transactional operation?"
    )
    
    navigation_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Transact Payout", callback_data="action_payout_dispatch"),
        InlineKeyboardButton(text="❌ Abort Matrix", callback_data="ui_return_home")
    ]])
    await message.answer(confirmation_template, reply_markup=navigation_markup)
    await state.set_state(UserWithdrawalWorkflow.payout_final_approval)

@core_router.callback_query(F.data == "action_payout_dispatch", UserWithdrawalWorkflow.payout_final_approval)
async def process_payout_finalization(callback: CallbackQuery, state: FSMContext):
    session_variables = await state.get_data()
    caller_id = callback.from_user.id
    user_live_profile = await DataEngine.fetch_user(caller_id)
    
    if user_live_profile["balance"] < session_variables["validated_volume"]:
        return await callback.answer("❌ Settlement Error: Ledger transaction collision detected.", show_alert=True)

    ticket_id = await DataEngine.schedule_withdrawal(
        user_id=caller_id,
        amount=session_variables["validated_volume"],
        name=session_variables["validated_title"],
        phone=session_variables["validated_phone"],
        method=session_variables["selected_gateway"]
    )
    
    await DataEngine.modify_balance(caller_id, -session_variables["validated_volume"])
    await state.clear()

    channeled_post_id = 0
    if PAYMENT_LOG_CHANNEL:
        try:
            account_alias = f"@{user_live_profile['username']}" if user_live_profile.get('username') else "Private Topology"
            broadcast_notification_text = (
                f"✅ <b>NEW PAYOUT TRANSACTION REQUESTED</b>\n\n"
                f"👤 <b>Claimant Node:</b> {session_variables['validated_title']} ({account_alias})\n"
                f"🆔 <b>Node Passport:</b> <code>{caller_id}</code>\n\n"
                f"💰 <b>Dispatched Resource:</b> <code>ETB {session_variables['validated_volume']:.2f}</code>\n"
                f"📱 <b>Payment Infrastructure:</b> <code>Telebirr API Portal</code>\n"
                f"📝 <b>Routing Footprint:</b> {session_variables['validated_title']} - {session_variables['validated_phone']}\n\n"
                f"⏰ <b>Timestamp:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
            )
            broadcast_receipt = await bot.send_message(chat_id=PAYMENT_LOG_CHANNEL, text=broadcast_notification_text)
            channeled_post_id = broadcast_receipt.message_id
            await DataEngine.update_withdrawal_log(ticket_id, "pending", channeled_post_id)
        except Exception as channel_post_error:
            logger.error(f"Failed broadcasting transaction telemetry to public channel node: {channel_post_error}")

    admin_alert_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve Ticket", callback_data=f"adm_payout_ap_{ticket_id}"),
        InlineKeyboardButton(text="❌ Deny Ticket", callback_data=f"adm_payout_rj_{ticket_id}")
    ]])
    
    for administrator_passport in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=administrator_passport,
                text=f"📥 <b>Incoming Settlement Ticket #{ticket_id}</b>\n\nUser ID: <code>{caller_id}</code>\nVolume: <b>{session_variables['validated_volume']:.2f} Birr</b>",
                reply_markup=admin_alert_markup
            )
        except Exception:
            pass

    await callback.message.edit_text("📨 <b>Transaction Sent:</b> Settlement parameters successfully queued. Track channel nodes for status updates.")

@core_router.callback_query(F.data.startswith("adm_payout_ap_"))
async def process_admin_ticket_approval(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
        
    extracted_parameters = callback.data.split("_")
    ticket_id = int(extracted_parameters[3])
    
    withdrawal_record = await DataEngine.get_withdrawal_ticket(ticket_id)
    if not withdrawal_record or withdrawal_record["status"] != "pending":
        return await callback.answer("Ticket already processed by another terminal.")
        
    await DataEngine.update_withdrawal_log(ticket_id, "approved", withdrawal_record["channel_post_id"])

    if PAYMENT_LOG_CHANNEL and withdrawal_record["channel_post_id"] != 0:
        try:
            success_channel_narration = (
                f"✅ <b>PAYOUT SETTLEMENT COMPLETED SUCCESSFULLY</b>\n\n"
                f"🎉 The financial network has dispatched user allocations successfully.\n\n"
                f"👤 <b>Recipient:</b> {withdrawal_record['full_name']}\n"
                f"💰 <b>Settled Weight:</b> <code>ETB {withdrawal_record['amount']:.2f}</code>\n"
                f"🚀 <b>Operational Registry:</b> Verified Success ✅\n\n"
                f"🤖 <b>Bot Terminal:</b> @{(await bot.get_me()).username}"
            )
            await bot.send_photo(
                chat_id=PAYMENT_LOG_CHANNEL,
                photo=TELEBIRR_PROOF_IMAGE,
                caption=success_channel_narration,
                reply_to_message_id=withdrawal_record["channel_post_id"]
            )
        except Exception as threading_err:
            logger.error(f"Failed to generate transactional reply metadata thread on node: {threading_err}")

    try:
        await bot.send_message(withdrawal_record["user_id"], f"🎉 <b>Settlement Alert:</b> Your payout request of {withdrawal_record['amount']:.2f} Birr has been processed!")
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n✅ <b>Result:</b> Approved & Threaded successfully.")

@core_router.callback_query(F.data.startswith("adm_payout_rj_"))
async def process_admin_ticket_rejection(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
        
    ticket_id = int(callback.data.split("_")[3])
    withdrawal_record = await DataEngine.get_withdrawal_ticket(ticket_id)
    
    if not withdrawal_record or withdrawal_record["status"] != "pending":
        return await callback.answer("Ticket neutralized or evaluated prior.")
        
    await DataEngine.update_withdrawal_log(ticket_id, "rejected")
    await DataEngine.modify_balance(withdrawal_record["user_id"], withdrawal_record["amount"])
    
    try:
        await bot.send_message(withdrawal_record["user_id"], "❌ <b>Settlement Refusal:</b> Your cashout request was rejected. Assets returned to balance.")
    except Exception:
        pass
        
    await callback.message.edit_text(callback.message.text + "\n\n❌ <b>Result:</b> Rejected & Assets Reversed.")

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN TERMINAL ROUTING & SYSTEM COMMAND ACTIONS
# ─────────────────────────────────────────────────────────────────────────────
@core_router.callback_query(F.data == "ui_admin_core")
async def process_admin_panel_hub(callback: CallbackQuery):
    if not evaluate_admin_access(callback.from_user.id):
        return
    await callback.message.edit_text("⚙️ <b>Advanced Operational System Master Configuration</b>", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_add_mand")
async def process_admin_add_mandatory_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.append_mandatory_id)
    await callback.message.edit_text("🔴 <b>Enter Mandatory Target Channel ID (-100...):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.append_mandatory_id)
async def process_admin_add_mandatory_id(message: Message, state: FSMContext):
    await state.update_data(ch_id=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_title)
    await message.answer("<b>Enter Channel Display Title:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_title)
async def process_admin_add_mandatory_title(message: Message, state: FSMContext):
    await state.update_data(ch_title=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_mandatory_url)
    await message.answer("<b>Enter Public / Private Invite Link:</b>")

@core_router.message(AdminConsoleWorkflow.append_mandatory_url)
async def process_admin_add_mandatory_finalize(message: Message, state: FSMContext):
    session_data = await state.get_data()
    await state.clear()
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        await conn.execute(
            "INSERT INTO marketing_channels (channel_id, channel_name, invite_link, is_optional) VALUES (?, ?, ?, 0)",
            (session_data["ch_id"], session_data["ch_title"], message.text.strip())
        )
        await conn.commit()
    await message.answer("🔴 <b>Target Node Registered:</b> Compulsory join rule locked successfully.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_add_sneaky")
async def process_admin_add_sneaky_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.append_sneaky_id)
    await callback.message.edit_text("🟢 <b>Enter Optional Sneaky Channel ID (-100...):</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.append_sneaky_id)
async def process_admin_add_sneaky_id(message: Message, state: FSMContext):
    await state.update_data(sn_id=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_sneaky_title)
    await message.answer("<b>Enter Optional Channel Title:</b>")

@core_router.message(AdminConsoleWorkflow.append_sneaky_title)
async def process_admin_add_sneaky_title(message: Message, state: FSMContext):
    await state.update_data(sn_title=message.text.strip())
    await state.set_state(AdminConsoleWorkflow.append_sneaky_url)
    await message.answer("<b>Enter Network Invite Link:</b>")

@core_router.message(AdminConsoleWorkflow.append_sneaky_url)
async def process_admin_add_sneaky_finalize(message: Message, state: FSMContext):
    session_data = await state.get_data()
    await state.clear()
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        await conn.execute(
            "INSERT INTO marketing_channels (channel_id, channel_name, invite_link, is_optional) VALUES (?, ?, ?, 1)",
            (session_data["sn_id"], session_data["sn_title"], message.text.strip())
        )
        await conn.commit()
    await message.answer("🟢 <b>Sneaky Mode Activated:</b> Optional channel linked safely without validation dependency.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_list_nodes")
async def process_admin_channel_listing(callback: CallbackQuery):
    node_collections = await extract_all_channels()
    compilation_buffer = "📋 <b>Active Infrastructure Nodes Registry:</b>\n\n"
    for counter, node in enumerate(node_collections, 1):
        classification = "🟢 Optional Sneaky" if node["is_optional"] == 1 else "🔴 Mandatory Node"
        compilation_buffer += f"{counter}. <b>{node['channel_name']}</b> ({classification})\nID: <code>{node['channel_id']}</code>\n\n"
    await callback.message.edit_text(compilation_buffer, reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.callback_query(F.data == "adm_cmd_rm_node")
async def process_admin_removal_menu(callback: CallbackQuery):
    active_nodes = await extract_all_channels()
    buttons_assembler = []
    for node in active_nodes:
        buttons_assembler.append([InlineKeyboardButton(text=f"🗑 Delete {node['channel_name']}", callback_data=f"execute_rm_node_{node['channel_id']}")])
    buttons_assembler.append([InlineKeyboardButton(text="🔙 Back", callback_data="ui_admin_core")])
    await callback.message.edit_text("<b>Select target node allocation to decouple:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons_assembler))

@core_router.callback_query(F.data.startswith("execute_rm_node_"))
async def process_admin_removal_action(callback: CallbackQuery):
    target_node_id = callback.data.replace("execute_rm_node_", "")
    await DataEngine.unlink_channel(target_node_id)
    await callback.message.edit_text("✅ Target entry dissociated completely.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_edit_bal")
async def process_admin_balance_modification_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.direct_balance_target_id)
    await callback.message.edit_text("<b>Enter Targeted Individual Telegram Identifier:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.direct_balance_target_id)
async def process_admin_balance_modification_id(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        return await message.answer("ID must be numeric.")
    await state.update_data(target_uid=int(message.text.strip()))
    await state.set_state(AdminConsoleWorkflow.direct_balance_volume)
    await message.answer("<b>Enter Ledger Adjustment Volume (e.g. 200 or -150):</b>")

@core_router.message(AdminConsoleWorkflow.direct_balance_volume)
async def process_admin_balance_modification_finalize(message: Message, state: FSMContext):
    session_data = await state.get_data()
    await state.clear()
    try:
        parsed_delta = float(message.text.strip())
        await DataEngine.modify_balance(session_data["target_uid"], parsed_delta)
        await message.answer("✅ User financial ledger adjusted successfully.", reply_markup=generate_admin_dashboard())
    except Exception as balance_err:
        await message.answer(f"Operation failed due to numerical evaluation discrepancy: {balance_err}")

@core_router.callback_query(F.data == "adm_cmd_stats")
async def process_admin_analytics(callback: CallbackQuery):
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT COUNT(*) as population, SUM(balance) as capital FROM system_users") as u_cursor:
            user_row = await u_cursor.fetchone()
        async with conn.execute("SELECT COUNT(*) as volume FROM user_withdrawals WHERE status='approved'") as w_cursor:
            withdrawal_row = await w_cursor.fetchone()
            
    analytics_template = (
        f"📊 <b>Network Analytics Matrix Dashboard:</b>\n\n"
        f"• Network Node Population: <b>{user_row['population'] or 0} active profiles</b>\n"
        f"• Floating Asset Allocations: <b>{user_row['capital'] or 0:.2f} ETB</b>\n"
        f"• Successful Payout Transactions: <b>{withdrawal_row['volume'] or 0} dispatches</b>"
    )
    await callback.message.edit_text(analytics_template, reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.callback_query(F.data == "adm_cmd_broadcast")
async def process_admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.broadcast_intel_payload)
    await callback.message.edit_text("📢 <b>Enter Information Payload for Global Transmission:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.broadcast_intel_payload)
async def process_admin_broadcast_execute(message: Message, state: FSMContext):
    broadcast_message_string = message.text
    await state.clear()
    progress_status_message = await message.answer("⏳ <b>Executing global dispatch stream...</b>")
    
    async with aiosqlite.connect(DATABASE_FILENAME) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT user_id FROM system_users") as cursor:
            global_user_nodes = await cursor.fetchall()
            
    success_dispatch_counter = 0
    for node in global_user_nodes:
        try:
            await bot.send_message(chat_id=node["user_id"], text=broadcast_message_string)
            success_dispatch_counter += 1
            await asyncio.sleep(0.04) # Prevent flooding
        except Exception:
            pass
            
    await progress_status_message.delete()
    await message.answer(f"✅ <b>Global Transmission Dispatched:</b> Successfully reached {success_dispatch_counter} nodes.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_search")
async def process_admin_search_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.lookup_individual_id)
    await callback.message.edit_text("🔍 <b>Enter User Telegram Passport Identity String:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.lookup_individual_id)
async def process_admin_search_execute(message: Message, state: FSMContext):
    input_string = message.text.strip()
    await state.clear()
    if not input_string.isdigit():
        return await message.answer("Invalid identity query configuration format.")
        
    user_record = await DataEngine.fetch_user(int(input_string))
    if not user_record:
        return await message.answer("Target user identity record not present inside system space.")
        
    compiled_result = (
        f"👤 <b>User Node Metrics Profile:</b>\n\n"
        f"• Passport Identity: <code>{user_record['user_id']}</code>\n"
        f"• Base System Label: {user_record['full_name']}\n"
        f"• System Username Alias: @{user_record['username'] or 'No Alias'}\n"
        f"• Wallet Reserves: <b>{user_record['balance']:.2f} Birr</b>\n"
        f"• Blacklist Quarantine Status: <b>{bool(user_record['is_banned'])}</b>"
    )
    await message.answer(compiled_result, reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_ban")
async def process_admin_ban_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.banish_individual_id)
    await callback.message.edit_text("🚫 <b>Enter Destination Target ID to Blacklist:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.banish_individual_id)
async def process_admin_ban_execute(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        return await message.answer("Numeric format exception.")
    await DataEngine.banish_user(int(message.text.strip()), 1)
    await state.clear()
    await message.answer("✅ Target identity block finalized. Permissions revoked.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_unban")
async def process_admin_unban_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.pardon_individual_id)
    await callback.message.edit_text("✅ <b>Enter Target Passport Identity to Pardon:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.pardon_individual_id)
async def process_admin_unban_execute(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        return await message.answer("Numeric format exception.")
    await DataEngine.banish_user(int(message.text.strip()), 0)
    await state.clear()
    await message.answer("✅ Target entity pardoned. Network access permissions reinstated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_reward")
async def process_admin_reward_config_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.modify_referral_bounty)
    await callback.message.edit_text("<b>Enter New Passive Reward Value Allocation:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_referral_bounty)
async def process_admin_reward_config_execute(message: Message, state: FSMContext):
    await DataEngine.write_config("reward_per_referral", message.text.strip())
    await state.clear()
    await message.answer("✅ Reward system settings updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_min_wd")
async def process_admin_min_wd_config_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminConsoleWorkflow.modify_minimum_cashout)
    await callback.message.edit_text("<b>Enter New Global Network Minimum Cashout Weight:</b>", reply_markup=generate_fallback_navigation("ui_admin_core"))

@core_router.message(AdminConsoleWorkflow.modify_minimum_cashout)
async def process_admin_min_wd_config_execute(message: Message, state: FSMContext):
    await DataEngine.write_config("min_withdrawal", message.text.strip())
    await state.clear()
    await message.answer("✅ Cashout threshold baseline configuration updated.", reply_markup=generate_admin_dashboard())

@core_router.callback_query(F.data == "adm_cmd_pending_tickets")
async def process_admin_tickets_inventory(callback: CallbackQuery):
    pending_tickets = await DataEngine.pull_pending_tickets()
    if not pending_tickets:
        return await callback.message.edit_text("<b>Zero Queue:</b> No active transactions demand settlement currently.", reply_markup=generate_admin_dashboard())
    await callback.message.edit_text(f"📥 Found <b>{len(pending_tickets)} active queue ticket(s)</b>. Manage metrics inside admin log.", reply_markup=generate_admin_dashboard())


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI BACKEND INFRASTRUCTURE & ANTI-BOT WEbAPP REST GATEWAY
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def application_lifespan(app: FastAPI):
    """Handles pipeline resource setup securely."""
    await DataEngine.init_database()
    asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    yield

# Native Application Setup
api_platform = FastAPI(lifespan=application_lifespan)
dp.include_router(core_router)

# CORS Rules Implementation
api_platform.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api_platform.get("/verify", response_class=HTMLResponse)
async def serve_frontend_module(uid: int = 0, ref: int = 0):
    """Hosts authentication template script direct from server memory stream."""
    try:
        with open("index.html", "r") as storage_file:
            loaded_html_payload = storage_file.read()
        return HTMLResponse(content=loaded_html_payload.replace("__BACKEND_URL__", WEBAPP_URL))
    except Exception as file_read_error:
        raise HTTPException(status_code=500, detail=f"Mini App Template missing from local directory stream: {file_read_error}")

@api_platform.post("/api/verify")
async def execute_secure_device_verification(request: Request):
    """Evaluates telemetry fingerprints, client node IPs, and manages rewards."""
    incoming_payload = await request.json()
    telegram_metadata_node = parse_telegram_webapp_handshake(incoming_payload.get("initData", ""))
    
    if not telegram_metadata_node:
        raise HTTPException(status_code=403, detail="Signature authentication breach.")
        
    client_user_id = int(telegram_metadata_node["id"])
    passed_referrer_id = int(incoming_payload.get("refId") or 0)

    if await DataEngine.check_device_status(client_user_id):
        return JSONResponse({"status": "already_verified"})

    # Execute Hybrid Anti-Clone Security Scan
    is_cloned = await DataEngine.discover_clones(incoming_payload.get("ip", ""), incoming_payload.get("fingerprint", ""), client_user_id)
    is_proxy_active = incoming_payload.get("isVpn") or await execute_network_vpn_lookup(incoming_payload.get("ip", ""))
    
    if is_cloned or is_proxy_active:
        logger.warning(f"Quarantine executed on user node {client_user_id}. Proxy/Clone metrics true.")
        await DataEngine.record_user(client_user_id, telegram_metadata_node.get("username", ""), telegram_metadata_node.get("first_name", ""))
        await DataEngine.banish_user(client_user_id, 1)
        return JSONResponse({"status": "blocked"})

    # Setup Profile Parameters
    await DataEngine.record_user(client_user_id, telegram_metadata_node.get("username", ""), telegram_metadata_node.get("first_name", ""), passed_referrer_id or None)
    await DataEngine.register_verification(client_user_id, incoming_payload.get("ip", ""), incoming_payload.get("fingerprint", ""), "MiniAppSecureModule")

    # Allocate Financial Bounty to Referrer
    if passed_referrer_id and passed_referrer_id != client_user_id:
        bounty_allocation = float(await DataEngine.read_config("reward_per_referral", "10"))
        await DataEngine.modify_balance(passed_referrer_id, bounty_allocation)
        try:
            await bot.send_message(
                chat_id=passed_referrer_id,
                text=f"🎉 <b>Network Bounty Allocated!</b>\nYour direct connection successfully verified identity. <code>+{bounty_allocation} Birr</code> credited."
            )
        except Exception:
            pass

    try:
        await bot.send_message(
            chat_id=client_user_id,
            text="✅ <b>Verification Confirmed Successfully!</b>\nYour node token is signed. Access granted to core dashboard menu.",
            reply_markup=generate_dashboard_matrix(client_user_id)
        )
    except Exception:
        pass

    return JSONResponse({"status": "verified"})

# ─────────────────────────────────────────────────────────────────────────────
# INFRASTRUCTURE START APPLICATION ENGINE ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Spawning operational FastAPI server layers on port 8000...")
    uvicorn.run("bot:api_platform", host="0.0.0.0", port=8000, log_level="info")

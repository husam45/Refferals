"""
bot.py – Telegram Referral Bot (Advanced Reply Proof & Sneaky Optional Channel Edition)
"""
import os
import asyncio
import hashlib
import hmac
import json
import logging
import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

import database as db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config & Environment Variables
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
PAYMENT_LOG_CH  = os.getenv("PAYMENT_LOG_CHANNEL", "")
WEBAPP_URL      = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
PROXYCHECK_KEY  = os.getenv("PROXYCHECK_API_KEY", "")

# 📸 የቴሌብር ማረጋገጫ File ID
TELEBIRR_PROOF_IMAGE = "AgACAgQAAxkBAAOYai38ooud5iofBd3aDGuCiX273t8AAj4PaxsYl3BR78MpfA_cDpkBAAMCAAN4AAM8BA"

if WEBAPP_URL.startswith("tg56") or not WEBAPP_URL.startswith(("http://", "https://")):
    WEBAPP_URL = f"https://{WEBAPP_URL}"

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)

# ─────────────────────────────────────────────────────────────────────────────
# FSM States (Finite State Machine)
# ─────────────────────────────────────────────────────────────────────────────
class WithdrawState(StatesGroup):
    method    = State()
    amount    = State()
    full_name = State()
    phone     = State()
    confirm   = State()

class AdminState(StatesGroup):
    set_reward         = State()
    set_min_withdrawal = State()
    
    # Force Join States
    add_channel_id     = State()
    add_channel_name   = State()
    add_channel_link   = State()
    
    # Optional Channel States (Sneaky Setup)
    add_opt_id         = State()
    add_opt_name       = State()
    add_opt_link       = State()
    
    edit_bal_uid       = State()
    edit_bal_amount    = State()
    
    broadcast_msg      = State()
    search_user_id     = State()
    ban_user_id        = State()
    unban_user_id      = State()

# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions & Core Logic
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def mask_phone(phone: str) -> str:
    if len(phone) < 6:
        return phone
    return phone[:-5] + "***" + phone[-2:]

def verify_telegram_initdata(init_data: str) -> dict | None:
    try:
        import urllib.parse
        pairs, hash_val = {}, ""
        for item in init_data.split("&"):
            k, _, v = item.partition("=")
            if k == "hash":
                hash_val = v
            else:
                pairs[k] = v
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            return None
        return json.loads(urllib.parse.unquote(pairs.get("user", "{}")))
    except Exception as e:
        log.warning("initData verify failed: %s", e)
        return None

async def check_force_join(uid: int) -> list:
    """ፎርስ ጆይን የሆኑትን ብቻ ይፈትሻል (Optional ቻናሎችን ይተዋቸዋል)"""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # በዳታቤዝ ውስጥ 'is_optional' ኮለምን መኖሩን ያረጋግጣል (ካለ 0 የሆኑትን ብቻ ይወስዳል)
        try:
            cur = await conn.execute("SELECT * FROM force_channels WHERE is_optional = 0")
        except Exception:
            # ሰንጠረዡ ገና ካልተሻሻለ ሁሉንም እንደ ግዴታ ይወስዳል
            cur = await conn.execute("SELECT * FROM force_channels")
        channels = await cur.fetchall()
        
    not_joined = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["channel_id"], uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined

async def get_all_display_channels() -> list:
    """ሁሉንም ቻናሎች (ግዴታዎቹንም አማራጮቹንም) ለተጠቃሚው እንዲታዩ በአንድ ላይ ያወጣል"""
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        try:
            cur = await conn.execute("SELECT * FROM force_channels")
            return await cur.fetchall()
        except Exception:
            return []

async def server_vpn_check(ip: str) -> bool:
    if not ip or ip in ("127.0.0.1", "::1", "unknown"):
        return False
    try:
        key_param = f"&key={PROXYCHECK_KEY}" if PROXYCHECK_KEY else ""
        url = f"https://proxycheck.io/v2/{ip}?vpn=1{key_param}"
        async with httpx.AsyncClient(timeout=5) as c:
            data = (await c.get(url)).json()
            r = data.get(ip, {})
            return r.get("proxy") == "yes" or r.get("vpn") == "yes"
    except Exception as e:
        log.warning("proxycheck error: %s", e)
        return False

# ─────────────────────────────────────────────────────────────────────────────
# Keyboards & Navigation Layouts
# ─────────────────────────────────────────────────────────────────────────────
def verify_button_kb(uid: int, ref: int) -> InlineKeyboardMarkup:
    url = f"{WEBAPP_URL}/verify?uid={uid}&ref={ref}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔐 Open Mini App & Verify", web_app=WebAppInfo(url=url))
    ]])

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💰 Balance / ሒሳብ",     callback_data="balance"),
            InlineKeyboardButton(text="👥 Referrals / ጋባዦች",   callback_data="referrals"),
        ],
        [
            InlineKeyboardButton(text="🔗 My Link / ሊንኬ",     callback_data="reflink"),
            InlineKeyboardButton(text="💸 Withdraw / ብር ማውጫ",    callback_data="withdraw"),
        ],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton(text="⚙️ Admin Panel / አድሚን ገጽ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 Set Referral Reward",  callback_data="admin_set_reward"),
            InlineKeyboardButton(text="💵 Set Min Withdrawal",     callback_data="admin_set_min_wd")
        ],
        [
            InlineKeyboardButton(text="✍️ Edit User Balance",       callback_data="admin_edit_balance"),
            InlineKeyboardButton(text="📊 Bot Statistics",          callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton(text="🔴 Add Force Channel",       callback_data="admin_add_ch"),
            InlineKeyboardButton(text="🟢 Add Optional Channel",    callback_data="admin_add_opt_ch")
        ],
        [
            InlineKeyboardButton(text="🗑 Remove Channel",          callback_data="admin_rm_ch"),
            InlineKeyboardButton(text="📋 List Channels",          callback_data="admin_list_ch")
        ],
        [
            InlineKeyboardButton(text="📥 Pending Withdrawals",    callback_data="admin_pending_wd"),
            InlineKeyboardButton(text="📢 Broadcast Message",      callback_data="admin_broadcast")
        ],
        [
            InlineKeyboardButton(text="🔍 Search User Info",       callback_data="admin_search_user"),
        ],
        [
            InlineKeyboardButton(text="🚫 Ban User",               callback_data="admin_ban_user"),
            InlineKeyboardButton(text="✅ Unban User",             callback_data="admin_unban_user")
        ],
        [InlineKeyboardButton(text="🔙 Back to Main Menu",           callback_data="main_menu")],
    ])

def back_kb(target="main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data=target)
    ]])

# ─────────────────────────────────────────────────────────────────────────────
# Public Bot Commands & Core Handlers
# ─────────────────────────────────────────────────────────────────────────────
router = Router()

@router.message(F.photo)
async def get_any_photo_file_id(msg: Message):
    if not is_admin(msg.from_user.id): return
    file_id = msg.photo[-1].file_id
    await msg.answer(f"📸 <b>Telegram File ID:</b>\n<code>{file_id}</code>")

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    fname = msg.from_user.full_name or ""
    args  = msg.text.split()[1] if len(msg.text.split()) > 1 else ""
    ref   = int(args) if args.isdigit() and int(args) != uid else 0

    user = await db.get_user(uid)
    if user and user["is_banned"]:
        return await msg.answer("🚫 <b>You are banned from using this bot.</b>")

    # ፎርስ ጆይን ብቻ ነው የሚፈትሸው (አማራጩን ይዘለዋል)
    not_joined_mandatory = await check_force_join(uid)
    
    if not_joined_mandatory:
        # ነገር ግን ለተጠቃሚው ሲያሳይ ሳይነቃበት ሁሉንም ቻናሎች በአንድ ላይ ቀላቅሎ ያሳያል!
        all_display = await get_all_display_channels()
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>" for c in all_display)
        if ref: await state.update_data(pending_ref=ref)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕ {c['channel_name']}", url=c["invite_link"])] for c in all_display],
            [InlineKeyboardButton(text="✅ Joined — Check Again", callback_data="recheck_join")],
        ])
        return await msg.answer(f"👋 Welcome, <b>{fname}</b>!\n\n⚠️ <b>You must join these channels first to unlock the bot:</b>\n{lines}", reply_markup=kb, disable_web_page_preview=True)

    if await db.is_verified(uid):
        reward = await db.get_setting("reward_per_referral", "10")
        return await msg.answer(f"👋 Welcome back, <b>{fname}</b>!\n\nEarn <b>{reward} Birr</b> for every verified referral.", reply_markup=main_menu_kb(uid))

    rules_text = "👋 Hello!\n\n⚠️ <b>Security Verification Required</b>\n\nTap the button below to open the Mini App and auto-verify."
    await msg.answer(rules_text, reply_markup=verify_button_kb(uid, ref))

@router.callback_query(F.data == "recheck_join")
async def recheck_join(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    not_joined_mandatory = await check_force_join(uid)
    
    if not_joined_mandatory:
        all_display = await get_all_display_channels()
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>" for c in all_display)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕ {c['channel_name']}", url=c["invite_link"])] for c in all_display],
            [InlineKeyboardButton(text="✅ Joined — Check Again", callback_data="recheck_join")],
        ])
        await cb.message.edit_text(f"⚠️ <b>Still not joined all channels:</b>\n{lines}", reply_markup=kb, disable_web_page_preview=True)
    else:
        await cb.message.delete()
        state_data = await state.get_data()
        ref = state_data.get("pending_ref", 0)
        await state.clear()
        if await db.is_verified(uid):
            await cb.message.answer("✅ Verification passed!", reply_markup=main_menu_kb(uid))
        else:
            rules_text = "✅ Channels joined successfully!\n\nPlease open the Mini App below to complete setup."
            await cb.message.answer(rules_text, reply_markup=verify_button_kb(uid, ref))
    await cb.answer()

@router.callback_query(F.data == "main_menu")
async def main_menu_callback(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Please verify first.", show_alert=True)
    reward = await db.get_setting("reward_per_referral", "10")
    await cb.message.edit_text(f"🏠 <b>Main Menu / ዋና ማውጫ</b>\n\nEarn <b>{reward} Birr</b> per verified referral.", reply_markup=main_menu_kb(uid))
    await cb.answer()

@router.callback_query(F.data == "balance")
async def show_balance(cb: CallbackQuery):
    if not await db.is_verified(cb.from_user.id): return await cb.answer("🔒 Unverified.", show_alert=True)
    user = await db.get_user(cb.from_user.id)
    bal  = user["balance"] if user else 0.0
    min_wd = await db.get_setting("min_withdrawal", "50")
    await cb.message.edit_text(f"💰 <b>Your Balance</b>\n\nAvailable : <b>{bal:.2f} Birr</b>\nMin. withdrawal : <b>{min_wd} Birr</b>", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "referrals")
async def show_referrals(cb: CallbackQuery):
    uid = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Unverified.", show_alert=True)
    count  = await db.get_referral_count(uid)
    reward = float(await db.get_setting("reward_per_referral", "10"))
    earned = count * reward
    await cb.message.edit_text(f"👥 <b>Your Referrals</b>\n\nTotal verified : <b>{count}</b>\nReward each    : <b>{reward:.2f} Birr</b>\nTotal earned   : <b>{earned:.2f} Birr</b>", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "reflink")
async def show_reflink(cb: CallbackQuery):
    uid = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Unverified.", show_alert=True)
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    await cb.message.edit_text(f"🔗 <b>Your Referral Link</b>\n\n<code>{link}</code>", reply_markup=back_kb(), disable_web_page_preview=True)
    await cb.answer()

# ─────────────────────────────────────────────────────────────────────────────
# 💸 Withdrawal Flow & Advanced Reply Channel Proof System
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "withdraw")
async def withdraw_start(cb: CallbackQuery, state: FSMContext):
    uid    = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Unverified.", show_alert=True)
    user   = await db.get_user(uid)
    min_wd = float(await db.get_setting("min_withdrawal", "50"))
    bal    = user["balance"] if user else 0.0

    if bal < min_wd:
        return await cb.answer(f"❌ Need {min_wd:.0f} Birr minimum. You have {bal:.2f}.", show_alert=True)
    
    await state.set_state(WithdrawState.method)
    await state.update_data(min_wd=min_wd, balance=bal)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 Telebirr / ቴሌብር", callback_data="wd_method_telebirr")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu")]
    ])
    await cb.message.edit_text("💸 <b>Withdrawal Method</b>\n\nእባክዎ መምረጫ ይጫኑ፦", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "wd_method_telebirr", WithdrawState.method)
async def wd_method_chosen(cb: CallbackQuery, state: FSMContext):
    await state.update_data(method="Telebirr")
    await state.set_state(WithdrawState.amount)
    data = await state.get_data()
    await cb.message.edit_text(f"Balance: <b>{data['balance']:.2f} Birr</b>\nማውጣት የሚፈልጉትን የብር መጠን ያስገቡ፦", reply_markup=back_kb())
    await cb.answer()

@router.message(WithdrawState.amount)
async def wd_amount(msg: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = float(msg.text.strip())
        assert data["min_wd"] <= amount <= data["balance"]
    except Exception:
        return await msg.answer(f"❌ እባክዎ በ <b>{data['min_wd']:.0f}</b> እና <b>{data['balance']:.2f}</b> መካከል ያስገቡ።")
    await state.update_data(amount=amount)
    await state.set_state(WithdrawState.phone)
    await msg.answer(
        "📱 የ Telebirr ስልክ ቁጥርዎን ያስገቡ፦",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📲 Share Number", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

@router.message(WithdrawState.phone, F.contact)
async def wd_phone_contact(msg: Message, state: FSMContext):
    await state.set_state(WithdrawState.full_name)
    await state.update_data(phone=msg.contact.phone_number)
    await msg.answer("📝 የ Telebirr አካውንት ሙሉ ስምዎን ያስገቡ፦", reply_markup=ReplyKeyboardRemove())

@router.message(WithdrawState.phone)
async def wd_phone_text(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if len(phone) < 9: return await msg.answer("❌ እባክዎ ትክክለኛ ቁጥር ያስገቡ።")
    await state.set_state(WithdrawState.full_name)
    await state.update_data(phone=phone)
    await msg.answer("📝 የ Telebirr አካውንት ሙሉ ስምዎን ያስገቡ፦")

@router.message(WithdrawState.full_name)
async def wd_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if len(name) < 3: return await msg.answer("❌ እባክዎ ስም ያስገቡ።")
    await state.update_data(full_name=name)
    
    data = await state.get_data()
    await msg.answer(
        "✅ <b>የማውጫ ማረጋገጫ (Confirmation)</b>\n\n"
        f"┌ መንገድ : <b>{data['method']}</b>\n"
        f"├ መጠን : <b>{data['amount']:.2f} ETB</b>\n"
        f"├ ስም   : <b>{data['full_name']}</b>\n"
        f"└ ስልክ  : <b>{data['phone']}</b>\n\n"
        "ሁሉም መረጃ ትክክል ከሆነ 'አረጋግጥ' የሚለውን ይጫኑ።",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ አረጋግጥ (Confirm)",  callback_data="wd_confirm"),
            InlineKeyboardButton(text="❌ ሰርዝ",   callback_data="main_menu"),
        ]])
    )
    await state.set_state(WithdrawState.confirm)

@router.callback_query(F.data == "wd_confirm", WithdrawState.confirm)
async def wd_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = cb.from_user.id
    user = await db.get_user(uid)
    
    if not user or user["balance"] < data["amount"]:
        await state.clear()
        return await cb.answer("❌ በቂ ባላንስ የሎትም።", show_alert=True)

    wid = await db.create_withdrawal(uid, data["amount"], data["full_name"], data["phone"])
    await db.add_balance(uid, -data["amount"])
    await state.clear()

    # 📢 1. ተጠቃሚው ሲጠይቅ ቻናል ላይ የሚለጠፍ ውብ የ Requested መልዕክት
    channel_msg_id = None
    if PAYMENT_LOG_CH:
        try:
            username_text = f"@{user['username']}" if user.get('username') else "No Username"
            channel_text = (
                f"✅ <b>PAYOUT SUCCESSFULLY REQUESTED</b>\n\n"
                f"👤 <b>User:</b> {user['full_name']} ({username_text})\n"
                f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
                f"💰 <b>Amount:</b> ETB {data['amount']:.2f}\n\n"
                f"📱 <b>Method:</b> Telebirr\n"
                f"📝 <b>Details:</b> {data['full_name']} - {data['phone']}\n\n"
                f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            ch_msg = await bot.send_message(chat_id=PAYMENT_LOG_CH, text=channel_text)
            channel_msg_id = ch_msg.message_id
        except Exception as e:
            log.warning("Channel post error: %s", e)

    # 📩 2. ለአድሚን የሚላክ (የቻናሉን የሜሴጅ ID ይዞ ይሄዳል)
    admin_text = (
        f"📥 <b>የክፍያ ጥያቄ #{wid}</b>\n\n"
        f"User: {user['full_name']}\n"
        f"Amount: <b>{data['amount']:.2f} Birr</b>"
    )
    ch_id_str = str(channel_msg_id) if channel_msg_id else "0"
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_ap_{wid}_{ch_id_str}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wid}"),
    ]])
    
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, admin_text, reply_markup=approve_kb)
        except Exception: pass

    await cb.message.edit_text("📨 <b>የማውጫ ጥያቄዎ በትክክል ተመዝግቧል!</b>\nመረጃው በቻናላችን ላይ ተለጥፏል።")
    await cb.answer("ጥያቄዎ ተልኳል!")

@router.callback_query(F.data.startswith("wd_ap_"))
async def wd_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    
    parts = cb.data.split("_")
    wid = int(parts[2])
    ch_msg_id = int(parts[3])

    wd = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending": return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "approved")

    # 📢 3. አድሚኑ ሲያጸድቅ የ Requested ፖስቱን REPLY አድርጎ PROCESSED ይላል!
    if PAYMENT_LOG_CH and ch_msg_id != 0:
        try:
            success_text = (
                f"✅ <b>PAYOUT SUCCESSFULLY PROCESSED</b>\n\n"
                f"🎉 <b>Congratulations! Your payout has been sent.</b>\n\n"
                f"👤 <b>Receiver:</b> {wd['full_name']}\n"
                f"💰 <b>Amount Paid:</b> ETB {wd['amount']:.2f}\n"
                f"🚀 <b>Status:</b> Success ✅\n\n"
                f"🤖 <b>Bot Link:</b> @{(await bot.get_me()).username}"
            )
            await bot.send_photo(
                chat_id=PAYMENT_LOG_CH, 
                photo=TELEBIRR_PROOF_IMAGE, 
                caption=success_text,
                reply_to_message_id=ch_msg_id
            )
        except Exception as e:
            log.warning("Channel reply error: %s", e)

    try: await bot.send_message(wd["user_id"], f"🎉 <b>ክፍያዎ ተሳክቷል!</b>\n\n{wd['amount']:.2f} Birr ተልኳል።")
    except Exception: pass

    await cb.message.edit_text(cb.message.text + "\n\n✅ <b>APPROVED & REPLIED ON CHANNEL</b>", reply_markup=None)
    await cb.answer("Approved ✅")

@router.callback_query(F.data.startswith("wd_reject_"))
async def wd_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    wid = int(cb.data.split("_")[-1])
    wd  = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending": return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "rejected")
    await db.add_balance(wd["user_id"], wd["amount"])
    try: await bot.send_message(wd["user_id"], f"❌ <b>የማውጫ ጥያቄዎ ውድቅ ተደርጓል።</b>")
    except Exception: pass
    await cb.message.edit_text(cb.message.text + "\n\n❌ <b>REJECTED</b>", reply_markup=None)
    await cb.answer("Rejected ❌")

# ─────────────────────────────────────────────────────────────────────────────
# 🛠 Advanced Admin Panel Engine (Optional Channel Feature Included)
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.clear()
    reward = await db.get_setting("reward_per_referral", "10")
    min_wd = await db.get_setting("min_withdrawal", "50")
    await cb.message.edit_text(f"⚙️ <b>Advanced Admin Panel</b>\n\nReward: <b>{reward} Birr</b>\nMin WD: <b>{min_wd} Birr</b>", reply_markup=admin_panel_kb())
    await cb.answer()

# 🔴 ፎርስ ጆይን (ግዴታ) ቻናል መጨመሪያ
@router.callback_query(F.data == "admin_add_ch")
async def admin_add_ch(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.add_channel_id)
    await cb.message.edit_text("🔴 <b>Add Force Join Channel (Mandatory)</b>\n\nEnter Channel ID (e.g. <code>-100...</code>) :")
    await cb.answer()

@router.message(AdminState.add_channel_id)
async def admin_add_ch_id(msg: Message, state: FSMContext):
    await state.update_data(channel_id=msg.text.strip())
    await state.set_state(AdminState.add_channel_name)
    await msg.answer("Enter Channel Name / የስም ማሳያ፦")

@router.message(AdminState.add_channel_name)
async def admin_add_ch_name(msg: Message, state: FSMContext):
    await state.update_data(channel_name=msg.text.strip())
    await state.set_state(AdminState.add_channel_link)
    await msg.answer("Enter Channel Invite Link / የሊንክ ማሳያ፦")

@router.message(AdminState.add_channel_link)
async def admin_add_ch_link(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    # በዳታቤዝ ውስጥ 'is_optional' 0 (ግዴታ) ሆኖ ይገባል
    async with aiosqlite.connect(db.DB_PATH) as conn:
        try:
            await conn.execute(
                "INSERT INTO force_channels (channel_id, channel_name, invite_link, is_optional) VALUES (?, ?, ?, 0)",
                (data["channel_id"], data["channel_name"], msg.text.strip())
            )
        except Exception:
            # ኮለምኑ ገና ካልተፈጠረ (Fallback)
            await conn.execute(
                "INSERT INTO force_channels (channel_id, channel_name, invite_link) VALUES (?, ?, ?)",
                (data["channel_id"], data["channel_name"], msg.text.strip())
            )
        await conn.commit()
    await msg.answer(f"🔴 Force Join Channel <b>{data['channel_name']}</b> Added Successfully!", reply_markup=admin_panel_kb())

# 🟢 [አዲስ] ኦፕሽናል (ሳይነቃበት የሚቀላቀል) ቻናል መጨመሪያ
@router.callback_query(F.data == "admin_add_opt_ch")
async def admin_add_opt_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.add_opt_id)
    await cb.message.edit_text("🟢 <b>Add Optional Channel (Sneaky Mode)</b>\n\nEnter Channel ID (e.g. <code>-100...</code>) :")
    await cb.answer()

@router.message(AdminState.add_opt_id)
async def admin_add_opt_id(msg: Message, state: FSMContext):
    await state.update_data(opt_id=msg.text.strip())
    await state.set_state(AdminState.add_opt_name)
    await msg.answer("Enter Channel Name / የስም ማሳያ፦")

@router.message(AdminState.add_opt_name)
async def admin_add_opt_name(msg: Message, state: FSMContext):
    await state.update_data(opt_name=msg.text.strip())
    await state.set_state(AdminState.add_opt_link)
    await msg.answer("Enter Channel Invite Link / የሊንክ ማሳያ፦")

@router.message(AdminState.add_opt_link)
async def admin_add_opt_link(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    # በዳታቤዝ ውስጥ 'is_optional' 1 (አማራጭ) ሆኖ ይገባል
    async with aiosqlite.connect(db.DB_PATH) as conn:
        # መጀመሪያ ኮለምኑ መኖሩን ለማረጋገጥ Alter እናደርጋለን (ካለ ችግር የለውም ይዘለዋል)
        try: await conn.execute("ALTER TABLE force_channels ADD COLUMN is_optional INTEGER DEFAULT 0")
        except Exception: pass
        
        await conn.execute(
            "INSERT INTO force_channels (channel_id, channel_name, invite_link, is_optional) VALUES (?, ?, ?, 1)",
            (data["opt_id"], data["opt_name"], msg.text.strip())
        )
        await conn.commit()
    await msg.answer(f"🟢 Optional Channel <b>{data['opt_name']}</b> Added Sneakily!", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_list_ch")
async def admin_list_ch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        try: cur = await conn.execute("SELECT * FROM force_channels")
        except Exception: return await cb.answer("No channels configuration table found.")
        channels = await cur.fetchall()
        
    if not channels: text = "No channels configured."
    else:
        text = "📋 <b>All Configured Channels:</b>\n\n"
        for ch in channels:
            is_opt = ch["is_optional"] if "is_optional" in ch.keys() else 0
            type_lbl = "🟢 Optional" if is_opt == 1 else "🔴 Mandatory (Force)"
            text += f"• <b>{ch['channel_name']}</b> ({type_lbl})\nID: <code>{ch['channel_id']}</code>\n\n"
    await cb.message.edit_text(text, reply_markup=admin_panel_kb())
    await cb.answer()

@router.callback_query(F.data == "admin_rm_ch")
async def admin_rm_ch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM force_channels")
        channels = await cur.fetchall()
    if not channels: return await cb.answer("No channels.", show_alert=True)
    btns = [[InlineKeyboardButton(text=f"🗑 {ch['channel_name']}", callback_data=f"admin_rm_do_{ch['channel_id']}")] for ch in channels]
    btns.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")])
    await cb.message.edit_text("Select channel to <b>remove</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await cb.answer()

@router.callback_query(F.data.startswith("admin_rm_do_"))
async def admin_rm_do(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    ch_id = cb.data.replace("admin_rm_do_", "")
    await db.remove_force_channel(ch_id)
    await cb.message.edit_text("✅ Channel removed.", reply_markup=admin_panel_kb())
    await cb.answer()

# ✍️ የተጠቃሚ ባላንስ ማስተካከያ (በ ID ወይም በ Username)
@router.callback_query(F.data == "admin_edit_balance")
async def admin_edit_balance_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.edit_bal_uid)
    await cb.message.edit_text("✍️ <b>User Balance Editor</b>\n\nየተጠቃሚውን <b>Telegram ID</b> ወይም <b>Username (@...)</b> ያስገቡ፦", reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.message(AdminState.edit_bal_uid)
async def admin_edit_balance_uid(msg: Message, state: FSMContext):
    input_text = msg.text.strip()
    user = None
    target_id_int = None
    
    if input_text.startswith("@") or not input_text.isdigit():
        username_clean = input_text.replace("@", "").strip()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
            user = await cur.fetchone()
            if user: target_id_int = user["user_id"]
    else:
        target_id_int = int(input_text)
        user = await db.get_user(target_id_int)

    if not user: return await msg.answer(f"❌ ተጠቃሚው '{input_text}' አልተገኘም።")
    await state.update_data(target_uid=target_id_int)
    await state.set_state(AdminState.edit_bal_amount)
    await msg.answer(f"👤 <b>{user['full_name']}</b>\n💰 ባላንስ: <b>{user['balance']:.2f} Birr</b>\n\nለመጨመር (ለምሳሌ 50) ለመቀነስ (ለምሳሌ -50) ያስገቡ፦")

@router.message(AdminState.edit_bal_amount)
async def admin_edit_balance_amount(msg: Message, state: FSMContext):
    try: amount = float(msg.text.strip())
    except ValueError: return await msg.answer("❌ ቁጥር ያስገቡ።")
    data = await state.get_data()
    target_uid = data["target_uid"]
    
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT balance, full_name FROM users WHERE user_id = ?", (target_uid,))
        u = await cur.fetchone()
        new_balance = (u["balance"] or 0.0) + amount
        await conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, target_uid))
        await conn.commit()
    await state.clear()
    await msg.answer(f"✅ ተስተካክሏል! አዲስ ባላንስ: <b>{new_balance:.2f} Birr</b>", reply_markup=admin_panel_kb())

# 📊 የቦት ስታቲስቲክስ
@router.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        u_cur = await conn.execute("SELECT COUNT(*) as total, SUM(balance) as total_bal FROM users")
        u_row = await u_cur.fetchone()
        w_cur = await conn.execute("SELECT COUNT(*) as total FROM withdrawals WHERE status='approved'")
        w_row = await w_cur.fetchone()
    text = f"📊 <b>Bot Stats:</b>\n\n• Total Users: <b>{u_row['total'] or 0}</b>\n• Paid out: <b>{w_row['total'] or 0} Payouts</b>"
    await cb.message.edit_text(text, reply_markup=back_kb("admin_panel"))

# 📢 ብሮድካስት መልዕክት መላኪያ
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.broadcast_msg)
    await cb.message.edit_text("📢 ለሁሉም ተጠቃሚዎች የሚላክ መልዕክት ይጻፉ፦", reply_markup=back_kb("admin_panel"))

@router.message(AdminState.broadcast_msg)
async def admin_broadcast_send(msg: Message, state: FSMContext):
    broadcast_text = msg.text
    await state.clear()
    await msg.answer("⏳ ሮጦሽ መላክ ተጀምሯል...")
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT user_id FROM users")
        users = await cur.fetchall()
    for u in users:
        try: await bot.send_message(u["user_id"], broadcast_text); await asyncio.sleep(0.05)
        except Exception: pass
    await msg.answer("✅ ሁሉም ጋ ደርሷል!", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_search_user")
async def admin_search_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.search_user_id)
    await cb.message.edit_text("🔍 የተጠቃሚ ID ያስገቡ፦", reply_markup=back_kb("admin_panel"))

@router.message(AdminState.search_user_id)
async def admin_search_result(msg: Message, state: FSMContext):
    uid_str = msg.text.strip()
    await state.clear()
    if not uid_str.isdigit(): return await msg.answer("❌ ቁጥር መሆን አለበት።")
    user = await db.get_user(int(uid_str))
    if not user: return await msg.answer("❌ አልተገኘም።")
    await msg.answer(f"👤 {user['full_name']}\n💎 ባላንስ: <b>{user['balance']:.2f} Birr</b>", reply_markup=admin_panel_kb())

# 🚫 ባን ሲስተም
@router.callback_query(F.data == "admin_ban_user")
async def admin_ban_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.ban_user_id)
    await cb.message.edit_text("🚫 ማገድ (Ban) የሚፈልጉትን ID ያስገቡ፦", reply_markup=back_kb("admin_panel"))

@router.message(AdminState.ban_user_id)
async def admin_ban_exec(msg: Message, state: FSMContext):
    uid = int(msg.text.strip()) if msg.text.strip().isdigit() else 0
    await state.clear()
    await db.ban_user(uid)
    await msg.answer("🚫 ተጠቃሚው ታግዷል።", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_unban_user")
async def admin_unban_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.unban_user_id)
    await cb.message.edit_text("✅ እገዳ ማንሳት የሚፈልጉትን ID ያስገቡ፦", reply_markup=back_kb("admin_panel"))

@router.message(AdminState.unban_user_id)
async def admin_unban_exec(msg: Message, state: FSMContext):
    uid = int(msg.text.strip()) if msg.text.strip().isdigit() else 0
    await state.clear()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (uid,))
        await conn.commit()
    await msg.answer("✅ እገዳ ተነስቷል።", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_set_reward")
async def admin_set_reward(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.set_reward)
    await cb.message.edit_text("Enter new reward:")

@router.message(AdminState.set_reward)
async def admin_set_reward_val(msg: Message, state: FSMContext):
    await db.set_setting("reward_per_referral", msg.text.strip())
    await state.clear()
    await msg.answer("✅ Updated.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_set_min_wd")
async def admin_set_min_wd(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.set_min_withdrawal)
    await cb.message.edit_text("Enter new min withdrawal:")

@router.message(AdminState.set_min_withdrawal)
async def admin_set_min_wd_val(msg: Message, state: FSMContext):
    await db.set_setting("min_withdrawal", msg.text.strip())
    await state.clear()
    await msg.answer("✅ Updated.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_pending_wd")
async def admin_pending_wd(cb: CallbackQuery):
    pending = await db.get_pending_withdrawals()
    if not pending: return await cb.message.edit_text("No pending withdrawals.", reply_markup=admin_panel_kb())
    await cb.message.edit_text(f"📥 {len(pending)} pending(s)", reply_markup=admin_panel_kb())
    for wd in pending:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve_{wd['id']}"),
            InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wd['id']}"),
        ]])
        await cb.message.answer(f"Withdrawal #{wd['id']}\nAmount: <b>{wd['amount']:.2f}</b>", reply_markup=kb)

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI MiniApp Framework & Verification Middleware
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Initializing database...")
    await db.init_db()
    # በዳታቤዝ ውስጥ 'is_optional' ኮለምን መኖሩን ማረጋገጫ
    async with aiosqlite.connect(db.DB_PATH) as conn:
        try: await conn.execute("ALTER TABLE force_channels ADD COLUMN is_optional INTEGER DEFAULT 0")
        except Exception: pass
        await conn.commit()
    asyncio.create_task(start_bot())
    yield

api = FastAPI(lifespan=lifespan)

@api.get("/verify", response_class=HTMLResponse)
async def serve_miniapp(uid: int = 0, ref: int = 0):
    with open("index.html", "r") as f: html = f.read()
    html = html.replace("__BACKEND_URL__", WEBAPP_URL)
    return HTMLResponse(content=html)

@api.post("/api/verify")
async def api_verify(request: Request):
    body = await request.json()
    init_data = body.get("initData", "")
    ip = body.get("ip", "unknown")
    fingerprint = body.get("fingerprint", "")
    is_vpn = body.get("isVpn", False)
    uid = int(body.get("uid") or 0)
    ref_id = int(body.get("refId") or 0)

    tg_user = verify_telegram_initdata(init_data)
    if tg_user: uid = int(tg_user.get("id", 0))
    if not uid: raise HTTPException(403, "Invalid User")

    uname = tg_user.get("username", "") if tg_user else "User"
    fname = tg_user.get("first_name", "") if tg_user else "User"

    if await db.is_verified(uid): return JSONResponse({"status": "already_verified"})
    if is_vpn or await server_vpn_check(ip):
        await db.create_user(uid, uname, fname, None); await db.ban_user(uid)
        return JSONResponse({"status": "blocked", "reason": "vpn"})

    duplicate = await db.find_duplicate(ip, fingerprint, uid)
    if duplicate:
        await db.create_user(uid, uname, fname, None); await db.ban_user(uid)
        return JSONResponse({"status": "blocked", "reason": "multiaccount"})

    await db.create_user(uid, uname, fname, ref_id or None)
    await db.save_verification(uid, ip, "MiniApp", fingerprint)

    if ref_id and ref_id != uid:
        referrer = await db.get_user(ref_id)
        if referrer and await db.is_verified(ref_id):
            reward = float(await db.get_setting("reward_per_referral", "10"))
            await db.add_balance(ref_id, reward)
            try: await bot.send_message(ref_id, f"🎉 <b>New Referral!</b>\n<b>+{reward:.2f} Birr</b> credited.")
            except Exception: pass

    try: await bot.send_message(uid, "✅ <b>Verification Completed Successfully!</b>", reply_markup=main_menu_kb(uid))
    except Exception: pass
    return JSONResponse({"status": "verified"})

dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

async def start_bot():
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    uvicorn.run("bot:api", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")

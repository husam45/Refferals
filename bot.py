"""
bot.py – Telegram Referral Bot (Fully Featured & Comprehensive Edition)
Flow: /start → Force Join Check → Mini App Verification → Reward & Unlock
Features: Telebirr Integration, Advanced Admin Panel, Auto-Fix User Balance Editor (ID & Username), Broadcast, Ban System, Stats
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

# 🖼 የቴሌብር ፕሩፍ ፎቶ URL
TELEBIRR_PROOF_IMAGE = os.getenv("https://i.postimg.cc/C5Q9k6Bz/IMG-20260614-035748-094.jpg", "https://i.imgur.com/8bX9K4m.jpg")

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
    add_channel_id     = State()
    add_channel_name   = State()
    add_channel_link   = State()
    
    # የባላንስ ማስተካከያ ስቴቶች
    edit_bal_uid       = State()
    edit_bal_amount    = State()
    
    # የአድሚን ስቴቶች
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
    channels = await db.get_force_channels()
    not_joined = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["channel_id"], uid)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, ChatMemberStatus.RESTRICTED):
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined

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
            InlineKeyboardButton(text="➕ Add Force Channel",       callback_data="admin_add_ch"),
            InlineKeyboardButton(text="🗑 Remove Force Channel",    callback_data="admin_rm_ch")
        ],
        [
            InlineKeyboardButton(text="📋 List Channels",          callback_data="admin_list_ch"),
            InlineKeyboardButton(text="📥 Pending Withdrawals",    callback_data="admin_pending_wd")
        ],
        [
            InlineKeyboardButton(text="📢 Broadcast Message",      callback_data="admin_broadcast"),
            InlineKeyboardButton(text="🔍 Search User Info",       callback_data="admin_search_user")
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
# Public Bot Commands & Interactive Endpoints
# ─────────────────────────────────────────────────────────────────────────────
router = Router()

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    fname = msg.from_user.full_name or ""
    args  = msg.text.split()[1] if len(msg.text.split()) > 1 else ""
    ref   = int(args) if args.isdigit() and int(args) != uid else 0

    user = await db.get_user(uid)
    if user and user["is_banned"]:
        return await msg.answer("🚫 <b>You are banned from using this bot.</b>\nሕጋዊ ያልሆነ ተግባር በመፈጸምዎ የታገዱ ተጠቃሚ ነዎት።")

    not_joined = await check_force_join(uid)
    if not_joined:
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>" for c in not_joined)
        if ref: await state.update_data(pending_ref=ref)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕ {c['channel_name']}", url=c["invite_link"])] for c in not_joined],
            [InlineKeyboardButton(text="✅ Joined — Check Again", callback_data="recheck_join")],
        ])
        return await msg.answer(f"👋 Welcome, <b>{fname}</b>!\n\n⚠️ <b>You must join these channels first to unlock the bot:</b>\n{lines}", reply_markup=kb, disable_web_page_preview=True)

    if await db.is_verified(uid):
        reward = await db.get_setting("reward_per_referral", "10")
        return await msg.answer(f"👋 Welcome back, <b>{fname}</b>!\n\nEarn <b>{reward} Birr</b> for every verified referral.", reply_markup=main_menu_kb(uid))

    rules_text = (
        f"👋 Hello <b>{fname}</b>!\n\n⚠️ <b>Security Verification Required</b>\n"
        f"To prevent multi-accounts, you must complete a fast verification.\n\n"
        f"🚫 <b>Strict Rules:</b>\n• VPN / Proxy is strictly prohibited!\n• Only 1 account per device!\n\n"
        f"Tap the button below to open the Mini App and auto-verify."
    )
    await msg.answer(rules_text, reply_markup=verify_button_kb(uid, ref))

@router.callback_query(F.data == "recheck_join")
async def recheck_join(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    not_joined = await check_force_join(uid)
    if not_joined:
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>" for c in not_joined)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕ {c['channel_name']}", url=c["invite_link"])] for c in not_joined],
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
            rules_text = "✅ Channels joined successfully!\n\n⚠️ <b>Final Step: Security Verification</b>\n\nPlease open the Mini App below to complete setup."
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
    await cb.message.edit_text(f"💰 <b>Your Balance / የሒሳብ መጠን</b>\n\nAvailable : <b>{bal:.2f} Birr</b>\nMin. withdrawal : <b>{min_wd} Birr</b>", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "referrals")
async def show_referrals(cb: CallbackQuery):
    uid = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Unverified.", show_alert=True)
    count  = await db.get_referral_count(uid)
    reward = float(await db.get_setting("reward_per_referral", "10"))
    earned = count * reward
    await cb.message.edit_text(f"👥 <b>Your Referrals / የጋበዟቸው ሰዎች</b>\n\nTotal verified : <b>{count}</b>\nReward each    : <b>{reward:.2f} Birr</b>\nTotal earned   : <b>{earned:.2f} Birr</b>", reply_markup=back_kb())
    await cb.answer()

@router.callback_query(F.data == "reflink")
async def show_reflink(cb: CallbackQuery):
    uid = cb.from_user.id
    if not await db.is_verified(uid): return await cb.answer("🔒 Unverified.", show_alert=True)
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    await cb.message.edit_text(f"🔗 <b>Your Referral Link / የእርስዎ መጋበዣ ሊንክ</b>\n\n<code>{link}</code>\n\nShare it — you earn Birr every time someone joins and passes verification.", reply_markup=back_kb(), disable_web_page_preview=True)
    await cb.answer()

# ─────────────────────────────────────────────────────────────────────────────
# 💸 Withdrawal Flow & Telebirr Setup
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
        [InlineKeyboardButton(text="❌ Cancel / ሰርዝ", callback_data="main_menu")]
    ])
    await cb.message.edit_text("💸 <b>Withdrawal Method / የክፍያ አማራጭ ይምረጡ</b>\n\nእባክዎ ገንዘብ ማውጣት የሚፈልጉበትን መንገድ ይምረጡ:", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "wd_method_telebirr", WithdrawState.method)
async def wd_method_chosen(cb: CallbackQuery, state: FSMContext):
    await state.update_data(method="Telebirr")
    await state.set_state(WithdrawState.amount)
    data = await state.get_data()
    await cb.message.edit_text(
        f"💸 <b>Telebirr Withdrawal / የቴሌብር ማውጫ</b>\n\n"
        f"Balance: <b>{data['balance']:.2f} Birr</b>\n"
        f"Minimum: <b>{data['min_wd']:.0f} Birr</b>\n\n"
        "ማውጣት የሚፈልጉትን የብር መጠን ያስገቡ:", reply_markup=back_kb()
    )
    await cb.answer()

@router.message(WithdrawState.amount)
async def wd_amount(msg: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = float(msg.text.strip())
        assert data["min_wd"] <= amount <= data["balance"]
    except Exception:
        return await msg.answer(f"❌ እባክዎ በ <b>{data['min_wd']:.0f}</b> እና <b>{data['balance']:.2f}</b> መካከል ያለ ትክክለኛ የብር መጠን ያስገቡ።")
    await state.update_data(amount=amount)
    await state.set_state(WithdrawState.phone)
    await msg.answer(
        "📱 የ <b>Telebirr ስልክ ቁጥርዎን</b> ያስገቡ (ምሳሌ፦ 0912345678)፦",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📲 ስልኬን አጋራ (Share Number)", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

@router.message(WithdrawState.phone, F.contact)
async def wd_phone_contact(msg: Message, state: FSMContext):
    phone = msg.contact.phone_number
    await state.set_state(WithdrawState.full_name)
    await state.update_data(phone=phone)
    await msg.answer("📝 የ <b>Telebirr አካውንት ስምዎን</b> (ሙሉ ስም) ያስገቡ፦", reply_markup=ReplyKeyboardRemove())

@router.message(WithdrawState.phone)
async def wd_phone_text(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if len(phone) < 9:
        return await msg.answer("❌ እባክዎ ትክክለኛ የስልክ ቁጥር ያስገቡ።")
    await state.set_state(WithdrawState.full_name)
    await state.update_data(phone=phone)
    await msg.answer("📝 የ <b>Telebirr አካውንት ስምዎን</b> (ሙሉ ስም) ያስገቡ፦")

@router.message(WithdrawState.full_name)
async def wd_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if len(name) < 3: return await msg.answer("❌ እባክዎ ሙሉ ስምዎን በትክክል ያስገቡ።")
    await state.update_data(full_name=name)
    
    data = await state.get_data()
    await msg.answer(
        "✅ <b>የማውጫ ማረጋገጫ (Confirmation)</b>\n\n"
        f"┌ መንገድ : <b>{data['method']}</b>\n"
        f"├ መጠን : <b>{data['amount']:.2f} Birr</b>\n"
        f"├ ስም   : <b>{data['full_name']}</b>\n"
        f"└ ስልክ  : <b>{data['phone']}</b>\n\n"
        "ሁሉም መረጃ ትክክል ከሆነ 'አረጋግጥ' የሚለውን ይጫኑ።",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ አረጋግጥ (Confirm)",  callback_data="wd_confirm"),
            InlineKeyboardButton(text="❌ ሰርዝ (Cancel)",   callback_data="main_menu"),
        ]])
    )
    await state.set_state(WithdrawState.confirm)

@router.callback_query(F.data == "wd_confirm", WithdrawState.confirm)
async def wd_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid  = cb.from_user.id
    user = await db.get_user(uid)
    if not user or user["balance"] < data["amount"]:
        await state.clear()
        return await cb.answer("❌ በቂ ባላንስ የሎትም።", show_alert=True)

    wid = await db.create_withdrawal(uid, data["amount"], data["full_name"], data["phone"])
    await db.add_balance(uid, -data["amount"])
    await state.clear()

    admin_text = (
        f"📥 <b>Withdrawal Request #{wid}</b>\n\n"
        f"User : <a href='tg://user?id={uid}'>{user['full_name']}</a> [<code>{uid}</code>]\n"
        f"Method: <b>{data['method']}</b>\n"
        f"Amount: <b>{data['amount']:.2f} Birr</b>\n"
        f"Name  : {data['full_name']}\n"
        f"Phone : {data['phone']}"
    )
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve_{wid}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wid}"),
    ]])
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, admin_text, reply_markup=approve_kb)
        except Exception: pass

    success_msg = (
        "📨 <b>የማውጫ ጥያቄዎ በትክክል ተልኳል!</b>\n\n"
        f"💰 የገንዘብ መጠን: <b>{data['amount']:.2f} Birr</b>\n"
        f"📲 የክፍያ መንገድ: <b>{data['method']}</b>\n\n"
        "⚠️ ጥያቄዎ በአሁኑ ሰዓት በግምገማ ላይ ነው። <b>ገንዘቡ ከ 2 እስከ 48 ሰአት ባለው ጊዜ ውስጥ</b> ወደ ቴሌብር አካውንትዎ የሚላክ ይሆናል። ስኬታማ ሲሆን መልእክት እንልክልዎታለን!"
    )
    await cb.message.edit_text(success_msg, reply_markup=back_kb())
    await cb.answer("ጥያቄዎ ተመዝግቧል!")

# ─────────────────────────────────────────────────────────────────────────────
# Withdrawal Approval with Image Proof
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("wd_approve_"))
async def wd_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔ Unauthorized.", show_alert=True)
    wid = int(cb.data.split("_")[-1])
    wd  = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending": return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "approved")

    if PAYMENT_LOG_CH:
        try:
            caption_text = (
                f"✅ <b>የክፍያ ማረጋገጫ / Payment Proof #{wid}</b>\n\n"
                f"👤 ተከፋይ : {wd['full_name']}\n"
                f"🆔 User ID : <code>{wd['user_id']}</code>\n"
                f"💰 መጠን : <b>{wd['amount']:.2f} Birr</b>\n"
                f"📲 መንገድ : <b>Telebirr</b>\n"
                f"📱 ስልክ : {mask_phone(wd['phone'])}\n"
                f"📅 ቀን : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"🎉 እንኳን ደስ አሎት! በቦታችን ሰዎችን እየጋበዙ የእውነተኛ ክፍያ ባለቤት ይሁኑ።"
            )
            await bot.send_photo(chat_id=PAYMENT_LOG_CH, photo=TELEBIRR_PROOF_IMAGE, caption=caption_text)
        except Exception as e:
            log.warning("Log channel photo error: %s", e)

    try:
        await bot.send_message(wd["user_id"], f"🎉 <b>የማውጫ ጥያቄዎ ጸድቋል!</b>\n\n{wd['amount']:.2f} Birr ወደ ቴሌብር አካውንትዎ ተልኳል። እባክዎ አካውንትዎን ይፈትሹ።")
    except Exception: pass

    await cb.message.edit_text(cb.message.text + "\n\n✅ <b>APPROVED & POSTED WITH IMAGE PROOF</b>", reply_markup=None)
    await cb.answer("Approved ✅")

@router.callback_query(F.data.startswith("wd_reject_"))
async def wd_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔ Unauthorized.", show_alert=True)
    wid = int(cb.data.split("_")[-1])
    wd  = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending": return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "rejected")
    await db.add_balance(wd["user_id"], wd["amount"])
    try:
        await bot.send_message(wd["user_id"], f"❌ <b>የማውጫ ጥያቄዎ ውድቅ ተደርጓል</b>\n\n{wd['amount']:.2f} Birr ወደ ባላንስዎ ተመላሽ ተደርጓል።")
    except Exception: pass
    await cb.message.edit_text(cb.message.text + "\n\n❌ <b>REJECTED</b>", reply_markup=None)
    await cb.answer("Rejected ❌")

# ─────────────────────────────────────────────────────────────────────────────
# 🛠 Advanced Admin Panel Engine (ID & Username Support)
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔ Unauthorized.", show_alert=True)
    await state.clear()
    reward = await db.get_setting("reward_per_referral", "10")
    min_wd = await db.get_setting("min_withdrawal", "50")
    await cb.message.edit_text(f"⚙️ <b>Advanced Admin Panel</b>\n\nReward / referral : <b>{reward} Birr</b>\nMin withdrawal : <b>{min_wd} Birr</b>", reply_markup=admin_panel_kb())
    await cb.answer()

@router.callback_query(F.data == "admin_edit_balance")
async def admin_edit_balance_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.edit_bal_uid)
    await cb.message.edit_text(
        "✍️ <b>User Balance Editor</b>\n\n"
        "እባክዎ ባላንስ ማስተካከል የሚፈልጉትን ተጠቃሚ <b>Telegram ID</b> ወይም <b>Username (@...)</b> ያስገቡ፦", 
        reply_markup=back_kb("admin_panel")
    )
    await cb.answer()

@router.message(AdminState.edit_bal_uid)
async def admin_edit_balance_uid(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    input_text = msg.text.strip()
    
    user = None
    target_id_int = None
    
    # በUsername መፈለግ (በ @ ከጀመረ ወይም ፊደል ካለበት)
    if input_text.startswith("@") or not input_text.isdigit():
        username_clean = input_text.replace("@", "").strip()
        async with aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
            user = await cur.fetchone()
            if user:
                target_id_int = user["user_id"]
    else:
        # በID መፈለግ
        target_id_int = int(input_text)
        user = await db.get_user(target_id_int)

    # ተጠቃሚው ካልተገኘ
    if not user:
        return await msg.answer(
            f"❌ ተጠቃሚው '<b>{input_text}</b>' በዳታቤዝ ውስጥ አልተገኘም።\n"
            f"እባክዎ ተጠቃሚው መጀመሪያ ቦቱን መጀመሩን ያረጋግጡ ወይም ትክክለኛ ID/Username ያስገቡ።"
        )
        
    await state.update_data(target_uid=target_id_int)
    await state.set_state(AdminState.edit_bal_amount)
    
    await msg.answer(
        f"👤 ተጠቃሚ፦ <b>{user['full_name']}</b>\n"
        f"username፦ @{user['username'] or 'የለውም'}\n"
        f"🆔 ID፦ <code>{user['user_id']}</code>\n"
        f"💰 የአሁኑ ባላንስ፦ <b>{user['balance']:.2f} Birr</b>\n\n"
        f"ለመጨめる ፖዘቲቭ ቁጥር (ምሳሌ 100)፦\nለመቀነስ የኔጋቲቭ ቁጥር (ምሳሌ -50) ያስገቡ፦"
    )

@router.message(AdminState.edit_bal_amount)
async def admin_edit_balance_amount(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        amount = float(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ እባክዎ ትክክለኛ ቁጥር ያስገቡ።")
        
    data = await state.get_data()
    target_uid = data["target_uid"]
    
    # ⚡ ባላንሱን በዳታቤዝ ላይ በቀጥታ ማዘመን (የCache ችግርን ይፈታል)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT balance, full_name FROM users WHERE user_id = ?", (target_uid,))
        user_row = await cur.fetchone()
        
        if not user_row:
            await state.clear()
            return await msg.answer("❌ ስህተት አጋጥሟል፤ ተጠቃሚው ሊገኝ አልቻለም።")
            
        old_balance = user_row["balance"] or 0.0
        new_balance = old_balance + amount
        
        await conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, target_uid))
        await conn.commit()
        
    await state.clear()
    
    # ለአድሚኑ ማረጋገጫ መስጠት
    await msg.answer(
        f"✅ ባላንስ በተሳካ ሁኔታ ተስተካክሏል!\n\n"
        f"👤 ተጠቃሚ፦ <b>{user_row['full_name']}</b>\n"
        f"💰 የነበረው ባላንስ፦ <b>{old_balance:.2f} Birr</b>\n"
        f"➕ የተደረገው ለውጥ፦ <b>{amount:+.2f} Birr</b>\n"
        f"💎 አዲስ ባላንስ፦ <b>{new_balance:.2f} Birr</b>", 
        reply_markup=admin_panel_kb()
    )
    
    # 🔥 ለተጠቃሚው በቦቱ በኩል ፈጣን ማሳወቂያ መላክ
    try:
        if amount > 0:
            notification_text = (
                f"🎉 <b>አዲስ ባላንስ ተጨምሮልዎታል!</b>\n\n"
                f"💰 የተጨመረው መጠን፦ <b>+{amount:.2f} Birr</b>\n"
                f"💎 የአሁኑ ጠቅላላ ባላንስዎ፦ <b>{new_balance:.2f} Birr</b>"
            )
        else:
            notification_text = (
                f"📉 <b>ከባላንስዎ ላይ ተቀንሷል!</b>\n\n"
                f"💰 የተቀነሰው መጠን፦ <b>{amount:.2f} Birr</b>\n"
                f"💎 የአሁኑ ጠቅላላ ባላንስዎ፦ <b>{new_balance:.2f} Birr</b>"
            )
        
        await bot.send_message(chat_id=target_uid, text=notification_text)
    except Exception as e:
        log.warning(f"ለተጠቃሚው {target_uid} ማሳወቂያ መላክ አልተቻለም: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# 📊 Statistics, Broadcast & Search Engine
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        u_cur = await conn.execute("SELECT COUNT(*) as total, SUM(balance) as total_bal FROM users")
        u_row = await u_cur.fetchone()
        v_cur = await conn.execute("SELECT COUNT(*) as total FROM verifications")
        v_row = await v_cur.fetchone()
        w_cur = await conn.execute("SELECT COUNT(*) as total, SUM(amount) as total_amt FROM withdrawals WHERE status='approved'")
        w_row = await w_cur.fetchone()
        
    text = (
        "📊 <b>Bot Realtime Statistics</b>\n\n"
        f"• Total Registered Users: <b>{u_row['total'] or 0}</b>\n"
        f"• Total Verified Users: <b>{v_row['total'] or 0}</b>\n"
        f"• Total Combined Balance: <b>{(u_row['total_bal'] or 0.0):.2f} Birr</b>\n"
        f"• Total Paid out (Approved): <b>{(w_row['total_amt'] or 0.0):.2f} Birr</b>\n"
        f"• Total Success Withdrawals: <b>{w_row['total'] or 0}</b>"
    )
    await cb.message.edit_text(text, reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.broadcast_msg)
    await cb.message.edit_text("📢 <b>Broadcast Engine</b>\n\nለሁሉም የቦቱ ተጠቃሚዎች በአንድ ጊዜ ለመላክ የሚፈልጉትን መልዕክት ይጻፉ፦", reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.message(AdminState.broadcast_msg)
async def admin_broadcast_send(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    broadcast_text = msg.text
    await state.clear()
    await msg.answer("⏳ Broadcast ተጀምሯል...")
    
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT user_id FROM users")
        users = await cur.fetchall()
        
    success, failed = 0, 0
    for u in users:
        try:
            await bot.send_message(u["user_id"], broadcast_text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
            
    await msg.answer(f"📢 <b>Broadcast ሪፖርት</b>\n\n✅ የተላከላቸው: <b>{success}</b>\n❌ ያልተላከላቸው: <b>{failed}</b>", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_search_user")
async def admin_search_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.search_user_id)
    await cb.message.edit_text("🔍 <b>Search User</b>\n\nለመፈለግ የሚፈልጉትን የተጠቃሚ <b>Telegram ID</b> ያስገቡ፦", reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.message(AdminState.search_user_id)
async def admin_search_result(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    uid_str = msg.text.strip()
    await state.clear()
    if not uid_str.isdigit(): return await msg.answer("❌ እባክዎ ትክክለኛ የቁጥር ID ያስገቡ።")
    
    user = await db.get_user(int(uid_str))
    if not user: return await msg.answer("❌ ተጠቃሚው በዳታቤዝ ውስጥ አልተገኘም።")
    
    verified = "Yes ✅" if await db.is_verified(user["user_id"]) else "No ❌"
    status = "Banned 🚫" if user["is_banned"] else "Active ✅"
    
    text = (
        f"👤 <b>User Advanced Profile</b>\n\n"
        f"• User ID: <code>{user['user_id']}</code>\n"
        f"• Name: <b>{user['full_name']}</b>\n"
        f"• Username: @{user['username'] or 'None'}\n"
        f"• Current Balance: <b>{user['balance']:.2f} Birr</b>\n"
        f"• Verified Status: <b>{verified}</b>\n"
        f"• Account Status: <b>{status}</b>\n"
        f"• Joined At: <code>{user['joined_at']}</code>\n"
        f"• Referred By ID: <code>{user['referred_by'] or 'Direct'}</code>"
    )
    await msg.answer(text, reply_markup=admin_panel_kb())

# ─────────────────────────────────────────────────────────────────────────────
# Ban / Unban Engine
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_ban_user")
async def admin_ban_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.ban_user_id)
    await cb.message.edit_text("🚫 <b>Ban User</b>\n\nማገድ (Ban) የሚፈልጉትን የተጠቃሚ <b>Telegram ID</b> ያስገቡ፦", reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.message(AdminState.ban_user_id)
async def admin_ban_exec(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    uid_str = msg.text.strip()
    await state.clear()
    if not uid_str.isdigit(): return await msg.answer("❌ ID ቁጥር መሆን አለበት።")
    
    uid = int(uid_str)
    user = await db.get_user(uid)
    if not user: return await msg.answer("❌ ተጠቃሚው አልተገኘም።")
    
    await db.ban_user(uid)
    await msg.answer(f"🚫 ተጠቃሚው <b>{user['full_name']}</b> [<code>{uid}</code>] ታግዷል።", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_unban_user")
async def admin_unban_start(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.unban_user_id)
    await cb.message.edit_text("✅ <b>Unban User</b>\n\nእገዳ ማንሳት የሚፈልጉትን የተጠቃሚ <b>Telegram ID</b> ያስገቡ፦", reply_markup=back_kb("admin_panel"))
    await cb.answer()

@router.message(AdminState.unban_user_id)
async def admin_unban_exec(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    uid_str = msg.text.strip()
    await state.clear()
    if not uid_str.isdigit(): return await msg.answer("❌ ID ቁጥር መሆን አለበት።")
    
    uid = int(uid_str)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (uid,))
        await conn.commit()
        
    await msg.answer(f"✅ የተጠቃሚ ID <code>{uid}</code> እገዳ ተነስቷል።", reply_markup=admin_panel_kb())

# ─────────────────────────────────────────────────────────────────────────────
# System Settings & Channel Operations
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_set_reward")
async def admin_set_reward(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.set_reward)
    await cb.message.edit_text("Enter new <b>reward per referral</b> (Birr):")
    await cb.answer()

@router.message(AdminState.set_reward)
async def admin_set_reward_val(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        val = float(msg.text.strip()); assert val > 0
    except Exception: return await msg.answer("❌ Enter a positive number.")
    await db.set_setting("reward_per_referral", str(val))
    await state.clear()
    await msg.answer(f"✅ Reward set to <b>{val} Birr</b>.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_set_min_wd")
async def admin_set_min_wd(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.set_min_withdrawal)
    await cb.message.edit_text("Enter new <b>minimum withdrawal</b> (Birr):")
    await cb.answer()

@router.message(AdminState.set_min_withdrawal)
async def admin_set_min_wd_val(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        val = float(msg.text.strip()); assert val > 0
    except Exception: return await msg.answer("❌ Enter a positive number.")
    await db.set_setting("min_withdrawal", str(val))
    await state.clear()
    await msg.answer(f"✅ Min withdrawal set to <b>{val} Birr</b>.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_add_ch")
async def admin_add_ch(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.add_channel_id)
    await cb.message.edit_text("Step 1/3 — Enter the <b>Channel ID</b>\ne.g. <code>-100123456789</code>")
    await cb.answer()

@router.message(AdminState.add_channel_id)
async def admin_add_ch_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(channel_id=msg.text.strip())
    await state.set_state(AdminState.add_channel_name)
    await msg.answer("Step 2/3 — Enter the <b>Channel Name</b>:")

@router.message(AdminState.add_channel_name)
async def admin_add_ch_name(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.update_data(channel_name=msg.text.strip())
    await state.set_state(AdminState.add_channel_link)
    await msg.answer("Step 3/3 — Enter the <b>Invite Link</b>:")

@router.message(AdminState.add_channel_link)
async def admin_add_ch_link(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    data = await state.get_data()
    await db.add_force_channel(data["channel_id"], data["channel_name"], msg.text.strip())
    await state.clear()
    await msg.answer(f"✅ <b>{data['channel_name']}</b> added.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_list_ch")
async def admin_list_ch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    channels = await db.get_force_channels()
    if not channels: text = "No force-join channels configured."
    else:
        text = "<b>Force-Join Channels:</b>\n\n"
        for ch in channels: text += f"• <b>{ch['channel_name']}</b>\n ID: <code>{ch['channel_id']}</code>\n Link: {ch['invite_link']}\n\n"
    await cb.message.edit_text(text, reply_markup=admin_panel_kb(), disable_web_page_preview=True)
    await cb.answer()

@router.callback_query(F.data == "admin_rm_ch")
async def admin_rm_ch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    channels = await db.get_force_channels()
    if not channels: return await cb.answer("No channels to remove.", show_alert=True)
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

@router.callback_query(F.data == "admin_pending_wd")
async def admin_pending_wd(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    pending = await db.get_pending_withdrawals()
    if not pending:
        await cb.message.edit_text("No pending withdrawals.", reply_markup=admin_panel_kb())
        return await cb.answer()
    await cb.message.edit_text(f"📥 <b>{len(pending)} pending withdrawal(s)</b>", reply_markup=admin_panel_kb())
    for wd in pending:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve_{wd['id']}"),
            InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wd['id']}"),
        ]])
        await cb.message.answer(f"📥 <b>Withdrawal #{wd['id']}</b>\n\nUser: <code>{wd['user_id']}</code>\nAmount: <b>{wd['amount']:.2f} Birr</b>\nName: {wd['full_name']}\nPhone: {wd['phone']}\nDate: {wd['created_at']}", reply_markup=kb)
    await cb.answer()

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI MiniApp Framework & Verification Middleware
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
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
    body       = await request.json()
    init_data  = body.get("initData", "")
    ip         = body.get("ip", "unknown")
    ua         = body.get("userAgent", "")
    fingerprint= body.get("fingerprint", "")
    is_vpn     = body.get("isVpn", False)
    
    uid = int(body.get("uid") or body.get("userId") or body.get("user_id") or 0)
    ref_id = int(body.get("refId") or body.get("ref_id") or body.get("referrer") or 0)

    tg_user = verify_telegram_initdata(init_data)
    if not tg_user and not uid: raise HTTPException(403, "Invalid Telegram data")
    if tg_user and not uid: uid = int(tg_user.get("id", 0))

    uname = tg_user.get("username", "") if tg_user else "User"
    fname = tg_user.get("first_name", "") if tg_user else "User"

    if not uid: raise HTTPException(403, "No user ID provided")
    if await db.is_verified(uid): return JSONResponse({"status": "already_verified"})

    if is_vpn or await server_vpn_check(ip):
        await db.create_user(uid, uname, fname, None) 
        await db.ban_user(uid)
        try: await bot.send_message(uid, "🚫 <b>Verification Failed</b>\n\nVPN/Proxy detected! Your account has been banned.")
        except Exception: pass
        return JSONResponse({"status": "blocked", "reason": "vpn"})

    duplicate = await db.find_duplicate(ip, fingerprint, uid)
    if duplicate:
        await db.create_user(uid, uname, fname, None) 
        await db.ban_user(uid)
        try: await bot.send_message(uid, "🚫 <b>Multi-Account Detected</b>\n\nYou are banned.")
        except Exception: pass
        return JSONResponse({"status": "blocked", "reason": "multiaccount"})

    await db.create_user(uid, uname, fname, ref_id or None)
    await db.save_verification(uid, ip, ua, fingerprint)

    if ref_id and ref_id != uid:
        referrer = await db.get_user(ref_id)
        if referrer and await db.is_verified(ref_id):
            reward = float(await db.get_setting("reward_per_referral", "10"))
            await db.add_balance(ref_id, reward)
            try:
                new_bal = referrer["balance"] + reward
                await bot.send_message(ref_id, f"🎉 <b>New Referral!</b>\n\n<b>+{reward:.2f} Birr</b> credited.\nBalance: <b>{new_bal:.2f} Birr</b>")
            except Exception: pass

    try: await bot.send_message(uid, "✅ <b>Verification Completed Successfully!</b>\n\nYour account is now active.", reply_markup=main_menu_kb(uid))
    except Exception: pass

    return JSONResponse({"status": "verified"})

dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

async def start_bot():
    log.info("Bot polling started.")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:api", host="0.0.0.0", port=port, log_level="info")

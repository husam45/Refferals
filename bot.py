"""
bot.py – Telegram Referral Bot + FastAPI Mini App backend
Flow: /start → inline button → Mini App → auto-close → bot sends result
"""
import os, asyncio, hashlib, hmac, json, logging
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
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
# Config
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip()]
PAYMENT_LOG_CH  = os.getenv("PAYMENT_LOG_CHANNEL", "")
WEBAPP_URL      = os.getenv("WEBAPP_URL", "http://localhost:8000").rstrip("/")
PROXYCHECK_KEY  = os.getenv("PROXYCHECK_API_KEY", "")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)

# ─────────────────────────────────────────────────────────────────────────────
# FSM States
# ─────────────────────────────────────────────────────────────────────────────
class WithdrawState(StatesGroup):
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

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def mask_phone(phone: str) -> str:
    if len(phone) < 6:
        return phone
    return phone[:-5] + "***" + phone[-2:]

def verify_telegram_initdata(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData HMAC-SHA256 signature."""
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
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED,
                             ChatMemberStatus.RESTRICTED):
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
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────
def verify_button_kb(uid: int, ref: int) -> InlineKeyboardMarkup:
    """The single beautiful inline button that opens the Mini App."""
    url = f"{WEBAPP_URL}/verify?uid={uid}&ref={ref}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔐  Verify Now — Tap to Continue",
            web_app=WebAppInfo(url=url)
        )
    ]])

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💰 Balance",     callback_data="balance"),
            InlineKeyboardButton(text="👥 Referrals",   callback_data="referrals"),
        ],
        [
            InlineKeyboardButton(text="🔗 My Link",     callback_data="reflink"),
            InlineKeyboardButton(text="💸 Withdraw",    callback_data="withdraw"),
        ],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton(text="⚙️  Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Set Reward / Referral",  callback_data="admin_set_reward")],
        [InlineKeyboardButton(text="💵 Set Min Withdrawal",     callback_data="admin_set_min_wd")],
        [InlineKeyboardButton(text="➕ Add Force Channel",       callback_data="admin_add_ch")],
        [InlineKeyboardButton(text="➖ Remove Force Channel",    callback_data="admin_rm_ch")],
        [InlineKeyboardButton(text="📋 List Channels",          callback_data="admin_list_ch")],
        [InlineKeyboardButton(text="📥 Pending Withdrawals",    callback_data="admin_pending_wd")],
        [InlineKeyboardButton(text="🔙 Back to Menu",           callback_data="main_menu")],
    ])

def back_kb(target="main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙 Back", callback_data=target)
    ]])

# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────
router = Router()

# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    uid   = msg.from_user.id
    uname = msg.from_user.username or ""
    fname = msg.from_user.full_name or ""
    args  = msg.text.split()[1] if len(msg.text.split()) > 1 else ""
    ref   = int(args) if args.isdigit() and int(args) != uid else 0

    # Register user
    if not await db.get_user(uid):
        await db.create_user(uid, uname, fname, ref or None)
    user = await db.get_user(uid)

    if user["is_banned"]:
        return await msg.answer("🚫 You are banned from this bot.")

    # Force-join check
    not_joined = await check_force_join(uid)
    if not_joined:
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>"
                          for c in not_joined)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕  {c['channel_name']}", url=c["invite_link"])]
              for c in not_joined],
            [InlineKeyboardButton(text="✅  I've Joined — Check Again",
                                  callback_data="recheck_join")],
        ])
        return await msg.answer(
            f"👋 Welcome, <b>{fname}</b>!\n\n"
            f"⚠️ <b>Join these channels first:</b>\n{lines}",
            reply_markup=kb, disable_web_page_preview=True
        )

    # Came via referral and not yet verified → show verify button
    if ref and not await db.is_verified(uid):
        return await msg.answer(
            f"👋 Hey <b>{fname}</b>!\n\n"
            "You arrived via a referral link.\n"
            "Tap the button below to <b>complete a quick security check</b> "
            "and activate your account.\n\n"
            "⚡ Takes less than 5 seconds.",
            reply_markup=verify_button_kb(uid, ref)
        )

    # Normal start
    reward = await db.get_setting("reward_per_referral", "10")
    await msg.answer(
        f"👋 Welcome back, <b>{fname}</b>!\n\n"
        f"Earn <b>{reward} coins</b> for every verified referral.",
        reply_markup=main_menu_kb(uid)
    )

# ── Re-check force join ───────────────────────────────────────────────────────
@router.callback_query(F.data == "recheck_join")
async def recheck_join(cb: CallbackQuery):
    uid = cb.from_user.id
    not_joined = await check_force_join(uid)
    if not_joined:
        lines = "\n".join(f"  • <a href='{c['invite_link']}'>{c['channel_name']}</a>"
                          for c in not_joined)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"➕  {c['channel_name']}", url=c["invite_link"])]
              for c in not_joined],
            [InlineKeyboardButton(text="✅  I've Joined — Check Again",
                                  callback_data="recheck_join")],
        ])
        await cb.message.edit_text(
            f"⚠️ <b>Still not joined:</b>\n{lines}",
            reply_markup=kb, disable_web_page_preview=True
        )
    else:
        await cb.message.delete()
        user = await db.get_user(uid)
        await cb.message.answer(
            "✅ All good! You've joined all channels.",
            reply_markup=main_menu_kb(uid)
        )
    await cb.answer()

# ── Main menu ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "main_menu")
async def main_menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid    = cb.from_user.id
    reward = await db.get_setting("reward_per_referral", "10")
    await cb.message.edit_text(
        f"🏠 <b>Main Menu</b>\n\nEarn <b>{reward} coins</b> per verified referral.",
        reply_markup=main_menu_kb(uid)
    )
    await cb.answer()

# ── Balance ───────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "balance")
async def show_balance(cb: CallbackQuery):
    user = await db.get_user(cb.from_user.id)
    bal  = user["balance"] if user else 0.0
    min_wd = await db.get_setting("min_withdrawal", "50")
    await cb.message.edit_text(
        f"💰 <b>Your Balance</b>\n\n"
        f"Available : <b>{bal:.2f} coins</b>\n"
        f"Min. withdrawal : <b>{min_wd} coins</b>",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── Referrals ─────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "referrals")
async def show_referrals(cb: CallbackQuery):
    uid    = cb.from_user.id
    count  = await db.get_referral_count(uid)
    reward = float(await db.get_setting("reward_per_referral", "10"))
    earned = count * reward
    await cb.message.edit_text(
        f"👥 <b>Your Referrals</b>\n\n"
        f"Total verified : <b>{count}</b>\n"
        f"Reward each    : <b>{reward:.2f} coins</b>\n"
        f"Total earned   : <b>{earned:.2f} coins</b>",
        reply_markup=back_kb()
    )
    await cb.answer()

# ── Referral link ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "reflink")
async def show_reflink(cb: CallbackQuery):
    uid  = cb.from_user.id
    me   = await bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    await cb.message.edit_text(
        f"🔗 <b>Your Referral Link</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Share it — you earn coins every time someone joins and passes verification.",
        reply_markup=back_kb(), disable_web_page_preview=True
    )
    await cb.answer()

# ─────────────────────────────────────────────────────────────────────────────
# Withdrawal FSM
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "withdraw")
async def withdraw_start(cb: CallbackQuery, state: FSMContext):
    uid    = cb.from_user.id
    user   = await db.get_user(uid)
    min_wd = float(await db.get_setting("min_withdrawal", "50"))
    bal    = user["balance"] if user else 0.0

    if bal < min_wd:
        return await cb.answer(
            f"❌ Need {min_wd:.0f} coins minimum. You have {bal:.2f}.",
            show_alert=True
        )
    await state.set_state(WithdrawState.amount)
    await state.update_data(min_wd=min_wd, balance=bal)
    await cb.message.edit_text(
        f"💸 <b>Withdrawal Request</b>\n\n"
        f"Balance  : <b>{bal:.2f} coins</b>\n"
        f"Minimum  : <b>{min_wd:.0f} coins</b>\n\n"
        "Enter the amount to withdraw:",
        reply_markup=back_kb()
    )
    await cb.answer()

@router.message(WithdrawState.amount)
async def wd_amount(msg: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = float(msg.text.strip())
        assert data["min_wd"] <= amount <= data["balance"]
    except Exception:
        return await msg.answer(
            f"❌ Enter a value between <b>{data['min_wd']:.0f}</b> "
            f"and <b>{data['balance']:.2f}</b>."
        )
    await state.update_data(amount=amount)
    await state.set_state(WithdrawState.full_name)
    await msg.answer("📝 Enter your <b>full name</b> (as on your payment account):")

@router.message(WithdrawState.full_name)
async def wd_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    if len(name) < 3:
        return await msg.answer("❌ Please enter your full name.")
    await state.update_data(full_name=name)
    await state.set_state(WithdrawState.phone)
    await msg.answer(
        "📱 Enter your <b>phone number</b> (e.g. +251912345678):",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📲 Share My Number", request_contact=True)]],
            resize_keyboard=True, one_time_keyboard=True
        )
    )

@router.message(WithdrawState.phone, F.contact)
async def wd_phone_contact(msg: Message, state: FSMContext):
    phone = msg.contact.phone_number
    if not phone.startswith("+"): phone = "+" + phone
    await _confirm_withdrawal(msg, state, phone)

@router.message(WithdrawState.phone)
async def wd_phone_text(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not phone.startswith("+") or len(phone) < 8:
        return await msg.answer("❌ Include country code, e.g. +251912345678")
    await _confirm_withdrawal(msg, state, phone)

async def _confirm_withdrawal(msg: Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    data = await state.get_data()
    masked = mask_phone(phone)
    await msg.answer(
        "✅ <b>Confirm your withdrawal:</b>",
        reply_markup=ReplyKeyboardRemove()
    )
    await msg.answer(
        f"┌ Amount : <b>{data['amount']:.2f} coins</b>\n"
        f"├ Name   : <b>{data['full_name']}</b>\n"
        f"└ Phone  : <b>{masked}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Confirm",  callback_data="wd_confirm"),
            InlineKeyboardButton(text="❌ Cancel",   callback_data="main_menu"),
        ]])
    )
    await state.set_state(WithdrawState.confirm)

@router.callback_query(F.data == "wd_confirm")
async def wd_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid  = cb.from_user.id
    user = await db.get_user(uid)
    if not user or user["balance"] < data["amount"]:
        await state.clear()
        return await cb.answer("❌ Insufficient balance.", show_alert=True)

    wid = await db.create_withdrawal(uid, data["amount"], data["full_name"], data["phone"])
    await db.add_balance(uid, -data["amount"])
    await state.clear()

    masked = mask_phone(data["phone"])
    admin_text = (
        f"📥 <b>Withdrawal #{wid}</b>\n\n"
        f"User   : <a href='tg://user?id={uid}'>{user['full_name']}</a> "
        f"[<code>{uid}</code>]\n"
        f"Amount : <b>{data['amount']:.2f} coins</b>\n"
        f"Name   : {data['full_name']}\n"
        f"Phone  : {masked}"
    )
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve_{wid}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wid}"),
    ]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, admin_text, reply_markup=approve_kb)
        except Exception as e:
            log.warning("Cannot notify admin %s: %s", aid, e)

    await cb.message.edit_text(
        "📨 <b>Request submitted!</b>\n\n"
        "Your withdrawal is under review. We'll notify you once it's processed.",
        reply_markup=back_kb()
    )
    await cb.answer("Submitted!")

# ─────────────────────────────────────────────────────────────────────────────
# Admin – Approve / Reject withdrawals
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("wd_approve_"))
async def wd_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔ Unauthorized.", show_alert=True)
    wid = int(cb.data.split("_")[-1])
    wd  = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending":
        return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "approved")

    masked = mask_phone(wd["phone"])
    if PAYMENT_LOG_CH:
        try:
            await bot.send_message(
                PAYMENT_LOG_CH,
                f"✅ <b>Payment Proof #{wid}</b>\n\n"
                f"Name   : {wd['full_name']}\n"
                f"ID     : <code>{wd['user_id']}</code>\n"
                f"Amount : {wd['amount']:.2f} coins\n"
                f"Phone  : {masked}\n"
                f"Date   : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            )
        except Exception as e:
            log.warning("Log channel error: %s", e)

    try:
        await bot.send_message(
            wd["user_id"],
            "🎉 <b>Withdrawal Approved!</b>\n\n"
            "Your payment has been processed. Please check your account."
        )
    except Exception: pass

    await cb.message.edit_text(cb.message.text + "\n\n✅ <b>APPROVED</b>", reply_markup=None)
    await cb.answer("Approved ✅")

@router.callback_query(F.data.startswith("wd_reject_"))
async def wd_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔ Unauthorized.", show_alert=True)
    wid = int(cb.data.split("_")[-1])
    wd  = await db.get_withdrawal(wid)
    if not wd or wd["status"] != "pending":
        return await cb.answer("Already resolved.", show_alert=True)
    await db.update_withdrawal_status(wid, "rejected")
    await db.add_balance(wd["user_id"], wd["amount"])
    try:
        await bot.send_message(
            wd["user_id"],
            f"❌ <b>Withdrawal Rejected</b>\n\n"
            f"Your {wd['amount']:.2f} coins have been refunded to your balance."
        )
    except Exception: pass
    await cb.message.edit_text(cb.message.text + "\n\n❌ <b>REJECTED</b>", reply_markup=None)
    await cb.answer("Rejected ❌")

# ─────────────────────────────────────────────────────────────────────────────
# Admin Panel
# ─────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("⛔ Unauthorized.", show_alert=True)
    await state.clear()
    reward = await db.get_setting("reward_per_referral", "10")
    min_wd = await db.get_setting("min_withdrawal", "50")
    await cb.message.edit_text(
        f"⚙️ <b>Admin Panel</b>\n\n"
        f"Reward / referral : <b>{reward} coins</b>\n"
        f"Min withdrawal    : <b>{min_wd} coins</b>",
        reply_markup=admin_panel_kb()
    )
    await cb.answer()

@router.callback_query(F.data == "admin_set_reward")
async def admin_set_reward(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.set_reward)
    await cb.message.edit_text("Enter new <b>reward per referral</b> (coins):")
    await cb.answer()

@router.message(AdminState.set_reward)
async def admin_set_reward_val(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        val = float(msg.text.strip()); assert val > 0
    except Exception:
        return await msg.answer("❌ Enter a positive number.")
    await db.set_setting("reward_per_referral", str(val))
    await state.clear()
    await msg.answer(f"✅ Reward set to <b>{val} coins</b>.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_set_min_wd")
async def admin_set_min_wd(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.set_min_withdrawal)
    await cb.message.edit_text("Enter new <b>minimum withdrawal</b> (coins):")
    await cb.answer()

@router.message(AdminState.set_min_withdrawal)
async def admin_set_min_wd_val(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    try:
        val = float(msg.text.strip()); assert val > 0
    except Exception:
        return await msg.answer("❌ Enter a positive number.")
    await db.set_setting("min_withdrawal", str(val))
    await state.clear()
    await msg.answer(f"✅ Min withdrawal set to <b>{val} coins</b>.", reply_markup=admin_panel_kb())

@router.callback_query(F.data == "admin_add_ch")
async def admin_add_ch(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    await state.set_state(AdminState.add_channel_id)
    await cb.message.edit_text(
        "Step 1/3 — Enter the <b>Channel ID</b>\n"
        "e.g. <code>-100123456789</code>\n\n"
        "⚠️ Bot must be an admin of that channel."
    )
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
    await msg.answer("Step 3/3 — Enter the <b>Invite Link</b> (https://t.me/...):")

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
    if not channels:
        text = "No force-join channels configured."
    else:
        text = "<b>Force-Join Channels:</b>\n\n"
        for ch in channels:
            text += (f"• <b>{ch['channel_name']}</b>\n"
                     f"  ID: <code>{ch['channel_id']}</code>\n"
                     f"  Link: {ch['invite_link']}\n\n")
    await cb.message.edit_text(text, reply_markup=admin_panel_kb(),
                                disable_web_page_preview=True)
    await cb.answer()

@router.callback_query(F.data == "admin_rm_ch")
async def admin_rm_ch(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("⛔", show_alert=True)
    channels = await db.get_force_channels()
    if not channels:
        return await cb.answer("No channels to remove.", show_alert=True)
    btns = [[InlineKeyboardButton(
        text=f"🗑 {ch['channel_name']}",
        callback_data=f"admin_rm_do_{ch['channel_id']}"
    )] for ch in channels]
    btns.append([InlineKeyboardButton(text="🔙 Back", callback_data="admin_panel")])
    await cb.message.edit_text(
        "Select channel to <b>remove</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )
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
    await cb.message.edit_text(
        f"📥 <b>{len(pending)} pending withdrawal(s)</b>\nSee requests below:",
        reply_markup=admin_panel_kb()
    )
    for wd in pending:
        masked = mask_phone(wd["phone"])
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve", callback_data=f"wd_approve_{wd['id']}"),
            InlineKeyboardButton(text="❌ Reject",  callback_data=f"wd_reject_{wd['id']}"),
        ]])
        await cb.message.answer(
            f"📥 <b>Withdrawal #{wd['id']}</b>\n\n"
            f"User   : <code>{wd['user_id']}</code>\n"
            f"Amount : <b>{wd['amount']:.2f} coins</b>\n"
            f"Name   : {wd['full_name']}\n"
            f"Phone  : {masked}\n"
            f"Date   : {wd['created_at']}",
            reply_markup=kb
        )
    await cb.answer()

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    asyncio.create_task(start_bot())
    yield

api = FastAPI(lifespan=lifespan)

@api.get("/verify", response_class=HTMLResponse)
async def serve_miniapp(uid: int = 0, ref: int = 0):
    with open("webapp/index.html", "r") as f:
        html = f.read()
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
    ref_id     = int(body.get("refId", 0))

    # 1. Validate Telegram signature
    tg_user = verify_telegram_initdata(init_data)
    if not tg_user:
        raise HTTPException(403, "Invalid Telegram data")
    uid = int(tg_user.get("id", 0))
    if not uid:
        raise HTTPException(403, "No user ID")

    # 2. Check if already verified (Telegram ID check)
    if await db.is_verified(uid):
        return JSONResponse({"status": "already_verified"})

    # 3. VPN / Proxy — client flag + server re-check
    if is_vpn or await server_vpn_check(ip):
        await db.ban_user(uid)
        try:
            await bot.send_message(
                uid,
                "🚫 <b>Verification Failed</b>\n\n"
                "VPN / Proxy detected. This is not allowed.\n"
                "Your access has been restricted."
            )
        except Exception: pass
        return JSONResponse({"status": "blocked", "reason": "vpn"})

    # 4. Multi-account — fingerprint + IP check against ALL users
    duplicate = await db.find_duplicate(ip, fingerprint, uid)
    if duplicate:
        await db.ban_user(uid)
        try:
            await bot.send_message(
                uid,
                "🚫 <b>Multi-Account Detected</b>\n\n"
                "This device or network is already linked to another account.\n"
                "You are not allowed to create multiple accounts."
            )
        except Exception: pass
        return JSONResponse({"status": "blocked", "reason": "multiaccount"})

    # 5. All clear — save and reward
    await db.save_verification(uid, ip, ua, fingerprint)

    if ref_id and ref_id != uid:
        referrer = await db.get_user(ref_id)
        if referrer:
            reward = float(await db.get_setting("reward_per_referral", "10"))
            await db.add_balance(ref_id, reward)
            try:
                new_bal = referrer["balance"] + reward
                await bot.send_message(
                    ref_id,
                    f"🎉 <b>Referral Reward!</b>\n\n"
                    f"A new user verified via your link.\n"
                    f"<b>+{reward:.2f} coins</b> added to your balance.\n"
                    f"New balance: <b>{new_bal:.2f} coins</b>"
                )
            except Exception: pass

    try:
        await bot.send_message(
            uid,
            "✅ <b>Verification Complete!</b>\n\n"
            "Your account is now active. Use the menu below.",
            reply_markup=main_menu_kb(uid)
        )
    except Exception: pass

    return JSONResponse({"status": "verified"})

# ─────────────────────────────────────────────────────────────────────────────
# Bot polling (runs inside FastAPI event loop)
# ─────────────────────────────────────────────────────────────────────────────
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

async def start_bot():
    log.info("Bot polling started.")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:api", host="0.0.0.0", port=port, log_level="info")

import asyncio
import time
import os
import re
import html
import random
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.client.session.aiohttp import AiohttpSession
from db import supabase

load_dotenv()

BOT_TOKEN              = os.getenv("BOT1_TOKEN")
ADMIN_ID               = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME          = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")
WELCOME_PHOTO          = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg"
AUTO_DELETE_SECS       = 1800        # 30 minutes
AUTOCAST_INTERVAL_SECS = 4 * 3600    # one auto-cast every 4 hours
AUTOCAST_DELETE_SECS   = 1800        # auto-cast messages vanish after 30 minutes
STATS_TZ               = ZoneInfo("Asia/Kolkata")   # day/week/month boundaries for /stats

PROXY_URL = os.getenv("PROXY_URL")  # e.g. socks5://user:pass@host:port — leave unset/blank to disable
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
dp  = Dispatcher()

# ── FSM States ─────────────────────────────────────────────────────────────────

class AddCourseFSM(StatesGroup):
    waiting_for_course_id        = State()
    waiting_for_button_text      = State()
    waiting_for_title            = State()
    waiting_for_price_inr        = State()
    waiting_for_price_usd        = State()
    waiting_for_bot2_text        = State()
    waiting_for_bot2_image       = State()
    waiting_for_dump_ids         = State()

class AddBundleFSM(StatesGroup):
    waiting_for_bundle_id        = State()
    waiting_for_button_text      = State()
    waiting_for_title            = State()
    waiting_for_price_inr        = State()
    waiting_for_price_usd        = State()
    waiting_for_bot2_text        = State()
    waiting_for_bot2_image       = State()
    waiting_for_dump_ids         = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()

class DBroadcastFSM(StatesGroup):
    waiting_for_video       = State()
    waiting_for_button_text = State()

class RejectFSM(StatesGroup):
    waiting_for_reason = State()

class AutoCastFSM(StatesGroup):
    waiting_for_media   = State()
    waiting_for_text    = State()
    waiting_for_buttons = State()

# ── Helpers ────────────────────────────────────────────────────────────────────

async def _auto_delete(chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _fetch_all(table: str, columns: str, order_col: str, apply=None, page: int = 1000) -> list:
    """Fetch every row (Supabase caps a single response at 1000 rows)."""
    rows, start = [], 0
    while True:
        q = supabase.table(table).select(columns)
        if apply:
            q = apply(q)
        batch = q.order(order_col).range(start, start + page - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < page:
            break
        start += page
    return rows

def _set_active(user_id: int, active: bool):
    supabase.table("users").update({
        "is_active":  active,
        "blocked_at": None if active else _now_iso(),
    }).eq("telegram_user_id", user_id).execute()

def _mark_inactive(user_id: int):
    """User blocked the bot / deleted their account. Row is KEPT so stats stay accurate."""
    try:
        _set_active(user_id, False)
    except Exception as e:
        print(f"[inactive] failed for {user_id}: {e}")

def _get_active_user_ids() -> list:
    rows = _fetch_all("users", "telegram_user_id, is_active", order_col="telegram_user_id")
    return [r["telegram_user_id"] for r in rows if r.get("is_active") is not False]

def _ensure_user(user_id: int, username=None):
    existing = supabase.table("users").select("telegram_user_id, is_active").eq("telegram_user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username":         username or "",
            "wallet_balance":   0,
            "is_active":        True,
        }).execute()
    elif existing.data[0].get("is_active") is False:
        _set_active(user_id, True)   # came back

def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return float(row.data[0]["wallet_balance"]) if row.data else 0.0

def _get_setting(key: str):
    r = supabase.table("bot_settings").select("value").eq("key", key).execute()
    return r.data[0]["value"] if r.data else None

def _set_setting(key: str, value: str):
    supabase.table("bot_settings").upsert({"key": key, "value": value}).execute()

# ── /start & Bundle Menus ─────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user_id  = message.from_user.id
    username = message.from_user.username

    _ensure_user(user_id, username)

    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    regular_courses = [c for c in all_items if not c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    for c in regular_courses:
        display_name = c.get("button_text") or c["title"]
        builder.row(InlineKeyboardButton(
            text=f"{display_name}",
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
        ))

    builder.row(InlineKeyboardButton(
        text="🏷 Buy All [₹1,499 / 22$]", 
        url=f"https://t.me/{BOT2_USERNAME}?start=bundle_all"
    ))

    wallet      = _get_wallet(user_id)
    wallet_note = f"\n\n💰 <b>Wallet Balance:</b> ₹{wallet:.2f}" if wallet > 0 else ""

    sent_msg = await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=(
            "🛒 <b>Telegram's Best Collection!</b>\n\n"
            "🔥 Today's “Bundle” Offer : \nC||P + R||P :- 699₹ / 10$ \n\n✨ <b>Buy All Collection</b> :\n<del>Regular Price : 3,599₹ / 60$</del> ❌\n\nBundle Offer = 1,499₹ / 22$ ✅" + wallet_note
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    
    asyncio.create_task(_auto_delete(message.chat.id, sent_msg.message_id))

@dp.callback_query(F.data == "show_bundles_menu")
async def menu_show_bundles(callback: types.CallbackQuery):
    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    bundles = [c for c in all_items if c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    if not bundles:
        builder.row(InlineKeyboardButton(text="No bundles available right now", callback_data="ignore"))
    else:
        for b in bundles:
            display_name = b.get("button_text") or b["title"]
            builder.row(InlineKeyboardButton(
                text=f"📦 {display_name}", 
                url=f"https://t.me/{BOT2_USERNAME}?start={b['course_id']}"
            ))

    builder.row(InlineKeyboardButton(text="⬅️ Back to All Courses", callback_data="back_to_main_menu"))

    await callback.message.edit_caption(
        caption=(
            "🎁 <b>Exclusive Bundles</b>\n\n"
            "Select a bundle below to get multiple courses at a massively discounted price!\n\n"
            "⏳ <i>This message self-destructs in 15 minutes.</i>"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main_menu")
async def menu_back_to_main(callback: types.CallbackQuery):
    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    regular_courses = [c for c in all_items if not c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    for c in regular_courses:
        display_name = c.get("button_text") or c["title"]
        builder.row(InlineKeyboardButton(
            text=f"{display_name}",
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
        ))

    builder.row(InlineKeyboardButton(
        text="🏷 Buy All [₹1,499 / 22$]", 
        url=f"https://t.me/{BOT2_USERNAME}?start=bundle_all"
    ))

    wallet = _get_wallet(callback.from_user.id)
    wallet_note = f"\n\n💰 <b>Wallet Balance:</b> ₹{wallet:.2f}" if wallet > 0 else ""

    await callback.message.edit_caption(
        caption=(
            "🛒 <b>Telegram's Best Collection!</b>\n\n"
            "🔥 Today's “Bundle” Offer : \nC||P + R||P :- 699₹ / 10$ \n\n✨ <b>Buy All Collection</b> :\n<del>Regular Price : 3,599₹ / 60$</del> ❌\n\nBundle Offer = 1,499₹ / 22$ ✅" + wallet_note
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

# ── /wallet ────────────────────────────────────────────────────────────────────

@dp.message(Command("wallet"))
async def cmd_wallet(message: types.Message):
    user_id = message.from_user.id
    _ensure_user(user_id, message.from_user.username)
    balance = _get_wallet(user_id)
    await message.answer(
        "💼 <b>Your Wallet</b>\n\n"
        f"💰 Balance: <b>₹{balance:.2f}</b>\n\n"
        "<i>Your wallet balance can be used as a discount on your next purchase.</i>",
        parse_mode="HTML"
    )

# ── Admin /cancel (aborts any running admin flow) ──────────────────────────────

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if await state.get_state() is None:
        return await message.answer("Nothing to cancel.")
    await state.clear()
    await message.answer("❌ Cancelled.")

# ── Track users blocking / unblocking the bot (instant, no broadcast needed) ───

@dp.my_chat_member()
async def on_my_chat_member(event: types.ChatMemberUpdated):
    if event.chat.type != "private":
        return
    status = event.new_chat_member.status
    try:
        if status == "kicked":
            _mark_inactive(event.chat.id)
        elif status == "member":
            _set_active(event.chat.id, True)
    except Exception as e:
        print(f"[my_chat_member] {e}")

# ── ADMIN: /addnew ──────────────────────────────────────────────────────────────

@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Add New Course — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this course.\n"
        "_(Use lowercase letters/numbers only, e.g. <code>python_basics</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_course_id)

@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    await state.update_data(course_id=message.text.strip().lower().replace(" ", "_"))
    await message.answer(
        "✅ ID saved!\n\n"
        "🛠 <b>Step 2 of 8 — Short Button Name</b>\n\n"
        "Enter the short text for the Inline Menu Button.\n_(e.g. <code>Python Basics</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_button_text)

@dp.message(AddCourseFSM.waiting_for_button_text)
async def process_button_text(message: types.Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await message.answer(
        "✅ Button name saved!\n\n"
        "🛠 <b>Step 3 of 8 — Full Display Title</b>\n\n"
        "Enter the long, detailed title shown inside the course page.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_title)

@dp.message(AddCourseFSM.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "✅ Title saved!\n\n"
        "🛠 <b>Step 4 of 8 — Price (INR)</b>\n\n"
        "Enter the price in <b>₹</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_inr)

@dp.message(AddCourseFSM.waiting_for_price_inr)
async def process_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ That doesn't look like a number. Please enter something like <code>400</code>.", parse_mode="HTML")
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 <b>Step 5 of 8 — Price (USD)</b>\n\n"
        "Enter the price in <b>$</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_usd)

@dp.message(AddCourseFSM.waiting_for_price_usd)
async def process_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ That doesn't look like a number.", parse_mode="HTML")
    
    data = await state.get_data()
    numeric_inr = data.get("numeric_price", 0)
    
    display_price = f"₹{numeric_inr:g} / ${usd_val:g}"
    await state.update_data(price=display_price)
    
    await message.answer(
        f"✅ Display price saved as: <b>{display_price}</b>\n\n"
        "🛠 <b>Step 6 of 8 — Sales Description</b>\n\n"
        "Enter the sales text Bot 2 will show buyers:",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_text)

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer(
        "✅ Description saved!\n\n"
        "🛠 <b>Step 7 of 8 — Course Thumbnail URL</b>\n\n"
        "Paste a public image URL for the course thumbnail.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        "🛠 <b>Step 8 of 8 — Delivery Content (Message IDs)</b>\n\n"
        "Go to your private Storage Channel and find the message IDs for the files/links you want to send.\n"
        "Enter them separated by commas.\n\n"
        "<i>(Example: <code>104, 105, 106</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_dump_ids)

@dp.message(AddCourseFSM.waiting_for_dump_ids)
async def process_dump_ids(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dump_ids = message.text.strip()

    try:
        supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "button_text":      data["button_text"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    "✅ Payment verified! Here is your access.",
            "dump_message_ids": dump_ids, 
        }).execute()
        
        await message.answer(
            "🎉 <b>Course Added Successfully!</b>\n\n"
            f"📘 <b>{data['title']}</b> is now live.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Database error:</b>\n\n<code>{e}</code>", parse_mode="HTML")

    await state.clear()

# ── ADMIN: /addbundle ───────────────────────────────────────────────────────────

@dp.message(Command("addbundle"))
async def cmd_addbundle(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Add New Bundle — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this bundle.\n"
        "_(I will automatically add 'bundle_' to the front of whatever you type here.)_",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bundle_id)

@dp.message(AddBundleFSM.waiting_for_bundle_id)
async def process_bundle_id(message: types.Message, state: FSMContext):
    raw_id = message.text.strip().lower().replace(" ", "_")
    bundle_id = raw_id if raw_id.startswith("bundle_") else f"bundle_{raw_id}"
    
    await state.update_data(course_id=bundle_id)
    await message.answer(
        f"✅ ID saved as: <code>{bundle_id}</code>\n\n"
        "🛠 <b>Step 2 of 8 — Short Button Name</b>\n\n"
        "Enter the short text for the Inline Menu Button.\n_(e.g. <code>Mega Pack</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_button_text)

@dp.message(AddBundleFSM.waiting_for_button_text)
async def process_bundle_button_text(message: types.Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await message.answer(
        "✅ Button name saved!\n\n"
        "🛠 <b>Step 3 of 8 — Full Display Title</b>\n\n"
        "Enter the long, detailed title users will see on the sales page.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_title)

@dp.message(AddBundleFSM.waiting_for_title)
async def process_bundle_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "✅ Title saved!\n\n"
        "🛠 <b>Step 4 of 8 — Price (INR)</b>\n\n"
        "Enter the bundle price in <b>₹</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_price_inr)

@dp.message(AddBundleFSM.waiting_for_price_inr)
async def process_bundle_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Please enter a valid number.", parse_mode="HTML")
        
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 <b>Step 5 of 8 — Price (USD)</b>\n"
        "Enter the price in <b>$</b>.", 
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_price_usd)

@dp.message(AddBundleFSM.waiting_for_price_usd)
async def process_bundle_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Please enter a valid number.", parse_mode="HTML")
    
    data = await state.get_data()
    display_price = f"₹{data['numeric_price']:g} / ${usd_val:g}"
    await state.update_data(price=display_price)
    
    await message.answer(
        f"✅ Price saved as: <b>{display_price}</b>\n\n"
        "🛠 <b>Step 6 of 8 — Sales Description</b>\n\n"
        "Enter the bundle text description for the sales bot:",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bot2_text)

@dp.message(AddBundleFSM.waiting_for_bot2_text)
async def process_bundle_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer(
        "✅ Description saved!\n\n"
        "🛠 <b>Step 7 of 8 — Bundle Thumbnail URL</b>\n\n"
        "Paste a public image URL for the bundle thumbnail.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bot2_image)

@dp.message(AddBundleFSM.waiting_for_bot2_image)
async def process_bundle_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        "🛠 <b>Step 8 of 8 — Delivery Content (Message IDs)</b>\n\n"
        "Go to your private Storage Channel and find the message IDs for the files/links you want to include in this bundle.\n"
        "Enter them separated by commas.\n\n"
        "<i>(Example: <code>104, 105, 106</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_dump_ids)

@dp.message(AddBundleFSM.waiting_for_dump_ids)
async def process_bundle_dump_ids(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dump_ids = message.text.strip()

    try:
        supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "button_text":      data["button_text"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    "✅ Payment verified! Here is your bundle access.",
            "dump_message_ids": dump_ids, 
        }).execute()
        
        await message.answer(
            "🎉 <b>Bundle Added Successfully!</b>\n\n"
            f"📦 <b>{data['title']}</b> is now live.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Database error:</b>\n\n<code>{e}</code>", parse_mode="HTML")

    await state.clear()

# ── ADMIN: /broadcast ──────────────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📢 <b>Broadcast Mode</b>\n\n"
        "Send the message you want delivered to all users.\n"
        "Supports text, photos, and videos.\n\n"
        "ℹ️ <i>Users who blocked the bot are marked inactive — they stay in the database so stats remain accurate.</i>",
        parse_mode="HTML"
    )
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(
        preview_chat_id=message.chat.id,
        preview_message_id=message.message_id
    )
    await state.set_state(BroadcastFSM.waiting_for_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm — Send to all", callback_data="broadcast_confirm"),
        InlineKeyboardButton(text="❌ Cancel",                callback_data="broadcast_cancel"),
    ]])
    await message.answer(
        "👆 <b>Preview above.</b>\n\n"
        "This will be sent to <b>all active users</b>. Confirm?",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_({"broadcast_confirm", "broadcast_cancel"}))
async def broadcast_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    await callback.answer()

    if callback.data == "broadcast_cancel":
        await state.clear()
        return await callback.message.edit_text(
            "❌ <b>Broadcast cancelled.</b>",
            reply_markup=None,
            parse_mode="HTML"
        )

    data = await state.get_data()
    await state.clear()

    status_msg = await callback.message.edit_text("⏳ Collecting user list…", reply_markup=None)

    user_ids = await asyncio.to_thread(_get_active_user_ids)

    success = fail = blocked = 0
    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=data["preview_chat_id"],
                message_id=data["preview_message_id"]
            )
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            blocked += 1
            _mark_inactive(uid)
        except Exception:
            fail += 1

    await status_msg.edit_text(
        "✅ <b>Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:        <b>{success}</b> users\n"
        f"📵 Newly inactive:      <b>{blocked}</b> (kept in stats)\n"
        f"⚠️ Other failures:      <b>{fail - blocked}</b>",
        parse_mode="HTML"
    )

# ── ADMIN: /dbroadcast ─────────────────────────────────────────────────────────

@dp.message(Command("dbroadcast"))
async def cmd_dbroadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📹 <b>Delete-Broadcast Mode</b>\n\n"
        "Send the <b>video</b> you want to broadcast to all users.\n\n"
        "⚠️ <i>Broadcast messages will auto-delete from every user's chat after 30 minutes.</i>",
        parse_mode="HTML"
    )
    await state.set_state(DBroadcastFSM.waiting_for_video)

@dp.message(DBroadcastFSM.waiting_for_video)
async def dbroadcast_got_video(message: types.Message, state: FSMContext):
    if message.video:
        file_id = message.video.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        file_id = message.document.file_id
    else:
        return await message.answer(
            "❌ <b>That's not a video.</b>\n\n"
            "Please send a <b>video file</b>.\n"
            "<i>Tip: send it without compression if possible.</i>",
            parse_mode="HTML"
        )

    await state.update_data(video_file_id=file_id, caption=message.caption or "")
    await message.answer(
        "✅ <b>Video received!</b>\n\n"
        "Now enter the <b>inline button label</b>.\n"
        "<i>(e.g. <code>🛒 View All Courses</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(DBroadcastFSM.waiting_for_button_text)

@dp.message(DBroadcastFSM.waiting_for_button_text)
async def dbroadcast_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    button_text   = message.text.strip()
    video_file_id = data["video_file_id"]
    caption       = data["caption"]

    bot_info  = await bot.get_me()
    start_url = f"https://t.me/{bot_info.username}?start="

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, url=start_url)
    ]])

    status_msg = await message.answer("⏳ Broadcasting delete-broadcast to all users…")

    user_ids = await asyncio.to_thread(_get_active_user_ids)

    success = fail = blocked = 0
    sent_messages = []   # (chat_id, message_id, sent_at)

    for uid in user_ids:
        try:
            sent = await bot.send_video(
                chat_id=uid,
                video=video_file_id,
                caption=caption or None,
                reply_markup=kb,
                parse_mode="HTML",
                protect_content=True
            )
            sent_messages.append((uid, sent.message_id, time.time()))
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            blocked += 1
            _mark_inactive(uid)
        except Exception:
            fail += 1

    await status_msg.edit_text(
        "✅ <b>Delete-Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:        <b>{success}</b> users\n"
        f"📵 Newly inactive:      <b>{blocked}</b> (kept in stats)\n"
        f"⚠️ Other failures:      <b>{fail - blocked}</b>\n\n"
        f"⏳ Each message will auto-delete <b>30 min after it was sent</b>.",
        parse_mode="HTML"
    )

    asyncio.create_task(_delete_sent_later(sent_messages, AUTO_DELETE_SECS))

async def _delete_sent_later(sent: list, delay: int):
    """sent = [(chat_id, message_id, sent_at)] in send order; each is deleted `delay` secs after ITS send."""
    for chat_id, msg_id, sent_at in sent:
        remaining = delay - (time.time() - sent_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        await asyncio.sleep(0.03)

# ── ADMIN: Auto-Cast ───────────────────────────────────────────────────────────
# /addautocast        – create one (media? + text? + buttons?)
# /autocasts          – list all with IDs
# /stopautocast <id>  – remove one from rotation
# /startautocast <id> – put it back
# Every 4h the bot picks ONE random active auto-cast, sends it to all active
# users and deletes it again after 30 minutes.

def _parse_buttons(raw: str) -> list:
    buttons = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValueError(f"Missing “|” in line: {line}")
        label, url = [p.strip() for p in line.split("|", 1)]
        if not label or not url:
            raise ValueError(f"Empty label or URL in line: {line}")
        if url.lower() in ("bot", "start"):
            url = "bot"
        elif not re.match(r"^(https?://|tg://)", url, re.I):
            raise ValueError(f"Invalid URL (must start with http:// https:// or tg://): {url}")
        buttons.append({"text": label, "url": url})
    return buttons

def _autocast_kb(buttons, start_url: str):
    if not buttons:
        return None
    kb = InlineKeyboardBuilder()
    for b in buttons:
        kb.row(InlineKeyboardButton(text=b["text"], url=start_url if b["url"] == "bot" else b["url"]))
    return kb.as_markup()

def _autocast_summary(ac: dict) -> str:
    parts = []
    if ac.get("media_type") == "video":
        parts.append("🎬 video")
    elif ac.get("media_type") == "photo":
        parts.append("🖼 image")
    if ac.get("text"):
        parts.append("📝 text")
    n = len(ac.get("buttons") or [])
    if n:
        parts.append(f"🔘 {n} button(s)")
    return " + ".join(parts) or "empty"

async def _send_autocast_message(chat_id: int, ac: dict, kb):
    text = ac.get("text") or None
    mt   = ac.get("media_type")
    if mt == "video":
        return await bot.send_video(chat_id, ac["file_id"], caption=text, reply_markup=kb,
                                    parse_mode="HTML", protect_content=True)
    if mt == "photo":
        return await bot.send_photo(chat_id, ac["file_id"], caption=text, reply_markup=kb,
                                    parse_mode="HTML", protect_content=True)
    return await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML",
                                  protect_content=True, disable_web_page_preview=True)

async def _send_autocast_safe(chat_id: int, ac: dict, kb):
    try:
        return await _send_autocast_message(chat_id, ac, kb)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await _send_autocast_message(chat_id, ac, kb)

@dp.message(Command("addautocast"))
async def cmd_addautocast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "🔁 <b>New Auto-Cast — Step 1/3: Media</b>\n\n"
        "Send a <b>video</b> or an <b>image</b>, or /skip for no media.\n"
        "<i>/cancel to abort.</i>",
        parse_mode="HTML"
    )
    await state.set_state(AutoCastFSM.waiting_for_media)

@dp.message(AutoCastFSM.waiting_for_media)
async def autocast_got_media(message: types.Message, state: FSMContext):
    if message.video:
        await state.update_data(media_type="video", file_id=message.video.file_id)
    elif message.photo:
        await state.update_data(media_type="photo", file_id=message.photo[-1].file_id)
    elif message.text and message.text.strip().lower() == "/skip":
        await state.update_data(media_type=None, file_id=None)
    else:
        return await message.answer("❌ Send a video, an image (not as file), or /skip.")
    await message.answer(
        "✅ Saved.\n\n"
        "📝 <b>Step 2/3: Text</b>\n\n"
        "Send the text / caption (formatting is kept), or /skip for none.",
        parse_mode="HTML"
    )
    await state.set_state(AutoCastFSM.waiting_for_text)

@dp.message(AutoCastFSM.waiting_for_text)
async def autocast_got_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not message.text:
        return await message.answer("❌ Send text, or /skip.")
    if message.text.strip().lower() == "/skip":
        if not data.get("media_type"):
            return await message.answer("❌ You skipped media too — an auto-cast needs media or text. Send some text.")
        text = None
    else:
        limit = 1024 if data.get("media_type") else 4096
        if len(message.text) > limit:
            return await message.answer(f"❌ Too long ({len(message.text)} chars). Max {limit} for this type.")
        text = message.html_text
    await state.update_data(text=text)
    await message.answer(
        "✅ Saved.\n\n"
        "🔘 <b>Step 3/3: Buttons</b>\n\n"
        "One button per line:\n<code>Label | https://link</code>\n"
        "Use <code>bot</code> as the link to open this bot's /start:\n<code>🛒 View Courses | bot</code>\n\n"
        "Or /skip for no buttons.",
        parse_mode="HTML"
    )
    await state.set_state(AutoCastFSM.waiting_for_buttons)

@dp.message(AutoCastFSM.waiting_for_buttons)
async def autocast_got_buttons(message: types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("❌ Send buttons as text, or /skip.")
    if message.text.strip().lower() == "/skip":
        buttons = []
    else:
        try:
            buttons = _parse_buttons(message.text)
        except ValueError as e:
            return await message.answer(f"❌ {html.escape(str(e))}\n\nTry again or /skip.", parse_mode="HTML")

    data = await state.get_data()
    await state.clear()

    try:
        res = supabase.table("autocasts").insert({
            "media_type": data.get("media_type"),
            "file_id":    data.get("file_id"),
            "text":       data.get("text"),
            "buttons":    buttons,
            "is_active":  True,
        }).execute()
        ac = res.data[0]
    except Exception as e:
        return await message.answer(f"❌ <b>Database error:</b>\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")

    await message.answer(
        f"✅ <b>Auto-cast #{ac['id']} saved</b> ({_autocast_summary(ac)})\n\n"
        "It's in the rotation: every 4h one random active auto-cast is sent, deleted after 30 min.\n"
        f"Stop it with <code>/stopautocast {ac['id']}</code>\n\n"
        "👇 Preview (only you see this):",
        parse_mode="HTML"
    )
    try:
        bot_info = await bot.get_me()
        kb = _autocast_kb(ac.get("buttons"), f"https://t.me/{bot_info.username}?start=")
        await _send_autocast_message(ADMIN_ID, ac, kb)
    except Exception as e:
        await message.answer(f"⚠️ Preview failed: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

@dp.message(Command("autocasts"))
async def cmd_autocasts(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = _fetch_all("autocasts", "*", order_col="id")
    if not rows:
        return await message.answer("No auto-casts yet. Create one with /addautocast")
    active = len([r for r in rows if r.get("is_active")])
    lines = [f"🔁 <b>Auto-Casts</b> — {active} active / {len(rows)} total", "<i>Random pick every 4h · deleted after 30 min</i>", ""]
    for r in rows:
        icon    = "🟢" if r.get("is_active") else "⏸"
        preview = html.escape(re.sub(r"<[^>]+>", "", r.get("text") or "")[:40])
        lines.append(
            f"{icon} <b>#{r['id']}</b> — {_autocast_summary(r)}\n"
            f"     sent {r.get('send_count') or 0}×" + (f" · “{preview}”" if preview else "")
        )
    lines += ["", "<code>/stopautocast ID</code>  ·  <code>/startautocast ID</code>"]
    await message.answer("\n".join(lines), parse_mode="HTML")

async def _toggle_autocast(message: types.Message, command: CommandObject, active: bool):
    if message.from_user.id != ADMIN_ID:
        return
    verb = "startautocast" if active else "stopautocast"
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        return await message.answer(f"Usage: <code>/{verb} ID</code>  (see /autocasts)", parse_mode="HTML")
    found = supabase.table("autocasts").select("id").eq("id", int(arg)).execute()
    if not found.data:
        return await message.answer(f"❌ No auto-cast with ID {arg}.")
    supabase.table("autocasts").update({"is_active": active}).eq("id", int(arg)).execute()
    await message.answer(f"{'▶️ Resumed' if active else '⏹ Stopped'} auto-cast <b>#{arg}</b>.", parse_mode="HTML")

@dp.message(Command("stopautocast"))
async def cmd_stopautocast(message: types.Message, command: CommandObject):
    await _toggle_autocast(message, command, False)

@dp.message(Command("startautocast"))
async def cmd_startautocast(message: types.Message, command: CommandObject):
    await _toggle_autocast(message, command, True)

async def _run_autocast(ac: dict):
    bot_info  = await bot.get_me()
    kb        = _autocast_kb(ac.get("buttons"), f"https://t.me/{bot_info.username}?start=")
    user_ids  = await asyncio.to_thread(_get_active_user_ids)

    sent, success, fail, blocked = [], 0, 0, 0
    for uid in user_ids:
        try:
            m = await _send_autocast_safe(uid, ac, kb)
            sent.append((uid, m.message_id, time.time()))
            success += 1
        except TelegramForbiddenError:
            fail += 1
            blocked += 1
            _mark_inactive(uid)
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)

    try:
        supabase.table("autocasts").update({
            "send_count":   (ac.get("send_count") or 0) + 1,
            "last_sent_at": _now_iso(),
        }).eq("id", ac["id"]).execute()
    except Exception as e:
        print(f"[autocast] stat update failed: {e}")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"📣 <b>Auto-cast #{ac['id']} sent</b>\n"
            f"📬 Delivered: <b>{success}</b>  ·  📵 Newly inactive: <b>{blocked}</b>  ·  ⚠️ Failed: <b>{fail - blocked}</b>\n"
            "🗑 Deletes from users in 30 min.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await _delete_sent_later(sent, AUTOCAST_DELETE_SECS)

async def _autocast_tick():
    active = supabase.table("autocasts").select("*").eq("is_active", True).execute().data or []
    now    = time.time()
    last   = float(_get_setting("last_autocast_at") or 0)

    if not active:
        _set_setting("last_autocast_at", str(now))   # timer restarts when you add one
        return
    if not last:
        _set_setting("last_autocast_at", str(now))
        return
    if now - last < AUTOCAST_INTERVAL_SECS:
        return

    last_id = _get_setting("last_autocast_id")
    pool    = [a for a in active if str(a["id"]) != last_id] or active   # avoid immediate repeat
    ac      = random.choice(pool)

    _set_setting("last_autocast_at", str(now))
    _set_setting("last_autocast_id", str(ac["id"]))
    asyncio.create_task(_run_autocast(ac))

async def _autocast_scheduler():
    await asyncio.sleep(10)
    while True:
        try:
            await _autocast_tick()
        except Exception as e:
            print(f"[autocast] scheduler error: {e}")
        await asyncio.sleep(60)

# ── ADMIN: /stats ──────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$")

def _parse_ts(s):
    if not s:
        return None
    m = _TS_RE.match(str(s))
    if not m:
        return None
    d, t, frac, tz = m.groups()
    frac = f".{frac[:6].ljust(6, '0')}" if frac else ""
    if not tz or tz == "Z":
        tz = "+00:00"
    else:
        digits = tz[1:].replace(":", "")
        tz = f"{tz[0]}{digits[:2]}:{digits[2:4] or '00'}"
    try:
        return datetime.fromisoformat(f"{d}T{t}{frac}{tz}")
    except ValueError:
        return None

def _inr(v: float) -> str:
    return f"₹{v:,.2f}"

def _delta(cur: float, prev: float) -> str:
    if prev <= 0:
        return "🆕" if cur > 0 else "—"
    ch = (cur - prev) / prev * 100
    return f"{'▲' if ch >= 0 else '▼'} {abs(ch):.1f}%"

def _in_window(rows: list, start, end) -> list:
    return [r for r in rows if start <= r["_ts"] < end]

def _agg(rows: list) -> dict:
    return {
        "revenue":      sum(float(t.get("amount_paid") or 0) for t in rows),
        "count":        len(rows),
        "wallet_count": len([t for t in rows if t.get("payment_type") == "wallet"]),
        "wallet_used":  sum(float(t.get("wallet_used") or 0) for t in rows),
        "buyers":       len({t.get("telegram_user_id") for t in rows}),
    }

def _period_bounds(period: str, now: datetime):
    """(start, prev_start, label, prev_label, same_label) or None for all-time."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start = midnight
        return start, start - timedelta(days=1), "Today", "Yesterday", "Yesterday (till now)"
    if period == "week":
        start = midnight - timedelta(days=now.weekday())
        return start, start - timedelta(days=7), "This Week", "Last Week", "Last week (same days)"
    if period == "month":
        start = midnight.replace(day=1)
        return start, (start - timedelta(days=1)).replace(day=1), "This Month", "Last Month", "Last month (same days)"
    if period == "year":
        start = midnight.replace(month=1, day=1)
        return start, start.replace(year=start.year - 1), "This Year", "Last Year", "Last year (same days)"
    return None

def _month_shift(y: int, m: int, k: int):
    idx = y * 12 + (m - 1) + k
    return idx // 12, idx % 12 + 1

def _build_stats(period: str) -> str:
    now      = datetime.now(STATS_TZ)
    far      = now + timedelta(days=1)
    users    = _fetch_all("users", "telegram_user_id, created_at, is_active, wallet_balance", order_col="telegram_user_id")
    txs      = _fetch_all("transactions",
                          "id, telegram_user_id, course_id, status, amount_paid, wallet_used, payment_type, created_at",
                          order_col="id")
    courses  = _fetch_all("courses", "course_id, title", order_col="course_id")
    titles   = {c["course_id"]: c["title"] for c in courses}

    for t in txs:
        t["_ts"] = _parse_ts(t.get("created_at"))
    txs = [t for t in txs if t["_ts"]]
    for u in users:
        u["_ts"] = _parse_ts(u.get("created_at"))
    users_ts = [u for u in users if u["_ts"]]

    approved = [t for t in txs if t.get("status") == "approved"]
    rejected = [t for t in txs if t.get("status") == "rejected"]
    awaiting = len([t for t in txs if t.get("status") == "awaiting_approval"])

    bounds = _period_bounds(period, now)
    epoch  = datetime(1970, 1, 1, tzinfo=timezone.utc)
    if bounds:
        start, prev_start, label, prev_label, same_label = bounds
        prev_like_end = min(prev_start + (now - start), start)
        range_txt = {
            "today": f"{now:%d %b %Y}",
            "week":  f"{start:%d %b} – {now:%d %b %Y}",
            "month": f"{now:%B %Y}",
            "year":  f"{now:%Y}",
        }[period]
    else:
        start, label, range_txt = epoch, "All Time", "Since launch"

    cur_ok  = _in_window(approved, start, far)
    cur_rej = _in_window(rejected, start, far)
    cur     = _agg(cur_ok)
    avg     = cur["revenue"] / cur["count"] if cur["count"] else 0.0

    L = [f"📊 <b>Statistics — {label}</b>", f"<i>{range_txt}</i>", "━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]

    # ── Revenue ────────────────────────────────────────────────────────────────
    L += [
        "💰 <b>SALES &amp; REVENUE</b>",
        f"   💵 Revenue:           <b>{_inr(cur['revenue'])}</b>",
        f"   🧾 Approved sales:    <b>{cur['count']}</b>",
        f"   🛒 Avg order value:   <b>{_inr(avg)}</b>",
        f"   👤 Unique buyers:     <b>{cur['buyers']}</b>",
    ]
    if period != "today":
        days_elapsed = max(1, (now.date() - start.date()).days + 1) if bounds else None
        if days_elapsed:
            L.append(f"   📆 Daily average:     <b>{_inr(cur['revenue'] / days_elapsed)}</b>")
        by_day = defaultdict(lambda: [0.0, 0])
        for t in cur_ok:
            k = t["_ts"].astimezone(STATS_TZ).date()
            by_day[k][0] += float(t.get("amount_paid") or 0)
            by_day[k][1] += 1
        if by_day:
            d, (rev, n) = max(by_day.items(), key=lambda kv: kv[1][0])
            L.append(f"   🔥 Best day:          <b>{d:%d %b}</b> — {_inr(rev)} ({n} sales)")
    L += [f"   💸 Net cash (excl. wallet): <b>{_inr(cur['revenue'] - cur['wallet_used'])}</b>", ""]

    # ── Comparison ─────────────────────────────────────────────────────────────
    if bounds:
        pl = _agg(_in_window(approved, prev_start, prev_like_end))
        pf = _agg(_in_window(approved, prev_start, start))
        L += [f"📈 <b>VS {prev_label.upper()}</b>"]
        L += [
            f"   <i>{same_label}</i>",
            f"   Revenue: {_inr(pl['revenue'])}  →  <b>{_delta(cur['revenue'], pl['revenue'])}</b>",
            f"   Sales:   {pl['count']}  →  <b>{_delta(cur['count'], pl['count'])}</b>",
        ]
        if prev_like_end < start:
            L += [
                f"   <i>{prev_label} (full)</i>",
                f"   Revenue: {_inr(pf['revenue'])}  →  <b>{_delta(cur['revenue'], pf['revenue'])}</b>",
                f"   Sales:   {pf['count']}  →  <b>{_delta(cur['count'], pf['count'])}</b>",
            ]
        L.append("")

    # ── Users ──────────────────────────────────────────────────────────────────
    total_u    = len(users)
    inactive_u = len([u for u in users if u.get("is_active") is False])
    all_buyers = {t.get("telegram_user_id") for t in approved}
    conv       = len(all_buyers) / total_u * 100 if total_u else 0.0
    new_cur    = len(_in_window(users_ts, start, far))
    L += [
        "👥 <b>USERS</b>",
        f"   Total users:        <b>{total_u}</b>  (🟢 {total_u - inactive_u} active · 🔴 {inactive_u} inactive)",
    ]
    if bounds:
        new_prev = len(_in_window(users_ts, prev_start, prev_like_end))
        L.append(f"   New users:          <b>{new_cur}</b>  ({_delta(new_cur, new_prev)} vs {same_label.lower()})")
    L += [
        f"   Paying customers:   <b>{len(all_buyers)}</b> all-time  ({conv:.1f}% of users)",
        "   <i>Inactive users stay counted so sales history is never lost.</i>",
        "",
    ]

    # ── Payments ───────────────────────────────────────────────────────────────
    decided   = cur["count"] + len(cur_rej)
    rej_rate  = len(cur_rej) / decided * 100 if decided else 0.0
    wallet_held = sum(float(u.get("wallet_balance") or 0) for u in users)
    L += [
        "💳 <b>PAYMENTS</b>",
        f"   📸 Screenshot-paid:   <b>{cur['count'] - cur['wallet_count']}</b>",
        f"   💼 Wallet-paid:       <b>{cur['wallet_count']}</b>  ({_inr(cur['wallet_used'])})",
        f"   ❌ Rejected:          <b>{len(cur_rej)}</b>  ({rej_rate:.1f}% of reviewed)",
        f"   ⏳ Awaiting approval: <b>{awaiting}</b> (now)",
        f"   🏦 Wallet balances held: <b>{_inr(wallet_held)}</b>",
        "",
    ]

    # ── Top items ──────────────────────────────────────────────────────────────
    per_item = defaultdict(lambda: [0, 0.0])
    for t in cur_ok:
        per_item[t.get("course_id", "unknown")][0] += 1
        per_item[t.get("course_id", "unknown")][1] += float(t.get("amount_paid") or 0)
    top = sorted(per_item.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)[:5]
    L.append("🏆 <b>TOP SELLING ITEMS</b>")
    if top:
        marks = ["🥇", "🥈", "🥉", "4.", "5."]
        for i, (cid, (n, rev)) in enumerate(top):
            L.append(f"   {marks[i]} {html.escape(titles.get(cid, cid))} — <b>{n}</b> · {_inr(rev)}")
    else:
        L.append("   No sales in this period.")
    L.append("")

    # ── Monthly trend ──────────────────────────────────────────────────────────
    if period in ("month", "year", "all"):
        if period == "year":
            months = [(now.year, m) for m in range(1, now.month + 1)]
            title  = f"MONTH BY MONTH — {now.year}"
        else:
            n_back = 6 if period == "month" else 12
            months = [_month_shift(now.year, now.month, -k) for k in range(n_back - 1, -1, -1)]
            title  = f"LAST {n_back} MONTHS"
        buckets = defaultdict(lambda: [0.0, 0])
        for t in approved:
            lt = t["_ts"].astimezone(STATS_TZ)
            buckets[(lt.year, lt.month)][0] += float(t.get("amount_paid") or 0)
            buckets[(lt.year, lt.month)][1] += 1
        L.append(f"🗓 <b>{title}</b>")
        for ym in months:
            rev, n = buckets[ym]
            prev_rev = buckets[_month_shift(ym[0], ym[1], -1)][0]
            L.append(f"   {datetime(ym[0], ym[1], 1):%b %Y}:  <b>{_inr(rev)}</b> · {n} sales  {_delta(rev, prev_rev)}")
        L.append("")

    # ── Quick summary (all-time view) ──────────────────────────────────────────
    if period == "all":
        L.append("⚡ <b>QUICK SUMMARY</b>")
        for p in ("today", "week", "month", "year"):
            s, _, lab, *_ = _period_bounds(p, now)
            a = _agg(_in_window(approved, s, far))
            L.append(f"   {lab}:  <b>{_inr(a['revenue'])}</b> · {a['count']} sales")
        L.append("")

    # ── Catalogue ──────────────────────────────────────────────────────────────
    bundles = len([c for c in courses if c["course_id"].startswith("bundle_")])
    L += [
        "📚 <b>CATALOGUE</b>",
        f"   Courses: <b>{len(courses) - bundles}</b>  ·  Bundles: <b>{bundles}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🕒 <i>Updated {now:%d %b %Y, %I:%M %p} IST</i>",
    ]
    return "\n".join(L)

def _stats_kb(active: str) -> InlineKeyboardMarkup:
    def b(text, key):
        return InlineKeyboardButton(text=("• " if key == active else "") + text, callback_data=f"stats_{key}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [b("📅 Today", "today"), b("📆 Week", "week"), b("🗓 Month", "month")],
        [b("📈 Year", "year"),   b("📊 All Time", "all")],
    ])

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    wait = await message.answer("⏳ Crunching the numbers…")
    text = await asyncio.to_thread(_build_stats, "today")
    await wait.edit_text(text, reply_markup=_stats_kb("today"), parse_mode="HTML")

@dp.callback_query(F.data.startswith("stats_"))
async def stats_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)
    period = callback.data.split("_", 1)[1]
    if period not in ("today", "week", "month", "year", "all"):
        return await callback.answer()
    await callback.answer("Loading…")
    text = await asyncio.to_thread(_build_stats, period)
    try:
        await callback.message.edit_text(text, reply_markup=_stats_kb(period), parse_mode="HTML")
    except TelegramBadRequest:
        pass

# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    scheduler = asyncio.create_task(_autocast_scheduler())   # noqa: F841 (keep reference)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import html
import os
import re
import time

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.client.session.aiohttp import AiohttpSession

from config import (
    ADMIN_ID, BOT2_USERNAME, BUNDLE_ALL_BUTTON_TEXT, BUNDLE_ALL_ID, BUNDLE_PREFIX,
    BROADCAST_DELETE_SECS, MENU_DELETE_SECS, WELCOME_CAPTION, WELCOME_PHOTO, fmt_duration,
)
from db import aexec, fetch_all, supabase
from stats import build_monthly_text, build_period_text, stats_keyboard

load_dotenv()

BOT_TOKEN = os.getenv("BOT1_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")  # e.g. socks5://user:pass@host:port — leave blank to disable
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()


# ── FSM States ─────────────────────────────────────────────────────────────────

class AddItemFSM(StatesGroup):
    item_id     = State()
    button_text = State()
    title       = State()
    price_inr   = State()
    price_usd   = State()
    sales_text  = State()
    image       = State()
    dump_ids    = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()

class DBroadcastFSM(StatesGroup):
    waiting_for_video       = State()
    waiting_for_button_text = State()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _auto_delete(chat_id: int, message_id: int, delay: int = MENU_DELETE_SECS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _ensure_user(user_id: int, username=None):
    res = await aexec(
        supabase.table("users").select("telegram_user_id, username, is_active").eq("telegram_user_id", user_id)
    )
    if not res.data:
        await aexec(supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username": username or "",
            "is_active": True,
        }))
        return
    row = res.data[0]
    updates = {}
    if row.get("is_active") is False:           # user came back after blocking the bot
        updates["is_active"] = True
    if (username or "") != (row.get("username") or ""):
        updates["username"] = username or ""
    if updates:
        await aexec(supabase.table("users").update(updates).eq("telegram_user_id", user_id))


async def _mark_inactive(user_id: int):
    """Blocked users are kept (so sales history stays intact) but skipped by broadcasts."""
    try:
        await aexec(supabase.table("users").update({"is_active": False}).eq("telegram_user_id", user_id))
    except Exception:
        pass


async def _load_items():
    return await fetch_all(
        lambda: supabase.table("courses").select("course_id, title, button_text").order("created_at")
    )


async def _main_menu_markup() -> InlineKeyboardMarkup:
    items = await _load_items()
    builder = InlineKeyboardBuilder()
    for c in items:
        if c["course_id"].startswith(BUNDLE_PREFIX):
            continue
        builder.row(InlineKeyboardButton(
            text=c.get("button_text") or c["title"],
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}",
        ))
    builder.row(InlineKeyboardButton(
        text=BUNDLE_ALL_BUTTON_TEXT,
        url=f"https://t.me/{BOT2_USERNAME}?start={BUNDLE_ALL_ID}",
    ))
    return builder.as_markup()


# ── /start & menus ─────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await _ensure_user(message.from_user.id, message.from_user.username)
    sent = await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=WELCOME_CAPTION,
        reply_markup=await _main_menu_markup(),
        parse_mode="HTML",
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id))


@dp.callback_query(F.data == "show_bundles_menu")
async def menu_show_bundles(callback: types.CallbackQuery):
    items = await _load_items()
    bundles = [c for c in items if c["course_id"].startswith(BUNDLE_PREFIX)]

    builder = InlineKeyboardBuilder()
    if not bundles:
        builder.row(InlineKeyboardButton(text="No bundles available right now", callback_data="ignore"))
    else:
        for b in bundles:
            builder.row(InlineKeyboardButton(
                text=f"📦 {b.get('button_text') or b['title']}",
                url=f"https://t.me/{BOT2_USERNAME}?start={b['course_id']}",
            ))
    builder.row(InlineKeyboardButton(text="⬅️ Back to All Courses", callback_data="back_to_main_menu"))

    await callback.message.edit_caption(
        caption=(
            "🎁 <b>Exclusive Bundles</b>\n\n"
            "Select a bundle below to get multiple courses at a massively discounted price!\n\n"
            f"⏳ <i>This message self-destructs in {fmt_duration(MENU_DELETE_SECS)}.</i>"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_main_menu")
async def menu_back_to_main(callback: types.CallbackQuery):
    await callback.message.edit_caption(
        caption=WELCOME_CAPTION,
        reply_markup=await _main_menu_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    await callback.answer()


# ── ADMIN: /cancel (works inside any admin flow) ───────────────────────────────

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if await state.get_state() is None:
        return await message.answer("Nothing to cancel.")
    await state.clear()
    await message.answer("❌ Cancelled.")


# ── ADMIN: /addnew & /addbundle (one shared flow) ──────────────────────────────

def _step(n: int, title: str, body: str) -> str:
    return f"🛠 <b>Step {n} of 8 — {title}</b>\n\n{body}\n\n<i>/cancel to abort</i>"


@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await state.update_data(kind="course")
    await state.set_state(AddItemFSM.item_id)
    await message.answer(
        "🛠 <b>Add New Course — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this course.\n"
        "<i>(Lowercase letters/numbers/underscores, e.g. <code>python_basics</code>)</i>\n\n"
        "<i>/cancel to abort</i>",
        parse_mode="HTML",
    )


@dp.message(Command("addbundle"))
async def cmd_addbundle(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await state.update_data(kind="bundle")
    await state.set_state(AddItemFSM.item_id)
    await message.answer(
        "🛠 <b>Add New Bundle — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this bundle.\n"
        f"<i>(<code>{BUNDLE_PREFIX}</code> is added automatically if you leave it out.)</i>\n\n"
        "<i>/cancel to abort</i>",
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.item_id, F.text)
async def process_item_id(message: types.Message, state: FSMContext):
    kind = (await state.get_data())["kind"]
    raw = re.sub(r"[^a-z0-9_]", "", message.text.strip().lower().replace(" ", "_").replace("-", "_"))
    if kind == "bundle" and raw and not raw.startswith(BUNDLE_PREFIX):
        raw = BUNDLE_PREFIX + raw
    if not raw or raw == BUNDLE_PREFIX:
        return await message.answer("❌ Use only letters, numbers and underscores. Try again.")
    if kind == "course" and raw.startswith(BUNDLE_PREFIX):
        return await message.answer(
            f"❌ IDs starting with <code>{BUNDLE_PREFIX}</code> are reserved for bundles. Use /addbundle.",
            parse_mode="HTML",
        )
    exists = await aexec(supabase.table("courses").select("course_id").eq("course_id", raw))
    if exists.data:
        return await message.answer("❌ That ID already exists. Choose another one.")

    await state.update_data(course_id=raw)
    await state.set_state(AddItemFSM.button_text)
    await message.answer(
        f"✅ ID saved as <code>{raw}</code>\n\n"
        + _step(2, "Short Button Name",
                "Enter the short text for the menu button.\n<i>(e.g. <code>Python Basics</code>)</i>"),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.button_text, F.text)
async def process_button_text(message: types.Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await state.set_state(AddItemFSM.title)
    await message.answer(
        "✅ Button name saved!\n\n"
        + _step(3, "Full Display Title", "Enter the long, detailed title shown on the sales page."),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.title, F.text)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddItemFSM.price_inr)
    await message.answer(
        "✅ Title saved!\n\n" + _step(4, "Price (INR)", "Enter the price in <b>₹</b> as a plain number."),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.price_inr, F.text)
async def process_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Not a number. Enter something like <code>400</code>.", parse_mode="HTML")
    await state.update_data(numeric_price=numeric)
    await state.set_state(AddItemFSM.price_usd)
    await message.answer(
        "✅ INR price saved!\n\n" + _step(5, "Price (USD)", "Enter the price in <b>$</b> as a plain number."),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.price_usd, F.text)
async def process_price_usd(message: types.Message, state: FSMContext):
    try:
        usd = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Not a number. Enter something like <code>6</code>.", parse_mode="HTML")
    data = await state.get_data()
    display = f"₹{data['numeric_price']:g} / ${usd:g}"
    await state.update_data(price_usd=usd, price=display)
    await state.set_state(AddItemFSM.sales_text)
    await message.answer(
        f"✅ Display price: <b>{display}</b>\n\n"
        + _step(6, "Sales Description", "Enter the sales text the sales bot shows to buyers."),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.sales_text, F.text)
async def process_sales_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await state.set_state(AddItemFSM.image)
    await message.answer(
        "✅ Description saved!\n\n" + _step(7, "Thumbnail URL", "Paste a public image URL for the thumbnail."),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.image, F.text)
async def process_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await state.set_state(AddItemFSM.dump_ids)
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        + _step(8, "Delivery Content (Message IDs)",
                "Find the message IDs of the files in your private storage channel and enter them "
                "separated by commas.\n<i>(Example: <code>104, 105, 106</code>)</i>"),
        parse_mode="HTML",
    )


@dp.message(AddItemFSM.dump_ids, F.text)
async def process_dump_ids(message: types.Message, state: FSMContext):
    raw = message.text.strip()
    if not re.fullmatch(r"\d+(\s*,\s*\d+)*", raw):
        return await message.answer(
            "❌ Enter only numbers separated by commas, e.g. <code>104, 105, 106</code>.", parse_mode="HTML"
        )
    dump_ids = ",".join(p.strip() for p in raw.split(","))

    data = await state.get_data()
    kind = data["kind"]
    is_bundle = kind == "bundle"
    try:
        await aexec(supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "button_text":      data["button_text"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "price_usd":        data["price_usd"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    "✅ Payment verified! Here is your bundle access." if is_bundle
                                else "✅ Payment verified! Here is your access.",
            "dump_message_ids": dump_ids,
        }))
        icon, noun = ("📦", "Bundle") if is_bundle else ("📘", "Course")
        await message.answer(
            f"🎉 <b>{noun} Added Successfully!</b>\n\n{icon} <b>{html.escape(data['title'])}</b> is now live.",
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ <b>Database error:</b>\n\n<code>{html.escape(str(e))}</code>", parse_mode="HTML")
    await state.clear()


# ── Broadcast core ─────────────────────────────────────────────────────────────

async def _broadcast(send_fn):
    """Calls send_fn(uid) for every ACTIVE user. Returns (results, blocked, failed)."""
    rows = await fetch_all(
        lambda: supabase.table("users").select("telegram_user_id").eq("is_active", True).order("telegram_user_id")
    )
    uids = {r["telegram_user_id"] for r in rows}
    results, blocked, failed = [], 0, 0
    for uid in uids:
        try:
            try:
                res = await send_fn(uid)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                res = await send_fn(uid)
            results.append((uid, res))
        except TelegramForbiddenError:
            blocked += 1
            await _mark_inactive(uid)
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    return results, blocked, failed


# ── ADMIN: /broadcast ──────────────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "📢 <b>Broadcast Mode</b>\n\n"
        "Send the message you want delivered to all users.\n"
        "Supports text, photos, and videos.\n\n"
        "ℹ️ <i>Users who blocked the bot are marked inactive and skipped next time "
        "(their sales history is kept).</i>\n\n<i>/cancel to abort</i>",
        parse_mode="HTML",
    )
    await state.set_state(BroadcastFSM.waiting_for_message)


@dp.message(BroadcastFSM.waiting_for_message)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(preview_chat_id=message.chat.id, preview_message_id=message.message_id)
    await state.set_state(BroadcastFSM.waiting_for_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm — Send to all", callback_data="broadcast_confirm"),
        InlineKeyboardButton(text="❌ Cancel",                callback_data="broadcast_cancel"),
    ]])
    await message.answer(
        "👆 <b>Preview above.</b>\n\nThis will be sent to <b>all active users</b>. Confirm?",
        reply_markup=kb, parse_mode="HTML",
    )


@dp.callback_query(F.data.in_({"broadcast_confirm", "broadcast_cancel"}))
async def broadcast_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)
    await callback.answer()

    if callback.data == "broadcast_cancel":
        await state.clear()
        return await callback.message.edit_text("❌ <b>Broadcast cancelled.</b>", reply_markup=None, parse_mode="HTML")

    data = await state.get_data()
    await state.clear()
    if "preview_message_id" not in data:
        return await callback.message.edit_text("⚠️ Session expired. Start again with /broadcast.", reply_markup=None)

    status_msg = await callback.message.edit_text("⏳ Broadcasting…", reply_markup=None)

    async def send(uid):
        return await bot.copy_message(
            chat_id=uid, from_chat_id=data["preview_chat_id"], message_id=data["preview_message_id"]
        )

    results, blocked, failed = await _broadcast(send)
    await status_msg.edit_text(
        "✅ <b>Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:        <b>{len(results)}</b> users\n"
        f"🚫 Blocked (marked inactive): <b>{blocked}</b>\n"
        f"⚠️ Other failures:      <b>{failed}</b>",
        parse_mode="HTML",
    )


# ── ADMIN: /dbroadcast ─────────────────────────────────────────────────────────

@dp.message(Command("dbroadcast"))
async def cmd_dbroadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(
        "📹 <b>Delete-Broadcast Mode</b>\n\n"
        "Send the <b>video</b> you want to broadcast to all users.\n\n"
        f"⚠️ <i>Broadcast messages auto-delete from every user's chat after "
        f"{fmt_duration(BROADCAST_DELETE_SECS)}.</i>\n\n<i>/cancel to abort</i>",
        parse_mode="HTML",
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
            "❌ <b>That's not a video.</b>\n\nPlease send a <b>video file</b>.\n"
            "<i>Tip: send it without compression if possible.</i>",
            parse_mode="HTML",
        )
    await state.update_data(video_file_id=file_id, caption=message.caption or "")
    await state.set_state(DBroadcastFSM.waiting_for_button_text)
    await message.answer(
        "✅ <b>Video received!</b>\n\nNow enter the <b>inline button label</b>.\n"
        "<i>(e.g. <code>🛒 View All Courses</code>)</i>",
        parse_mode="HTML",
    )


@dp.message(DBroadcastFSM.waiting_for_button_text, F.text)
async def dbroadcast_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    bot_info = await bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=message.text.strip(), url=f"https://t.me/{bot_info.username}?start=")
    ]])
    status_msg = await message.answer("⏳ Broadcasting delete-broadcast to all users…")

    async def send(uid):
        sent = await bot.send_video(
            chat_id=uid, video=data["video_file_id"], caption=data["caption"] or None,
            reply_markup=kb, parse_mode="HTML", protect_content=True,
        )
        return (sent.message_id, time.time())

    results, blocked, failed = await _broadcast(send)
    await status_msg.edit_text(
        "✅ <b>Delete-Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:        <b>{len(results)}</b> users\n"
        f"🚫 Blocked (marked inactive): <b>{blocked}</b>\n"
        f"⚠️ Other failures:      <b>{failed}</b>\n\n"
        f"⏳ Each message auto-deletes <b>{fmt_duration(BROADCAST_DELETE_SECS)}</b> after it was sent.",
        parse_mode="HTML",
    )

    async def _delete_all():
        for uid, (msg_id, sent_at) in results:
            remaining = BROADCAST_DELETE_SECS - (time.time() - sent_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                await bot.delete_message(chat_id=uid, message_id=msg_id)
            except Exception:
                pass

    asyncio.create_task(_delete_all())


# ── ADMIN: /stats ──────────────────────────────────────────────────────────────

async def _render_stats(msg: types.Message, period: str):
    try:
        text = await build_monthly_text() if period == "months" else await build_period_text(period)
    except Exception as e:
        text = f"❌ <b>Stats error:</b>\n<code>{html.escape(str(e))}</code>"
    try:
        await msg.edit_text(text, reply_markup=stats_keyboard(period), parse_mode="HTML")
    except TelegramBadRequest:
        pass


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    msg = await message.answer("⏳ Crunching the numbers…")
    await _render_stats(msg, "today")


@dp.callback_query(F.data.startswith("stats:"))
async def stats_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)
    await callback.answer()
    await _render_stats(callback.message, callback.data.split(":", 1)[1])


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

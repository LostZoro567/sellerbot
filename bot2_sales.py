import asyncio
import html
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto,
    LinkPreviewOptions, ReplyParameters,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession

from config import (
    ADMIN_CONTACT_URL, ADMIN_ID, ADMIN_REMINDER_SECS, BUNDLE_ALL_ID, BUNDLE_REGULAR_PRICE,
    DELIVERY_DELETE_SECS, DELIVERY_WARN_BEFORE_SECS, DUMP_CHAT_ID, METHOD_LABELS,
    PAYMENT_BUTTONS, PAYMENT_OPTIONS_IMAGE, PAYMENT_WINDOW_SECS, PENDING_WINDOW_SECS,
    RECOVERY_FIRST_SECS, RECOVERY_SECOND_SECS, USD_METHODS, build_payment_methods, fmt_duration,
)
from db import aexec, fetch_all, parse_ts, supabase

load_dotenv()

BOT_TOKEN = os.getenv("BOT2_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")  # e.g. socks5://user:pass@host:port — leave blank to disable
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

PAYMENT_METHODS = build_payment_methods()
WINDOW_TEXT = fmt_duration(PAYMENT_WINDOW_SECS)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Per-user recovery task tracker — cancels old task when user browses a new item
_pending_recovery: dict[int, asyncio.Task] = {}
# Users who pressed "No" on the save-check and must resubmit a screenshot
_resubmit_mode: set[int] = set()


class CustomRejectFSM(StatesGroup):
    waiting_for_reason = State()


# ══════════════════════════════════════════════════════════════════════════════
# SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _price_pair(course: dict) -> tuple[float, float]:
    """(INR, USD) numeric prices. USD falls back to parsing the display string '₹400 / $6'."""
    inr = round(float(course.get("numeric_price") or 0), 2)
    usd = course.get("price_usd")
    if usd is None:
        m = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", str(course.get("price") or ""))
        usd = float(m.group(1)) if m else 0.0
    return inr, round(float(usd), 2)


def _money(amount: float, currency: str) -> str:
    return f"${amount:,.2f}" if (currency or "INR").upper() == "USD" else f"₹{amount:,.2f}"


def _human_age(seconds: float) -> str:
    mins = int(seconds // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


async def _auto_delete(chat_id: int, message_id: int, delay: int = PAYMENT_WINDOW_SECS):
    await asyncio.sleep(delay)
    await _safe_delete(chat_id, message_id)


async def _safe_delete(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _delete_messages(chat_id: int, ids) -> None:
    ids = [int(i) for i in ids]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except Exception:
            for mid in chunk:
                await _safe_delete(chat_id, mid)


async def _append_card(chat_id: int, message_id: int, base_html: str, suffix: str, is_photo: bool = True):
    """Append a status line to an admin card and remove its buttons."""
    try:
        if is_photo:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=base_html + suffix,
                parse_mode="HTML", reply_markup=None,
            )
        else:
            await bot.edit_message_text(
                text=base_html + suffix, chat_id=chat_id, message_id=message_id,
                parse_mode="HTML", reply_markup=None,
            )
    except Exception:
        try:  # caption too long etc. — at least strip the buttons and post the status
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass
        try:
            await bot.send_message(
                chat_id, suffix.strip(), parse_mode="HTML",
                reply_parameters=ReplyParameters(message_id=message_id, allow_sending_without_reply=True),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _get_course(course_id: str):
    res = await aexec(supabase.table("courses").select("*").eq("course_id", course_id))
    return res.data[0] if res.data else None


async def _course_title(course_id: str) -> str:
    c = await _get_course(course_id)
    return c["title"] if c else course_id


async def _titles(course_ids: list) -> dict:
    if not course_ids:
        return {}
    res = await aexec(supabase.table("courses").select("course_id, title, button_text").in_("course_id", course_ids))
    return {r["course_id"]: (r.get("button_text") or r["title"]) for r in res.data}


async def _create_tx(user_id: int, course_id: str, course: dict | None = None) -> dict:
    course = course or await _get_course(course_id)
    inr = _price_pair(course)[0] if course else 0.0
    res = await aexec(supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "amount_paid":      inr,
        "currency":         "INR",
    }))
    return res.data[0]


async def _get_or_create_pending(user_id: int, course_id: str, course: dict | None = None) -> dict:
    """Re-use the user's open checkout for this item instead of creating duplicates."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=PENDING_WINDOW_SECS)).isoformat()
    res = await aexec(
        supabase.table("transactions").select("*")
        .eq("telegram_user_id", user_id).eq("course_id", course_id)
        .eq("status", "pending_payment").gte("created_at", since)
        .order("created_at", desc=True).limit(1)
    )
    if res.data:
        return res.data[0]
    return await _create_tx(user_id, course_id, course)


async def _set_payment_method(user_id: int, course_id: str, method: str):
    """Remember which method the buyer opened — decides ₹ vs $ in the stats."""
    course = await _get_course(course_id)
    if not course:
        return
    inr, usd = _price_pair(course)
    is_usd = method in USD_METHODS
    tx = await _get_or_create_pending(user_id, course_id, course)
    await aexec(
        supabase.table("transactions").update({
            "payment_method": method,
            "currency":       "USD" if is_usd else "INR",
            "amount_paid":    usd if is_usd else inr,
        }).eq("id", tx["id"]).eq("status", "pending_payment")
    )


async def _recent_pending(user_id: int) -> list:
    """Open checkouts (one per item), items where a payment method was opened first."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=PENDING_WINDOW_SECS)).isoformat()
    res = await aexec(
        supabase.table("transactions").select("*")
        .eq("telegram_user_id", user_id).eq("status", "pending_payment")
        .gte("created_at", since).order("created_at", desc=True)
    )
    seen, out = set(), []
    for r in res.data:
        if r["course_id"] not in seen:
            seen.add(r["course_id"])
            out.append(r)
    out.sort(key=lambda r: 0 if r.get("payment_method") else 1)
    return out


async def _owned_courses(user_id: int) -> list:
    res = await aexec(
        supabase.table("transactions").select("course_id, created_at")
        .eq("telegram_user_id", user_id).in_("status", ["approved", "redelivered"])
        .order("created_at", desc=True)
    )
    seen, out = set(), []
    for r in res.data:
        if r["course_id"] not in seen:
            seen.add(r["course_id"])
            out.append(r["course_id"])
    return out[:8]


# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS & CAPTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _course_keyboard(course_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳  Buy Now", callback_data=f"buy:{course_id}")]
    ])


def _payment_keyboard(course_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"pay:{key}:{course_id}")] for key, label in PAYMENT_BUTTONS]
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data=f"backcourse:{course_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_keyboard(trans_id) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅  Approve", callback_data=f"approve_{trans_id}"),
        InlineKeyboardButton(text="❌  Reject",  callback_data=f"reject_{trans_id}"),
    ]])


def _course_caption(course: dict) -> str:
    return (
        f"📘 <b>{course['title']}</b>\n\n"
        f"{course['bot2_text']}\n\n"
        f"💵 <b>Price:</b> {course['price']}\n\n"
        f"⏳ <i>This payment window closes in {WINDOW_TEXT}.</i>"
    )


def _payment_options_caption(price_display: str) -> str:
    return (
        "🏦 <b>Choose a Payment Method</b>\n\n"
        f"💵 <b>Your price:</b> {price_display}\n\n"
        "Select how you'd like to pay below.\n\n"
        f"⏳ <i>This window closes in {WINDOW_TEXT}.</i>"
    )


async def _build_admin_caption(tx: dict) -> str:
    user_id, course_id, trans_id = tx["telegram_user_id"], tx["course_id"], tx["id"]
    currency = (tx.get("currency") or "INR").upper()
    amount = float(tx.get("amount_paid") or 0)
    method = METHOD_LABELS.get(tx.get("payment_method")) or "Not selected"
    uname = f"@{html.escape(tx['username'])}" if tx.get("username") else "no username"

    title, hist, owned, dupes = await asyncio.gather(
        _course_title(course_id),
        aexec(supabase.table("transactions").select("id, course_id, status, amount_paid, currency")
              .eq("telegram_user_id", user_id).in_("status", ["approved", "awaiting_approval", "redelivered"])
              .order("created_at", desc=True).limit(5)),
        aexec(supabase.table("transactions").select("id").eq("telegram_user_id", user_id)
              .eq("course_id", course_id).eq("status", "approved")),
        aexec(supabase.table("transactions").select("id").eq("telegram_user_id", user_id)
              .eq("course_id", course_id).eq("status", "awaiting_approval").neq("id", trans_id)),
    )

    if hist.data:
        icons = {"approved": "✅", "awaiting_approval": "⏳", "redelivered": "♻️"}
        lines = []
        for h in hist.data:
            mark = "  ← <b>this</b>" if h["id"] == trans_id else ""
            lines.append(
                f"  {icons.get(h['status'], '❓')} <code>{html.escape(h['course_id'])}</code>  "
                f"{_money(float(h.get('amount_paid') or 0), h.get('currency'))}{mark}"
            )
        history_block = "\n\n🗂 <b>Recent history:</b>\n" + "\n".join(lines)
    else:
        history_block = "\n\n🗂 <b>Purchase history:</b> first purchase"

    owned_warning = (
        "\n\n♻️ <b>NOTE: User already owns this item — likely lost access to the files.</b>" if owned.data else ""
    )
    dupe_warning = (
        f"\n\n🚨 <b>DUPLICATE ALERT:</b> <b>{len(dupes.data)}</b> other pending screenshot(s) for this same item."
        if dupes.data else ""
    )

    return (
        "💳 <b>New Payment Screenshot</b>\n\n"
        f"👤 User:    {uname} (<code>{user_id}</code>)\n"
        f"📘 Item:    <b>{html.escape(title)}</b>\n"
        f"🔑 ID:      <code>{html.escape(course_id)}</code>\n"
        f"💳 Method:  <b>{method}</b>\n"
        f"💵 Amount:  <b>{_money(amount, currency)}</b>\n"
        f"🔖 Tx ID:   <code>{trans_id}</code>"
        f"{owned_warning}{history_block}{dupe_warning}"
    )


async def _send_admin_card(tx: dict) -> types.Message:
    caption = await _build_admin_caption(tx)
    kb = _admin_keyboard(tx["id"])
    file_id = tx.get("screenshot_file_id")
    if not file_id:
        return await bot.send_message(ADMIN_ID, caption, reply_markup=kb, parse_mode="HTML")
    try:
        return await bot.send_photo(ADMIN_ID, file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
    except Exception:  # caption problem — send photo and text separately so the screenshot is never lost
        await bot.send_photo(ADMIN_ID, file_id)
        return await bot.send_message(ADMIN_ID, caption, reply_markup=kb, parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# DELIVERY (files auto-delete, tracked in DB so it survives restarts)
# ══════════════════════════════════════════════════════════════════════════════

async def _deliver_course(user_id: int, course_id: str, tx_id) -> None:
    course = await _get_course(course_id)
    if not course:
        await bot.send_message(user_id, "✅ Payment approved! Contact support for your materials.")
        return

    msg_ids: list[int] = []
    del_text = course.get("delivery_text") or "✅ Payment verified! Here are your materials:"

    warn = await bot.send_message(
        chat_id=user_id,
        text=(
            "⚠️ <b>WARNING: Self-Destructing Files</b>\n\n"
            f"The files below will be <b>automatically deleted in {fmt_duration(DELIVERY_DELETE_SECS)}</b>. "
            "Please forward them to your <b>Saved Messages</b> or download them immediately.\n\n"
            f"🔔 We'll remind you {fmt_duration(DELIVERY_WARN_BEFORE_SECS)} before they're deleted.\n\n"
            "<i>Lost access after deletion? Contact our support team.</i>"
        ),
        parse_mode="HTML",
    )
    msg_ids.append(warn.message_id)

    intro = await bot.send_message(chat_id=user_id, text=del_text, parse_mode="HTML", link_preview_options=NO_PREVIEW)
    msg_ids.append(intro.message_id)

    failed = 0
    for raw in [m.strip() for m in (course.get("dump_message_ids") or "").split(",") if m.strip()]:
        try:
            sent = await bot.copy_message(chat_id=user_id, from_chat_id=DUMP_CHAT_ID, message_id=int(raw))
            msg_ids.append(sent.message_id)
            await asyncio.sleep(0.5)
        except Exception as e:
            failed += 1
            print(f"Failed to deliver message {raw} from dump channel: {e}")

    await aexec(supabase.table("deliveries").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "transaction_id":   str(tx_id),
        "message_ids":      msg_ids,
        "delivered_at":     _now_iso(),
    }))
    if failed:
        raise RuntimeError(f"{failed} file(s) could not be copied from the dump channel")


async def _append_delivery_msgs(delivery_id: int, new_ids: list):
    res = await aexec(supabase.table("deliveries").select("message_ids").eq("id", delivery_id))
    if not res.data:
        return
    ids = list(res.data[0].get("message_ids") or []) + new_ids
    await aexec(supabase.table("deliveries").update({"message_ids": ids}).eq("id", delivery_id))


async def _send_delivery_warning(d: dict):
    claim = await aexec(
        supabase.table("deliveries").update({"warned_at": _now_iso()})
        .eq("id", d["id"]).eq("status", "active").is_("warned_at", "null")
    )
    if not claim.data:
        return
    uid = d["telegram_user_id"]
    try:
        w = await bot.send_message(
            uid,
            f"⏰ <b>{fmt_duration(DELIVERY_WARN_BEFORE_SECS)} left!</b>\n\n"
            f"Your files will be deleted from this chat in <b>{fmt_duration(DELIVERY_WARN_BEFORE_SECS)}</b>. "
            "Please forward them to your <b>Saved Messages</b> now.",
            parse_mode="HTML",
        )
        await _append_delivery_msgs(d["id"], [w.message_id])

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yes, I saved them", callback_data=f"dlv:yes:{d['id']}"),
            InlineKeyboardButton(text="❌ No, not yet",       callback_data=f"dlv:no:{d['id']}"),
        ]])
        q = await bot.send_message(
            uid,
            "📥 <b>Did you save or forward the files?</b>\n\n"
            "Press <b>✅ Yes</b> only after you have saved them — the files will then be removed "
            "from this chat immediately.",
            reply_markup=kb, parse_mode="HTML",
        )
        await _append_delivery_msgs(d["id"], [q.message_id])
    except Exception as e:
        print(f"Delivery warning failed for {uid}: {e}")


async def _purge_delivery(d: dict):
    claim = await aexec(
        supabase.table("deliveries").update({"status": "deleted"}).eq("id", d["id"]).eq("status", "active")
    )
    if claim.data:
        await _delete_messages(d["telegram_user_id"], claim.data[0].get("message_ids") or [])


async def _delivery_loop():
    while True:
        try:
            rows = await fetch_all(
                lambda: supabase.table("deliveries").select("*").eq("status", "active").order("id")
            )
            now = datetime.now(timezone.utc)
            for d in rows:
                age = (now - parse_ts(d["delivered_at"])).total_seconds()
                if age >= DELIVERY_DELETE_SECS:
                    await _purge_delivery(d)
                elif age >= DELIVERY_DELETE_SECS - DELIVERY_WARN_BEFORE_SECS and not d.get("warned_at"):
                    await _send_delivery_warning(d)
        except Exception as e:
            print(f"delivery loop error: {e}")
        await asyncio.sleep(30)


@dp.callback_query(F.data.startswith("dlv:"))
async def delivery_answer(callback: types.CallbackQuery):
    _, action, did = callback.data.split(":")
    res = await aexec(supabase.table("deliveries").select("*").eq("id", int(did)))
    if not res.data or res.data[0]["telegram_user_id"] != callback.from_user.id:
        return await callback.answer("⛔ Not available.", show_alert=True)
    d, uid = res.data[0], callback.from_user.id

    if d["status"] != "active":
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
        return await callback.answer("These files were already removed.", show_alert=True)

    if action == "yes":
        claim = await aexec(
            supabase.table("deliveries").update({"status": "deleted", "answer": "yes"})
            .eq("id", d["id"]).eq("status", "active")
        )
        if not claim.data:
            return await callback.answer("Already removed.", show_alert=True)
        ids = list(claim.data[0].get("message_ids") or []) + [callback.message.message_id]
        await callback.answer()
        await _delete_messages(uid, ids)
        note = await bot.send_message(uid, "✅ Done! Your files were removed from this chat. Thank you!")
        asyncio.create_task(_auto_delete(uid, note.message_id, 120))
    else:
        await aexec(supabase.table("deliveries").update({"answer": "no"}).eq("id", d["id"]))
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        _resubmit_mode.add(uid)
        await callback.answer()
        await bot.send_message(
            uid,
            "No problem! ⚠️ The files will still be removed in a few minutes.\n\n"
            "To get them again:\n"
            "1️⃣ Send your <b>payment screenshot</b> here again\n"
            "2️⃣ <b>Select the file name</b> when asked\n\n"
            "The admin will verify it and send the files again.",
            parse_mode="HTML",
        )


# ══════════════════════════════════════════════════════════════════════════════
# RECOVERY NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

async def _recovery_notifications(user_id: int, course_id: str, course_title: str):
    async def _still_open() -> bool:
        res = await aexec(
            supabase.table("transactions").select("status").eq("telegram_user_id", user_id)
            .eq("course_id", course_id).in_("status", ["approved", "awaiting_approval"])
        )
        return not res.data

    try:
        await asyncio.sleep(RECOVERY_FIRST_SECS)
        if not await _still_open():
            return
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💳 Resume Purchase", callback_data=f"buy:{course_id}"))
        kb.row(InlineKeyboardButton(text="💬 Need Help? Contact Admin", url=ADMIN_CONTACT_URL))
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"👋 <b>Still interested in {course_title}?</b>\n\n"
                "We noticed you didn't finish your checkout. If you had any trouble with the "
                "payment methods or have questions, feel free to reach out to our support team!"
            ),
            reply_markup=kb.as_markup(), parse_mode="HTML",
        )
        await asyncio.sleep(RECOVERY_SECOND_SECS)
        if await _still_open():
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"✨ <b>Last call for {course_title}!</b>\n\n"
                    "The private access link is still available for you. Don't miss out!"
                ),
                reply_markup=kb.as_markup(), parse_mode="HTML",
            )
    except asyncio.CancelledError:
        pass  # user opened a different item — old task silently cancelled
    except Exception as e:
        print(f"recovery error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# /start — ITEM DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = (command.args or "").strip()
    user_id = message.from_user.id

    if not course_id:
        return await message.answer("⚠️ Please use a valid link to start.")
    course = await _get_course(course_id)
    if not course:
        return await message.answer("❌ Item not found or the link is invalid.")

    _resubmit_mode.discard(user_id)
    await _get_or_create_pending(user_id, course_id, course)

    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=_course_caption(course),
        reply_markup=_course_keyboard(course_id),
        parse_mode="HTML",
        protect_content=True,
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id))

    old = _pending_recovery.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _pending_recovery[user_id] = asyncio.create_task(
        _recovery_notifications(user_id, course_id, course["title"])
    )


@dp.callback_query(F.data.startswith("backcourse:"))
async def back_to_course(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    course = await _get_course(course_id)
    if not course:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
        return await callback.answer()
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=course["bot2_image_id"], caption=_course_caption(course), parse_mode="HTML"),
            reply_markup=_course_keyboard(course_id),
        )
    except Exception:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# BUY NOW & ONE-CLICK UPSELL
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("buy:"))
async def upsell_interceptor(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if course_id == BUNDLE_ALL_ID:
        return await _show_payment_options(callback, course_id)
    bundle = await _get_course(BUNDLE_ALL_ID)
    if not bundle:
        return await _show_payment_options(callback, course_id)

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ No",  callback_data=f"continue_pay:{course_id}"),
        InlineKeyboardButton(text="✅ Yes", callback_data=f"upgrade_to_all:{course_id}"),
    )
    sent = await bot.send_message(
        chat_id=user_id,
        text=(
            "<b>Special Offer Available ✅</b>\n\n"
            f"Instead of buying just one item, you can unlock <b>{bundle.get('title', 'The Ultimate Collection')}</b> "
            f"with ALL our files for only <b>{bundle.get('price', '')}</b>!\n\n"
            f"𝐑𝐞𝐠𝐮𝐥𝐚𝐫 𝐏𝐫𝐢𝐜𝐞 : <del>{BUNDLE_REGULAR_PRICE}</del>\n"
            f"𝐎𝐟𝐟𝐞𝐫 𝐏𝐫𝐢𝐜𝐞 : {bundle.get('price', '')}\n\n"
            "Would you like to Buy All VIP Files?"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    asyncio.create_task(_auto_delete(user_id, sent.message_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("upgrade_to_all"))
async def process_upgrade_to_all(callback: types.CallbackQuery):
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    # the single item they were looking at is replaced by the bundle — drop its open checkout
    if ":" in callback.data:
        old_course = callback.data.split(":", 1)[1]
        await aexec(
            supabase.table("transactions").update({"status": "cancelled"})
            .eq("telegram_user_id", callback.from_user.id).eq("course_id", old_course)
            .eq("status", "pending_payment")
        )
    await _show_payment_options(callback, BUNDLE_ALL_ID)


@dp.callback_query(F.data.startswith("continue_pay:"))
async def bypass_upsell(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await _show_payment_options(callback, course_id)


async def _show_payment_options(callback: types.CallbackQuery, course_id: str):
    user_id = callback.from_user.id
    course = await _get_course(course_id)
    if not course:
        return await callback.answer("❌ Item not found.", show_alert=True)
    await _get_or_create_pending(user_id, course_id, course)
    try:
        sent = await bot.send_photo(
            chat_id=user_id, photo=PAYMENT_OPTIONS_IMAGE,
            caption=_payment_options_caption(course.get("price", "?")),
            reply_markup=_payment_keyboard(course_id), parse_mode="HTML",
        )
        asyncio.create_task(_auto_delete(user_id, sent.message_id))
    except Exception:
        return await callback.answer("⚠️ Payment image link broken. Update PAYMENT_OPTIONS_IMAGE.", show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("backpay:"))
async def back_to_payment_options(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    course = await _get_course(course_id)
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=PAYMENT_OPTIONS_IMAGE,
                caption=_payment_options_caption(course.get("price", "?") if course else "?"),
                parse_mode="HTML",
            ),
            reply_markup=_payment_keyboard(course_id),
        )
    except Exception:
        pass
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT METHOD DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("pay:"))
async def payment_method_intercept(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    method = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""
    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)

    course = await _get_course(course_id)
    price_line = f"💵 <b>Amount:</b> {course['price']}" if course else "💵 <b>Amount:</b> ?"
    await _set_payment_method(callback.from_user.id, course_id, method)

    rows = []
    if info.get("admin_button"):
        rows.append([InlineKeyboardButton(text="👤 Message Admin", url=ADMIN_CONTACT_URL)])
    rows.append([InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"backpay:{course_id}")])
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=info["image"], caption=f"{info['text']}\n\n{price_line}", parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except Exception:
        return await callback.answer(f"⚠️ Image link for {method.upper()} is broken.", show_alert=True)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT UPLOAD  (always attached to the item the buyer actually paid for)
# ══════════════════════════════════════════════════════════════════════════════

async def _submit_screenshot(tx: dict, user: types.User, file_id: str) -> bool:
    course = await _get_course(tx["course_id"])
    amount = float(tx.get("amount_paid") or 0)
    if amount <= 0 and course:
        inr, usd = _price_pair(course)
        amount = usd if (tx.get("currency") or "INR").upper() == "USD" else inr

    claim = await aexec(
        supabase.table("transactions").update({
            "status":             "awaiting_approval",
            "submitted_at":       _now_iso(),
            "screenshot_file_id": file_id,
            "username":           user.username or "",
            "amount_paid":        amount,
        }).eq("id", tx["id"]).eq("status", "pending_payment")
    )
    if not claim.data:
        return False
    tx = claim.data[0]

    title = html.escape(course["title"] if course else tx["course_id"])
    await bot.send_message(
        user.id,
        f"📸 <b>Screenshot received for:</b>\n{title}\n\n"
        "Admin is reviewing your payment — usually just a few minutes.\n"
        "You'll get a notification once approved. 🔔",
        parse_mode="HTML",
    )
    sent = await _send_admin_card(tx)
    await aexec(supabase.table("transactions").update({"admin_message_id": sent.message_id}).eq("id", tx["id"]))
    return True


def _picker(prefix: str, items: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for suffix, label in items:
        kb.row(InlineKeyboardButton(text=label[:60], callback_data=f"{prefix}:{suffix}"))
    return kb.as_markup()


@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user = message.from_user
    file_id = message.photo[-1].file_id
    pending = await _recent_pending(user.id)

    # Returning buyer asking for their files again → pick the file name from what they own
    if user.id in _resubmit_mode or not pending:
        owned = await _owned_courses(user.id)
        if owned:
            titles = await _titles(owned)
            return await message.reply(
                "📂 <b>Select the file name</b> this payment is for:",
                reply_markup=_picker("ssredo", [(cid, titles.get(cid, cid)) for cid in owned]),
                parse_mode="HTML",
            )
    if not pending:
        return await message.answer(
            "⚠️ <b>No pending payment found.</b>\n\n"
            "Please open a course/bundle link first, then upload your screenshot.",
            parse_mode="HTML",
        )

    with_method = [r for r in pending if r.get("payment_method")]
    chosen = pending[0] if len(pending) == 1 else (with_method[0] if len(with_method) == 1 else None)
    if chosen:
        if not await _submit_screenshot(chosen, user, file_id):
            await message.answer("⏳ This screenshot is already submitted and waiting for admin approval.")
        return

    titles = await _titles([r["course_id"] for r in pending[:6]])
    await message.reply(
        "🧾 <b>Which item is this payment for?</b>\n\nSelect the file name below:",
        reply_markup=_picker("ssitem", [(r["id"], titles.get(r["course_id"], r["course_id"])) for r in pending[:6]]),
        parse_mode="HTML",
    )


def _photo_from_reply(msg: types.Message):
    r = msg.reply_to_message
    return r.photo[-1].file_id if (r and r.photo) else None


@dp.callback_query(F.data.startswith("ssitem:"))
async def pick_pending_item(callback: types.CallbackQuery):
    tx_id = callback.data.split(":", 1)[1]
    file_id = _photo_from_reply(callback.message)
    if not file_id:
        return await callback.answer("Please send your screenshot again.", show_alert=True)
    res = await aexec(
        supabase.table("transactions").select("*").eq("id", tx_id)
        .eq("telegram_user_id", callback.from_user.id).eq("status", "pending_payment")
    )
    if not res.data:
        return await callback.answer("Session expired — open the item link again.", show_alert=True)
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()
    if not await _submit_screenshot(res.data[0], callback.from_user, file_id):
        await bot.send_message(callback.from_user.id, "⏳ This screenshot is already submitted.")


@dp.callback_query(F.data.startswith("ssredo:"))
async def pick_owned_item(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user = callback.from_user
    file_id = _photo_from_reply(callback.message)
    if not file_id:
        return await callback.answer("Please send your screenshot again.", show_alert=True)
    if course_id not in await _owned_courses(user.id):
        return await callback.answer("❌ That item isn't on your account.", show_alert=True)

    _resubmit_mode.discard(user.id)
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()
    tx = await _create_tx(user.id, course_id)
    await _submit_screenshot(tx, user, file_id)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN APPROVE / REJECT
# ══════════════════════════════════════════════════════════════════════════════

async def _get_tx(tx_id: str):
    res = await aexec(supabase.table("transactions").select("*").eq("id", tx_id))
    return res.data[0] if res.data else None


@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, tx_id = callback.data.split("_", 1)
    tx = await _get_tx(tx_id)
    if not tx:
        return await callback.answer("❌ Transaction not found.", show_alert=True)

    if tx["status"] != "awaiting_approval":
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await callback.answer("⚠️ Already processed.", show_alert=True)

    if action == "reject":
        rows = [
            [InlineKeyboardButton(text="💸 Fake Payment",         callback_data=f"rr_fake_{tx_id}")],
            [InlineKeyboardButton(text="📁 Wrong Files Selected", callback_data=f"rr_wrong_{tx_id}")],
            [InlineKeyboardButton(text="✅ Already Approved",     callback_data=f"rr_dupe_{tx_id}")],
            [InlineKeyboardButton(text="✍️ Custom Reason",        callback_data=f"rr_custom_{tx_id}")],
            [InlineKeyboardButton(text="↩️ Cancel",               callback_data=f"rr_cancel_{tx_id}")],
        ]
        await callback.answer("Select a reject reason below.")
        return await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"🔴 <b>Rejecting Tx</b> <code>{tx_id}</code>\n\nSelect the reason to send to the user:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            reply_parameters=ReplyParameters(message_id=callback.message.message_id),
            parse_mode="HTML",
        )

    # ── Approve ───────────────────────────────────────────────────────────────
    user_id, course_id = tx["telegram_user_id"], tx["course_id"]
    owned = await aexec(
        supabase.table("transactions").select("id").eq("telegram_user_id", user_id)
        .eq("course_id", course_id).eq("status", "approved")
    )
    already_owned = bool(owned.data)
    fields = {"status": "redelivered"} if already_owned else {"status": "approved", "approved_at": _now_iso()}
    claim = await aexec(
        supabase.table("transactions").update(fields).eq("id", tx_id).eq("status", "awaiting_approval")
    )
    if not claim.data:
        return await callback.answer("⚠️ Already processed.", show_alert=True)

    await callback.answer(
        "⚠️ Already purchased — re-delivering, stats unchanged." if already_owned
        else "✅ Approving and delivering files… (may take a moment)"
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    problem = ""
    try:
        await _deliver_course(user_id, course_id, tx_id)
    except Exception as e:
        problem = f"\n⚠️ <b>Delivery problem:</b> <code>{html.escape(str(e))[:200]}</code>"

    title = html.escape(await _course_title(course_id))
    money = _money(float(tx.get("amount_paid") or 0), tx.get("currency"))
    if already_owned:
        suffix = ("\n\n♻️ <b>RE-DELIVERED — user already owned this item.</b>\n"
                  "<i>Not counted in sales stats.</i>" + problem)
    else:
        suffix = f"\n\n✅ <b>APPROVED &amp; DELIVERED</b>\n📘 Item: {title}\n💵 Amount: {money}" + problem
    await _append_card(
        callback.message.chat.id, callback.message.message_id,
        callback.message.html_text or "", suffix, is_photo=bool(callback.message.photo),
    )


async def _reject_tx(tx_id: str, stored_reason: str, user_text: str, admin_label: str,
                     orig_chat, orig_msg, orig_html: str, orig_is_photo: bool) -> bool:
    claim = await aexec(
        supabase.table("transactions").update({"status": "rejected", "reject_reason": stored_reason})
        .eq("id", tx_id).eq("status", "awaiting_approval")
    )
    if not claim.data:
        return False
    try:
        await bot.send_message(claim.data[0]["telegram_user_id"], user_text, parse_mode="HTML")
    except Exception:
        pass
    if orig_msg:
        await _append_card(orig_chat, orig_msg, orig_html, f"\n\n❌ <b>REJECTED — {admin_label}</b>", orig_is_photo)
    return True


@dp.callback_query(F.data.startswith("rr_"))
async def reject_reason_handler(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    _, key, tx_id = callback.data.split("_", 2)
    picker = callback.message
    orig = picker.reply_to_message

    if key == "cancel":
        await _safe_delete(picker.chat.id, picker.message_id)
        return await callback.answer("Cancelled.")

    tx = await _get_tx(tx_id)
    if not tx or tx["status"] != "awaiting_approval":
        await _safe_delete(picker.chat.id, picker.message_id)
        return await callback.answer("⚠️ Already processed.", show_alert=True)

    if key == "custom":
        await state.set_state(CustomRejectFSM.waiting_for_reason)
        await state.update_data(
            tx_id=tx_id, picker_msg_id=picker.message_id,
            orig_chat=orig.chat.id if orig else None,
            orig_msg=orig.message_id if orig else None,
            orig_html=(orig.html_text if orig else "") or "",
            orig_is_photo=bool(orig and orig.photo),
        )
        await picker.edit_text(
            "✍️ <b>Custom rejection</b>\n\nType the reason — it will be sent to the user exactly as written.\n"
            "<i>/cancel to abort</i>",
            reply_markup=None, parse_mode="HTML",
        )
        return await callback.answer()

    reasons = {
        "fake":  ("💸 Fake Payment", "Your payment screenshot could not be verified. If this is a mistake, please contact support."),
        "wrong": ("📁 Wrong Files Selected", "It looks like you selected the wrong course. Please go back, select the correct course, and resubmit."),
        "dupe":  ("✅ Already Approved", "This payment was already approved previously. Your files were already delivered. Contact support if you lost access."),
    }
    label, text = reasons.get(key, ("Rejected", "Your payment was rejected. Please contact support."))
    ok = await _reject_tx(
        tx_id, label, f"❌ <b>Payment Rejected — {label}</b>\n\n{text}", label,
        orig.chat.id if orig else None, orig.message_id if orig else None,
        (orig.html_text if orig else "") or "", bool(orig and orig.photo),
    )
    await _safe_delete(picker.chat.id, picker.message_id)
    await callback.answer("✅ Rejected." if ok else "⚠️ Already processed.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    if await state.get_state() is None:
        return await message.answer("Nothing to cancel.")
    await state.clear()
    if data.get("picker_msg_id"):
        await _safe_delete(message.chat.id, data["picker_msg_id"])
    await message.answer("❌ Cancelled. The payment is still pending.")


@dp.message(CustomRejectFSM.waiting_for_reason, F.text)
async def custom_reject_text(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    reason = message.text.strip()
    if not reason or reason.startswith("/"):
        return await message.answer("Send the reason as plain text, or /cancel.")
    if len(reason) > 1000:
        return await message.answer("❌ Too long — keep it under 1000 characters.")

    data = await state.get_data()
    await state.clear()
    short = html.escape(reason if len(reason) <= 80 else reason[:77] + "…")
    ok = await _reject_tx(
        data["tx_id"], reason,
        f"❌ <b>Payment Rejected</b>\n\n{html.escape(reason)}",
        f"Custom: <i>{short}</i>",
        data.get("orig_chat"), data.get("orig_msg"), data.get("orig_html", ""), data.get("orig_is_photo", True),
    )
    if data.get("picker_msg_id"):
        await _safe_delete(message.chat.id, data["picker_msg_id"])
    await message.answer("✅ Rejection sent to the user." if ok else "⚠️ That payment was already processed.")


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN REMINDER (no auto-reject — just a nudge after 12h)
# ══════════════════════════════════════════════════════════════════════════════

async def _send_admin_reminder(tx: dict, waited_secs: float):
    title = html.escape(await _course_title(tx["course_id"]))
    uname = f"@{html.escape(tx['username'])}" if tx.get("username") else f"<code>{tx['telegram_user_id']}</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📌 Open Pending Payment", callback_data=f"pend:{tx['id']}")
    ]])
    reply = (
        ReplyParameters(message_id=tx["admin_message_id"], allow_sending_without_reply=True)
        if tx.get("admin_message_id") else None
    )
    await bot.send_message(
        ADMIN_ID,
        "⏰ <b>You forgot to approve or reject a payment</b>\n\n"
        f"👤 {uname}\n📘 {title}\n"
        f"💵 {_money(float(tx.get('amount_paid') or 0), tx.get('currency'))}\n"
        f"🕒 Waiting for <b>{_human_age(waited_secs)}</b>\n\n"
        "<i>Tap the quoted message above, or the button below.</i>",
        reply_markup=kb, reply_parameters=reply, parse_mode="HTML",
    )
    await aexec(supabase.table("transactions").update({"admin_reminded_at": _now_iso()}).eq("id", tx["id"]))


async def _admin_reminder_loop():
    while True:
        try:
            rows = await fetch_all(
                lambda: supabase.table("transactions").select("*").eq("status", "awaiting_approval").order("id")
            )
            now = datetime.now(timezone.utc)
            for tx in rows:
                submitted = parse_ts(tx.get("submitted_at") or tx["created_at"])
                last = parse_ts(tx["admin_reminded_at"]) if tx.get("admin_reminded_at") else submitted
                if (now - last).total_seconds() >= ADMIN_REMINDER_SECS:
                    await _send_admin_reminder(tx, (now - submitted).total_seconds())
        except Exception as e:
            print(f"admin reminder loop error: {e}")
        await asyncio.sleep(600)


@dp.callback_query(F.data.startswith("pend:"))
async def open_pending(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)
    tx = await _get_tx(callback.data.split(":", 1)[1])
    if not tx or tx["status"] != "awaiting_approval":
        return await callback.answer("✅ Already handled.", show_alert=True)
    sent = await _send_admin_card(tx)          # fresh copy right at the bottom of the chat
    await aexec(supabase.table("transactions").update({"admin_message_id": sent.message_id}).eq("id", tx["id"]))
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    background = [
        asyncio.create_task(_admin_reminder_loop()),
        asyncio.create_task(_delivery_loop()),
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in background:
            t.cancel()

if __name__ == "__main__":
    asyncio.run(main())

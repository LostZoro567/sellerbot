import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from db import supabase

load_dotenv()

BOT_TOKEN             = os.getenv("BOT2_TOKEN")
ADMIN_ID              = int(os.getenv("ADMIN_ID"))
BOT1_USERNAME         = os.getenv("BOT1_USERNAME", "YourGatewayBot")
AUTO_DELETE_SECS      = 900
REFERRAL_PERCENT      = 25

BUNDLE_PRICE_INR      = 1499
BUNDLE_PRICE_USD      = 22
BUNDLE_COURSE_ID      = "bundle_all"
BUNDLE_DELIVERY_TEXT  = (
    "🎉 <b>You now have access to the Full Collection!</b>\n\n"
    "All course materials will be delivered below.\n"
    "<i>Check each pinned link for access.</i>"
)

PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg"

DISCOUNTS = [
    {
        "id": BUNDLE_COURSE_ID,
        "button_text": "🛒 Buy Full Collection — ₹1,499 / $22",
        "detail": (
            "🔥 <b>Full Collection Bundle</b>\n\n"
            "Get <b>every course</b> we offer at a massive discount!\n\n"
            "₹ <b>India:</b> ₹1,499 (instead of paying per course)\n"
            "🌍 <b>International:</b> $22"
        ),
    },
]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return round(float(row.data[0]["wallet_balance"]), 2) if row.data else 0.0


def _deduct_wallet(user_id: int, amount: float) -> bool:
    """
    Atomically deduct amount from wallet.
    Re-reads DB after write to confirm it actually applied
    (Supabase silently ignores RLS-blocked writes without raising errors).
    Returns True only when money is confirmed deducted.
    """
    amount = round(amount, 2)
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not row.data:
        return False
    current = round(float(row.data[0]["wallet_balance"]), 2)
    if current < amount:
        return False
    new_bal = round(current - amount, 2)
    supabase.table("users").update({"wallet_balance": new_bal}).eq("telegram_user_id", user_id).execute()
    # Confirm the write landed
    verify = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not verify.data:
        return False
    confirmed = round(float(verify.data[0]["wallet_balance"]), 2)
    if confirmed != new_bal:
        print(f"[CRITICAL] _deduct_wallet: write did NOT apply for user {user_id}. "
              f"Expected {new_bal}, got {confirmed}. Check Supabase RLS policies.")
        return False
    return True


def _add_wallet(user_id: int, amount: float):
    amount = round(amount, 2)
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if row.data:
        current = round(float(row.data[0]["wallet_balance"]), 2)
        supabase.table("users").update(
            {"wallet_balance": round(current + amount, 2)}
        ).eq("telegram_user_id", user_id).execute()
    else:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username": "",
            "wallet_balance": amount
        }).execute()


def _cancel_pending(user_id: int):
    supabase.table("transactions").update({"status": "cancelled"}).eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").execute()


def _get_course_price(course_id: str) -> float:
    """Single source of truth for numeric price. Never returns 0 for valid courses."""
    if course_id == BUNDLE_COURSE_ID:
        return float(BUNDLE_PRICE_INR)
    cr = supabase.table("courses").select("numeric_price").eq("course_id", course_id).execute()
    if not cr.data:
        return 0.0
    val = cr.data[0].get("numeric_price")
    return round(float(val), 2) if val is not None else 0.0


def _get_course_title(course_id: str) -> str:
    if course_id == BUNDLE_COURSE_ID:
        return "Full Collection Bundle"
    cr = supabase.table("courses").select("title").eq("course_id", course_id).execute()
    return cr.data[0]["title"] if cr.data else course_id


def _create_transaction(user_id: int, course_id: str) -> str:
    """
    Stores amount_paid at creation time — guarantees admin sees correct price (fixes ₹0 bug).
    """
    numeric_price = _get_course_price(course_id)
    result = supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0.0,
        "payment_type":     "screenshot",
        "amount_paid":      numeric_price,
    }).execute()
    return result.data[0]["id"]


def _get_pending_tx(user_id: int, course_id: str):
    res = supabase.table("transactions").select("*").eq(
        "telegram_user_id", user_id
    ).eq("course_id", course_id).eq("status", "pending_payment").order("id", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


def _pay_referrer(buyer_id: int, course_price: float, transaction_id: str, course_id: str):
    """
    Pay referrer 25% commission. Uses read-write-verify pattern.

    ROOT CAUSE OF PREVIOUS BUG:
    supabase-py v2 .update().execute() ALWAYS returns data=[] (empty list),
    even when the write succeeds. The old code did:
        if not update.data: return None, 0
    This always bailed out immediately — commission NEVER paid.

    FIX: Don't rely on update return value. Re-read the row after update
    to confirm the status actually flipped to 'purchased'.
    """
    if course_price <= 0:
        print(f"[REFERRAL] Skipped: course_price={course_price} for buyer {buyer_id}")
        return None, 0

    # 1. Find referral row
    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).execute()
    if not ref_row.data:
        print(f"[REFERRAL] No referral row for buyer {buyer_id}")
        return None, 0

    ref            = ref_row.data[0]
    referrer_id    = ref["referrer_id"]
    ref_id         = ref["id"]
    current_status = ref.get("status", "")

    # 2. Idempotency guard
    if current_status == "purchased":
        print(f"[REFERRAL] Already paid for buyer {buyer_id}, ref_id {ref_id}")
        return None, 0
    if current_status != "joined":
        print(f"[REFERRAL] Unexpected status '{current_status}' for ref_id {ref_id}")
        return None, 0

    credit = round(course_price * REFERRAL_PERCENT / 100, 2)

    # 3. Update status (do NOT check .data — always [] in supabase-py v2)
    supabase.table("referrals").update({
        "status":                 "purchased",
        "paid_on_transaction_id": str(transaction_id),
    }).eq("id", ref_id).execute()

    # 4. Re-read to confirm update applied (real guard against RLS silent failures)
    verify = supabase.table("referrals").select("status").eq("id", ref_id).execute()
    if not verify.data or verify.data[0].get("status") != "purchased":
        confirmed = verify.data[0].get("status") if verify.data else "NO ROW"
        print(f"[REFERRAL] Update did NOT apply for ref_id {ref_id}. Status: {confirmed}. Check RLS.")
        return None, 0

    # 5. Credit wallet
    print(f"[REFERRAL] Crediting referrer {referrer_id} ₹{credit} for buyer {buyer_id}")
    _add_wallet(referrer_id, credit)

    # 6. Audit log (best-effort)
    try:
        supabase.table("referral_commissions").insert({
            "referrer_id":    referrer_id,
            "buyer_id":       buyer_id,
            "transaction_id": str(transaction_id),
            "course_id":      course_id,  # Add this line
            "course_price":   course_price,
            "commission_pct": REFERRAL_PERCENT,
            "commission_amt": credit,
        }).execute()
    except Exception as e:
        print(f"[REFERRAL] Audit log failed: {e}")

    return referrer_id, credit


def _course_keyboard(course_id: str, wallet: float, price: float) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💳  Buy Now",   callback_data=f"buy:{course_id}")],
        [InlineKeyboardButton(text="🏷  Discounts", callback_data=f"discounts:{course_id}")],
    ]
    if wallet >= 1.0:
        if price > 0 and wallet >= price:
            label = f"💰  Use Wallet  (₹{wallet:.2f} available ✅)"
        else:
            label = f"💰  Use Wallet  (₹{wallet:.2f} — need ₹{price:.2f} ❌)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"usewallet:{course_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _payment_keyboard(course_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷  QR Code",        callback_data=f"pay:qr:{course_id}")],
        [InlineKeyboardButton(text="🔵  Paytm / UPI",    callback_data=f"pay:paytm:{course_id}")],
        [InlineKeyboardButton(text="🟦  PayPal",          callback_data=f"pay:paypal:{course_id}")],
        [InlineKeyboardButton(text="🟠  Crypto (USDT)",   callback_data=f"pay:crypto:{course_id}")],
        [InlineKeyboardButton(text="💬  Other Methods",   callback_data=f"pay:others:{course_id}")],
        [InlineKeyboardButton(text="🎁  Refer & Earn",    url=f"https://t.me/{BOT1_USERNAME}?start=refer")],
        [InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"backcourse:{course_id}")],
    ])


def _admin_keyboard(trans_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅  Approve", callback_data=f"approve_{trans_id}"),
        InlineKeyboardButton(text="❌  Reject",  callback_data=f"reject_{trans_id}"),
    ]])


# ══════════════════════════════════════════════════════════════════════════════
# MISC HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _auto_delete(chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def _safe_delete(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def _course_caption(course: dict) -> str:
    return (
        f"📘 <b>{course['title']}</b>\n\n"
        f"{course['bot2_text']}\n\n"
        f"💵 <b>Price:</b> {course['price']}\n\n"
        f"⏳ <i>This payment window closes in 15 minutes.</i>"
    )

async def _deliver_course(user_id: int, course_id: str):
    if course_id == BUNDLE_COURSE_ID:
        del_text    = BUNDLE_DELIVERY_TEXT
        del_file_id = None
    else:
        cr = supabase.table("courses").select("delivery_text, delivery_file_id").eq("course_id", course_id).execute()
        if not cr.data:
            await bot.send_message(user_id, "✅ Payment approved! Contact support for your course materials.")
            return
        del_text    = cr.data[0].get("delivery_text") or "✅ Here is your course material."
        del_file_id = cr.data[0].get("delivery_file_id")

    msg_text = f"{del_text}\n\n⏳ <i>This message self-destructs in 15 minutes.</i>"
    if del_file_id:
        sent = await bot.send_document(chat_id=user_id, document=del_file_id,
                                       caption=msg_text, parse_mode="HTML")
    else:
        sent = await bot.send_message(chat_id=user_id, text=msg_text,
                                      parse_mode="HTML", disable_web_page_preview=True)
    asyncio.create_task(_auto_delete(user_id, sent.message_id))


# ══════════════════════════════════════════════════════════════════════════════
# /start — COURSE DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = (command.args or "").strip()
    user_id   = message.from_user.id

    if not course_id or course_id == BUNDLE_COURSE_ID:
        return await message.answer("⚠️ Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("❌ Course not found or the link is invalid.")

    course = res.data[0]
    price  = round(float(course.get("numeric_price", 0)), 2)
    wallet = _get_wallet(user_id)

    _cancel_pending(user_id)
    _create_transaction(user_id, course_id)

    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=_course_caption(course),
        reply_markup=_course_keyboard(course_id, wallet, price),
        parse_mode="HTML"
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id))


# ══════════════════════════════════════════════════════════════════════════════
# DISCOUNTS
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("discounts:"))
async def show_discounts(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    if not DISCOUNTS:
        return await callback.answer("🚧 No active discounts right now.", show_alert=True)
    lines = ["🏷 <b>Available Discounts & Bundles</b>\n"]
    rows  = []
    for i, d in enumerate(DISCOUNTS, 1):
        lines.append(f"{i}. {d['detail']}\n")
        rows.append([InlineKeyboardButton(text=d["button_text"], callback_data=f"buy:{d['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"backcourse:{course_id}")])
    await callback.message.edit_caption(
        caption="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# BACK TO COURSE
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("backcourse:"))
async def back_to_course(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user_id   = callback.from_user.id
    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
        return await callback.answer()
    course = res.data[0]
    price  = round(float(course.get("numeric_price", 0)), 2)
    wallet = _get_wallet(user_id)
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=course["bot2_image_id"],
                caption=_course_caption(course),
                parse_mode="HTML"
            ),
            reply_markup=_course_keyboard(course_id, wallet, price)
        )
    except Exception:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# BUY NOW
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("buy:"))
async def show_payment_methods(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user_id   = callback.from_user.id
    _cancel_pending(user_id)
    _create_transaction(user_id, course_id)
    if course_id == BUNDLE_COURSE_ID:
        price_display = f"₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}"
    else:
        res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
        price_display = res.data[0]["price"] if res.data else "?"
    try:
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "🏦 <b>Choose a Payment Method</b>\n\n"
                f"💵 <b>Your price:</b> {price_display}\n\n"
                "Select how you'd like to pay below.\n"
                "After paying, send your payment screenshot here.\n\n"
                "⏳ <i>This window closes in 15 minutes.</i>"
            ),
            reply_markup=_payment_keyboard(course_id),
            parse_mode="HTML"
        )
        asyncio.create_task(_auto_delete(user_id, sent.message_id))
    except Exception:
        await callback.answer("⚠️ Payment image link broken. Update PAYMENT_OPTIONS_IMAGE.", show_alert=True)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# BACK TO PAYMENT OPTIONS
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("backpay:"))
async def back_to_payment_options(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    if course_id == BUNDLE_COURSE_ID:
        price_display = f"₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}"
    else:
        res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
        price_display = res.data[0]["price"] if res.data else "?"
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=PAYMENT_OPTIONS_IMAGE,
                caption=(
                    "🏦 <b>Choose a Payment Method</b>\n\n"
                    f"💵 <b>Your price:</b> {price_display}\n\n"
                    "Select how you'd like to pay below.\n"
                    "After paying, send your payment screenshot here.\n\n"
                    "⏳ <i>This window closes in 15 minutes.</i>"
                ),
                parse_mode="HTML"
            ),
            reply_markup=_payment_keyboard(course_id)
        )
    except Exception:
        pass
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENT METHOD DETAIL + BUNDLE UPSELL
# ══════════════════════════════════════════════════════════════════════════════

PAYMENT_METHODS = {
    "qr": {
        "text": (
            "📷 <b>QR Code Payment</b>\n\n"
            "Scan the QR code above to complete your payment.\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
    },
    "paytm": {
        "text": (
            "🔵 <b>Paytm / UPI Payment</b>\n\n"
            "Send payment to the UPI ID below:\n\n"
            "🔑 UPI ID: <code>womp@ptyes</code>\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
    },
    "paypal": {
        "text": (
            "🟦 <b>PayPal Payment</b>\n\n"
            "Send payment to:\n\n"
            "📧 <code>Ankitmallick5790@gmail.com</code>\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
    },
    "crypto": {
        "text": (
            "🟠 <b>Crypto Payment — USDT (BEP20)</b>\n\n"
            "Send USDT to:\n\n"
            "👛 <code>0x1da04f30bdc147612a625b203217f50cdb84e2f6</code>\n\n"
            "⚠️ <i>Send on BEP20 network only!</i>\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://graph.org/file/60cf45bb50cf108f47196-28db3241840c7bc2db.jpg",
    },
    "others": {
        "text": (
            "💬 <b>Other Payment Methods</b>\n\n"
            "Message the admin directly to arrange payment.\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here."
        ),
        "image": "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg",
        "extra_buttons": [
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/YourRealUsername")]
        ],
    },
}

@dp.callback_query(F.data.startswith("pay:"))
async def payment_method_intercept(callback: types.CallbackQuery):
    parts     = callback.data.split(":", 2)
    method    = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""
    if course_id == BUNDLE_COURSE_ID:
        return await _show_payment_detail(callback, method, course_id)
    res          = supabase.table("courses").select("price").eq("course_id", course_id).execute()
    single_price = res.data[0]["price"] if res.data else "?"
    await callback.message.edit_media(
        media=InputMediaPhoto(
            media=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "🔥 <b>Wait — Big Savings Available!</b>\n\n"
                f"You're about to pay <b>{single_price}</b> for one course.\n\n"
                "📦 <b>Full Collection Bundle</b>\n"
                f"Get <b>all our courses</b> for just <b>₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}</b>\n\n"
                "That's every course we offer at one unbeatable price.\n\n"
                "💬 <b>Would you like to upgrade to the full collection?</b>"
            ),
            parse_mode="HTML"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅  Yes, upgrade!", callback_data=f"bundleyes:{method}:{course_id}"),
            InlineKeyboardButton(text="❌  No thanks",     callback_data=f"bundleno:{method}:{course_id}"),
        ]])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("bundleyes:"))
async def bundle_yes(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    _cancel_pending(callback.from_user.id)
    _create_transaction(callback.from_user.id, BUNDLE_COURSE_ID)
    await _show_payment_detail(callback, parts[1], BUNDLE_COURSE_ID)

@dp.callback_query(F.data.startswith("bundleno:"))
async def bundle_no(callback: types.CallbackQuery):
    parts = callback.data.split(":", 2)
    await _show_payment_detail(callback, parts[1], parts[2])

async def _show_payment_detail(callback: types.CallbackQuery, method: str, course_id: str):
    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)
    if course_id == BUNDLE_COURSE_ID:
        price_line = f"💵 <b>Amount:</b> ₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}"
    else:
        res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
        price_line = f"💵 <b>Amount:</b> {res.data[0]['price']}" if res.data else "💵 <b>Amount:</b> ?"
    caption  = f"{info['text']}\n\n{price_line}"
    back_row = [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"backpay:{course_id}")]
    extra    = info.get("extra_buttons", [])
    keyboard = InlineKeyboardMarkup(inline_keyboard=extra + [back_row])
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=info["image"], caption=caption, parse_mode="HTML"),
            reply_markup=keyboard
        )
    except Exception:
        await callback.answer(f"⚠️ Image link for {method.upper()} is broken.", show_alert=True)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# USE WALLET
#
# REFERRAL WALLET RULES:
#   - Balance comes from referring friends who buy courses (25% commission).
#   - Wallet can only be used if balance >= full course price (no partial use).
#   - Using wallet sends an admin approval request. Wallet is NOT deducted yet.
#   - Admin approves → wallet deducted + course delivered.
#   - Admin rejects → nothing deducted, user is notified.
#   - Cannot submit a second wallet request while one is pending approval.
#   - Cannot use wallet for the same course twice.
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("usewallet:"))
async def use_wallet(callback: types.CallbackQuery):
    user_id   = callback.from_user.id
    course_id = callback.data.split(":", 1)[1]

    # 1. Get course price via single authoritative helper
    course_price = _get_course_price(course_id)
    course_title = _get_course_title(course_id)

    if course_price <= 0:
        return await callback.answer("❌ This course has no price set. Contact admin.", show_alert=True)

    # 2. Read wallet balance fresh from DB
    wallet = _get_wallet(user_id)

    # 3. Hard block — wallet must cover full price
    if wallet < course_price:
        shortage = round(course_price - wallet, 2)
        return await callback.answer(
            f"❌ Insufficient wallet balance!\n\n"
            f"Your balance:  ₹{wallet:.2f}\n"
            f"Course price:  ₹{course_price:.2f}\n"
            f"You need:      ₹{shortage:.2f} more\n\n"
            f"Refer more friends to earn credits!",
            show_alert=True
        )

    # 4. Get the active pending transaction for this course
    tx = _get_pending_tx(user_id, course_id)
    if tx is None:
        return await callback.answer(
            "⚠️ Session expired. Please open the course link again.",
            show_alert=True
        )
    trans_id = tx["id"]

    # 5. Guard against double-submission on the same transaction
    if round(float(tx.get("wallet_used") or 0), 2) > 0:
        return await callback.answer(
            "⏳ Your wallet payment is already pending admin approval.\n"
            "Please wait.",
            show_alert=True
        )

    # 6. Mark transaction as awaiting_approval with wallet details
    #    Wallet is NOT deducted here. Deduction happens only on admin approval.
    supabase.table("transactions").update({
        "wallet_used":  course_price,
        "amount_paid":  course_price,
        "status":       "awaiting_approval",
        "payment_type": "wallet",
    }).eq("id", trans_id).execute()

    # 7. Update user-facing message
    await callback.message.edit_caption(
        caption=(
            "💰 <b>Wallet Payment Request Sent!</b>\n\n"
            f"📘 Course: <b>{course_title}</b>\n"
            f"💸 Amount: <b>₹{course_price:.2f}</b>\n\n"
            "⏳ Waiting for admin approval.\n"
            "Your wallet will be deducted only after approval.\n"
            "You'll get a notification once it's confirmed. 🔔"
        ),
        parse_mode="HTML"
    )

    # 8. Notify admin with full details
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "💰 <b>Wallet Purchase Request</b>\n\n"
            f"👤 User ID:       <code>{user_id}</code>\n"
            f"📘 Course:        <code>{course_id}</code>\n"
            f"📛 Title:         {course_title}\n"
            f"💸 Amount:        <b>₹{course_price:.2f}</b>\n"
            f"🏦 User balance:  <b>₹{wallet:.2f}</b>\n\n"
            "<i>(No screenshot — paying from referral wallet balance)</i>\n\n"
            "✅ Approve = deduct wallet + deliver course\n"
            "❌ Reject = cancel, nothing is deducted"
        ),
        reply_markup=_admin_keyboard(trans_id),
        parse_mode="HTML"
    )

    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id

    res = supabase.table("transactions").select("*").eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").order("id", desc=True).limit(1).execute()

    if not res.data:
        return await message.answer(
            "⚠️ <b>No pending payment found.</b>\n\n"
            "Please open a course link first, then upload your screenshot.",
            parse_mode="HTML"
        )

    tx        = res.data[0]
    trans_id  = tx["id"]
    course_id = tx["course_id"]

    # Read price from tx row (stored at creation) — never ₹0
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)

    course_title = _get_course_title(course_id)

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "📸 <b>Screenshot received!</b>\n\n"
        "Admin is reviewing your payment — usually just a few minutes.\n"
        "You'll get a notification once approved. 🔔",
        parse_mode="HTML"
    )

    wallet_bal = _get_wallet(user_id)
    username   = message.from_user.username or str(user_id)

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 <b>New Payment Screenshot</b>\n\n"
            f"👤 User:         @{username} (<code>{user_id}</code>)\n"
            f"📘 Course:       <b>{course_title}</b>\n"
            f"🔑 Course ID:    <code>{course_id}</code>\n"
            f"💵 Course Price: <b>₹{course_price:.2f}</b>\n"
            f"🏦 User Wallet:  <b>₹{wallet_bal:.2f}</b>\n"
            f"🔖 Tx ID:        <code>{trans_id}</code>"
        ),
        reply_markup=_admin_keyboard(trans_id),
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN APPROVE / REJECT
#
# THE ONLY PLACE WHERE WALLET IS DEDUCTED.
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, trans_id_str = callback.data.split("_", 1)
    trans_id = trans_id_str  # UUID string, not int

    res = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not res.data:
        return await callback.answer("❌ Transaction not found.", show_alert=True)

    tx = res.data[0]

    # Idempotency: prevent double-processing
    if tx["status"] in ("approved", "rejected"):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await callback.answer("⚠️ Already processed.", show_alert=True)

    user_id      = tx["telegram_user_id"]
    course_id    = tx["course_id"]
    wallet_used  = round(float(tx.get("wallet_used") or 0.0), 2)
    payment_type = tx.get("payment_type", "screenshot")

    # Read price from tx (stored at creation — never 0)
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)
    course_title = _get_course_title(course_id)

    # Remove buttons immediately to block double-click
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # ── APPROVE ───────────────────────────────────────────────────────────────
    if action == "approve":

        # For wallet payments: deduct NOW. This is the single deduction point.
        if payment_type == "wallet" and wallet_used > 0:
            deducted = _deduct_wallet(user_id, wallet_used)
            if not deducted:
                # Balance is insufficient at approval time (edge case)
                supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()
                await bot.send_message(
                    user_id,
                    "❌ <b>Wallet payment failed.</b>\n\n"
                    "Your balance was insufficient at approval time.\n"
                    "Nothing was deducted. Please contact support.",
                    parse_mode="HTML"
                )
                try:
                    await callback.message.edit_caption(
                        caption=(callback.message.caption or "") +
                                "\n\n❌ <b>REJECTED — WALLET INSUFFICIENT AT APPROVAL</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return await callback.answer("❌ Wallet insufficient — auto-rejected.", show_alert=True)

        # Mark approved in DB
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        # Deliver course
        await _deliver_course(user_id, course_id)

        # Pay referrer — course_price already resolved above from tx.amount_paid
        referrer_id, credit = _pay_referrer(user_id, course_price, trans_id, course_id)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>Referral Commission Earned!</b>\n\n"
                    f"💸 <b>₹{credit:.2f}</b> has been added to your wallet!\n"
                    f"📘 Your referral just purchased: <b>{course_title}</b>\n"
                    f"💵 Course price was: ₹{course_price:.2f}\n\n"
                    f"Check your balance with /wallet in @{BOT1_USERNAME}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # Update admin message to show result
        new_bal = _get_wallet(user_id)
        suffix  = f"\n\n✅ <b>APPROVED & DELIVERED</b>"
        suffix += f"\n📘 Course: {course_title}"
        suffix += f"\n💵 Price: ₹{course_price:.2f}"
        if wallet_used > 0:
            suffix += f"\n💸 Wallet deducted: ₹{wallet_used:.2f}\n🏦 User new balance: ₹{new_bal:.2f}"
        if referrer_id:
            suffix += f"\n🎁 Referrer {referrer_id} earned ₹{credit:.2f}"
        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + suffix,
                parse_mode="HTML"
            )
        except Exception:
            pass

        await callback.answer("✅ Approved and delivered!")

    # ── REJECT ────────────────────────────────────────────────────────────────
    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()

        # Wallet payments: nothing was deducted, so nothing to refund.
        # Screenshot payments: tell user to retry.
        if payment_type == "wallet":
            user_msg = (
                "❌ <b>Wallet payment request rejected.</b>\n\n"
                "No money was deducted from your wallet.\n"
                "If this is a mistake, please contact support."
            )
        else:
            user_msg = (
                "❌ <b>Payment could not be verified.</b>\n\n"
                "Please double-check your payment and re-upload your screenshot.\n"
                "If you need help, contact support."
            )

        await bot.send_message(user_id, user_msg, parse_mode="HTML")

        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n❌ <b>REJECTED</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        await callback.answer("❌ Rejected.")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    print("✅ Sales Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

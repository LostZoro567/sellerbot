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
    "🎉 *You now have access to the Full Collection!*\n\n"
    "All course materials will be delivered below.\n"
    "_Check each pinned link for access._"
)

PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg"

DISCOUNTS = [
    {
        "id": BUNDLE_COURSE_ID,
        "button_text": "🛒 Buy Full Collection — ₹1,499 / $22",
        "detail": (
            "🔥 *Full Collection Bundle*\n\n"
            "Get *every course* we offer at a massive discount!\n\n"
            "₹ *India:* ₹1,499 (instead of paying per course)\n"
            "🌍 *International:* $22"
        ),
    },
]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_user(user_id: int, username: str = ""):
    existing = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username":         username or "",
            "wallet_balance":   0.0,
        }).execute()


def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return round(float(row.data[0]["wallet_balance"]), 2) if row.data else 0.0


def _deduct_wallet(user_id: int, amount: float) -> bool:
    """
    Deduct from wallet. Re-reads DB to confirm write.
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
    # Confirm the write landed (guards against silent RLS failures)
    verify = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not verify.data:
        return False
    confirmed = round(float(verify.data[0]["wallet_balance"]), 2)
    if confirmed != new_bal:
        print(f"[CRITICAL] _deduct_wallet write did NOT apply for user {user_id}. "
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
            "username":         "",
            "wallet_balance":   amount,
        }).execute()


def _cancel_pending(user_id: int):
    supabase.table("transactions").update({"status": "cancelled"}).eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").execute()


def _get_course_price(course_id: str) -> float:
    """
    FIXED: Single authoritative function to get numeric price for any course.
    Returns float. Never returns None or 0 for valid courses.
    """
    if course_id == BUNDLE_COURSE_ID:
        return float(BUNDLE_PRICE_INR)
    cr = supabase.table("courses").select("numeric_price").eq("course_id", course_id).execute()
    if not cr.data:
        return 0.0
    val = cr.data[0].get("numeric_price")
    if val is None:
        return 0.0
    return round(float(val), 2)


def _get_course_title(course_id: str) -> str:
    if course_id == BUNDLE_COURSE_ID:
        return "Full Collection Bundle"
    cr = supabase.table("courses").select("title").eq("course_id", course_id).execute()
    return cr.data[0]["title"] if cr.data else course_id


def _create_transaction(user_id: int, course_id: str) -> str:
    """
    FIXED: Store numeric_price directly in the transaction row at creation time.
    This guarantees the admin notification always shows the correct price,
    regardless of any subsequent DB state changes.
    """
    numeric_price = _get_course_price(course_id)
    result = supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0.0,
        "payment_type":     "screenshot",
        "amount_paid":      numeric_price,  # stored at creation — never zero
    }).execute()
    return result.data[0]["id"]


def _get_pending_tx(user_id: int, course_id: str):
    res = supabase.table("transactions").select("*").eq(
        "telegram_user_id", user_id
    ).eq("course_id", course_id).eq("status", "pending_payment").order("id", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


def _pay_referrer(buyer_id: int, course_price: float, transaction_id: str):
    """
    FIXED: Credit referrer 25% of course_price, one-time only.
    - Atomically flips status joined→purchased before crediting (prevents double-payment).
    - course_price is passed in directly — never re-fetched from DB here.
    - transaction_id stored as text (UUID-safe).
    """
    if course_price <= 0:
        return None, 0

    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).eq("status", "joined").execute()
    if not ref_row.data:
        return None, 0

    ref         = ref_row.data[0]
    referrer_id = ref["referrer_id"]
    credit      = round(course_price * REFERRAL_PERCENT / 100, 2)

    # Atomic update: only succeeds if status is still "joined"
    update = supabase.table("referrals").update({
        "status":                 "purchased",
        "paid_on_transaction_id": str(transaction_id),
    }).eq("id", ref["id"]).eq("status", "joined").execute()

    if not update.data:
        return None, 0  # Already claimed by another process

    _add_wallet(referrer_id, credit)
    return referrer_id, credit


# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARD BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

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
        f"📘 *{course['title']}*\n\n"
        f"{course['bot2_text']}\n\n"
        f"💵 *Price:* {course['price']}\n\n"
        f"⏳ _This payment window closes in 15 minutes._"
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

    msg_text = f"{del_text}\n\n⏳ _This message self-destructs in 15 minutes._"
    if del_file_id:
        sent = await bot.send_document(chat_id=user_id, document=del_file_id,
                                       caption=msg_text, parse_mode="Markdown")
    else:
        sent = await bot.send_message(chat_id=user_id, text=msg_text,
                                      parse_mode="Markdown", disable_web_page_preview=True)
    asyncio.create_task(_auto_delete(user_id, sent.message_id))


# ══════════════════════════════════════════════════════════════════════════════
# /start — COURSE DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = (command.args or "").strip()
    user_id   = message.from_user.id

    _ensure_user(user_id, message.from_user.username or "")

    if not course_id or course_id == BUNDLE_COURSE_ID:
        return await message.answer("⚠️ Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("❌ Course not found or the link is invalid.")

    course = res.data[0]
    price  = round(float(course.get("numeric_price") or 0), 2)
    wallet = _get_wallet(user_id)

    _cancel_pending(user_id)
    _create_transaction(user_id, course_id)

    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=_course_caption(course),
        reply_markup=_course_keyboard(course_id, wallet, price),
        parse_mode="Markdown"
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
    lines = ["🏷 *Available Discounts & Bundles*\n"]
    rows  = []
    for i, d in enumerate(DISCOUNTS, 1):
        lines.append(f"{i}. {d['detail']}\n")
        rows.append([InlineKeyboardButton(text=d["button_text"], callback_data=f"buy:{d['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"backcourse:{course_id}")])
    await callback.message.edit_caption(
        caption="\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="Markdown"
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
    price  = round(float(course.get("numeric_price") or 0), 2)
    wallet = _get_wallet(user_id)
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=course["bot2_image_id"],
                caption=_course_caption(course),
                parse_mode="Markdown"
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
                "🏦 *Choose a Payment Method*\n\n"
                f"💵 *Your price:* {price_display}\n\n"
                "Select how you'd like to pay below.\n"
                "After paying, send your payment screenshot here.\n\n"
                "⏳ _This window closes in 15 minutes._"
            ),
            reply_markup=_payment_keyboard(course_id),
            parse_mode="Markdown"
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
                    "🏦 *Choose a Payment Method*\n\n"
                    f"💵 *Your price:* {price_display}\n\n"
                    "Select how you'd like to pay below.\n"
                    "After paying, send your payment screenshot here.\n\n"
                    "⏳ _This window closes in 15 minutes._"
                ),
                parse_mode="Markdown"
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
            "📷 *QR Code Payment*\n\n"
            "Scan the QR code above to complete your payment.\n\n"
            "📸 *Once paid:* send your payment screenshot right here.\n\n"
            "⏳ _Window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
    },
    "paytm": {
        "text": (
            "🔵 *Paytm / UPI Payment*\n\n"
            "Send payment to the UPI ID below:\n\n"
            "🔑 UPI ID: `womp@ptyes`\n\n"
            "📸 *Once paid:* send your payment screenshot right here.\n\n"
            "⏳ _Window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
    },
    "paypal": {
        "text": (
            "🟦 *PayPal Payment*\n\n"
            "Send payment to:\n\n"
            "📧 `Ankitmallick5790@gmail.com`\n\n"
            "📸 *Once paid:* send your payment screenshot right here.\n\n"
            "⏳ _Window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
    },
    "crypto": {
        "text": (
            "🟠 *Crypto Payment — USDT (BEP20)*\n\n"
            "Send USDT to:\n\n"
            "👛 `0x1da04f30bdc147612a625b203217f50cdb84e2f6`\n\n"
            "⚠️ _Send on BEP20 network only!_\n\n"
            "📸 *Once paid:* send your payment screenshot right here.\n\n"
            "⏳ _Window closes in 15 minutes._"
        ),
        "image": "https://graph.org/file/60cf45bb50cf108f47196-28db3241840c7bc2db.jpg",
    },
    "others": {
        "text": (
            "💬 *Other Payment Methods*\n\n"
            "Message the admin directly to arrange payment.\n\n"
            "📸 *Once paid:* send your payment screenshot right here."
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

    # Bundle already selected — skip upsell
    if course_id == BUNDLE_COURSE_ID:
        return await _show_payment_detail(callback, method, course_id)

    res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
    single_price = res.data[0]["price"] if res.data else "?"

    await callback.message.edit_media(
        media=InputMediaPhoto(
            media=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "🔥 *Wait — Big Savings Available!*\n\n"
                f"You're about to pay *{single_price}* for one course.\n\n"
                "📦 *Full Collection Bundle*\n"
                f"Get *all our courses* for just *₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}*\n\n"
                "That's every course we offer at one unbeatable price.\n\n"
                "💬 *Would you like to upgrade to the full collection?*"
            ),
            parse_mode="Markdown"
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
        price_line = f"💵 *Amount:* ₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}"
    else:
        res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
        price_line = f"💵 *Amount:* {res.data[0]['price']}" if res.data else "💵 *Amount:* ?"
    caption  = f"{info['text']}\n\n{price_line}"
    back_row = [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"backpay:{course_id}")]
    extra    = info.get("extra_buttons", [])
    keyboard = InlineKeyboardMarkup(inline_keyboard=extra + [back_row])
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=info["image"], caption=caption, parse_mode="Markdown"),
            reply_markup=keyboard
        )
    except Exception:
        await callback.answer(f"⚠️ Image link for {method.upper()} is broken.", show_alert=True)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# USE WALLET
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

    # 5. Guard against double-submission
    if round(float(tx.get("wallet_used") or 0), 2) > 0:
        return await callback.answer(
            "⏳ Your wallet payment is already pending admin approval.\nPlease wait.",
            show_alert=True
        )

    # 6. Mark as awaiting_approval with wallet details (NOT deducted yet)
    supabase.table("transactions").update({
        "wallet_used":  course_price,
        "amount_paid":  course_price,
        "status":       "awaiting_approval",
        "payment_type": "wallet",
    }).eq("id", trans_id).execute()

    # 7. Update user-facing message
    await callback.message.edit_caption(
        caption=(
            "💰 *Wallet Payment Request Sent!*\n\n"
            f"📘 Course: *{course_title}*\n"
            f"💸 Amount: *₹{course_price:.2f}*\n\n"
            "⏳ Waiting for admin approval.\n"
            "Your wallet will be deducted only after approval.\n"
            "You'll get a notification once it's confirmed. 🔔"
        ),
        parse_mode="Markdown"
    )

    # 8. Notify admin
    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "💰 *Wallet Purchase Request*\n\n"
            f"👤 User ID:       `{user_id}`\n"
            f"📘 Course:        `{course_id}`\n"
            f"📛 Title:         {course_title}\n"
            f"💸 Amount:        *₹{course_price:.2f}*\n"
            f"🏦 User balance:  *₹{wallet:.2f}*\n\n"
            "_(No screenshot — paying from referral wallet balance)_\n\n"
            "✅ Approve = deduct wallet + deliver course\n"
            "❌ Reject = cancel, nothing is deducted"
        ),
        reply_markup=_admin_keyboard(trans_id),
        parse_mode="Markdown"
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
            "⚠️ *No pending payment found.*\n\n"
            "Please open a course link first, then upload your screenshot.",
            parse_mode="Markdown"
        )

    tx        = res.data[0]
    trans_id  = tx["id"]
    course_id = tx["course_id"]

    # FIXED: Read price from transaction row (stored at creation) — never ₹0
    # Falls back to live DB fetch only if missing (for old rows)
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)

    course_title = _get_course_title(course_id)

    # Update transaction status
    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "📸 *Screenshot received!*\n\n"
        "Admin is reviewing your payment — usually just a few minutes.\n"
        "You'll get a notification once approved. 🔔",
        parse_mode="Markdown"
    )

    wallet_bal = _get_wallet(user_id)
    username   = message.from_user.username or str(user_id)

    # FIXED: Send admin notification with correct course price (never ₹0.00)
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 *New Payment Screenshot*\n\n"
            f"👤 User:         @{username} (`{user_id}`)\n"
            f"📘 Course:       *{course_title}*\n"
            f"🔑 Course ID:    `{course_id}`\n"
            f"💵 Course Price: *₹{course_price:.2f}*\n"
            f"🏦 User Wallet:  *₹{wallet_bal:.2f}*\n"
            f"🔖 Tx ID:        `{trans_id}`"
        ),
        reply_markup=_admin_keyboard(trans_id),
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN APPROVE / REJECT
# THE ONLY PLACE WHERE WALLET IS DEDUCTED.
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, trans_id_str = callback.data.split("_", 1)
    trans_id = trans_id_str  # UUID string

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

    # FIXED: Read price from transaction (stored at creation for accuracy)
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

        # Wallet payments: deduct NOW (single deduction point)
        if payment_type == "wallet" and wallet_used > 0:
            deducted = _deduct_wallet(user_id, wallet_used)
            if not deducted:
                supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()
                await bot.send_message(
                    user_id,
                    "❌ *Wallet payment failed.*\n\n"
                    "Your balance was insufficient at approval time.\n"
                    "Nothing was deducted. Please contact support.",
                    parse_mode="Markdown"
                )
                try:
                    await callback.message.edit_caption(
                        caption=(callback.message.caption or "") +
                                "\n\n❌ *REJECTED — WALLET INSUFFICIENT AT APPROVAL*",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
                return await callback.answer("❌ Wallet insufficient — auto-rejected.", show_alert=True)

        # Mark approved
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        # Deliver course
        await _deliver_course(user_id, course_id)

        # FIXED: Pay referrer with the price already resolved above (never 0)
        referrer_id, credit = _pay_referrer(user_id, course_price, trans_id)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 *Referral Commission Earned!*\n\n"
                    f"💸 *₹{credit:.2f}* has been added to your wallet!\n"
                    f"📘 Your referral just purchased: *{course_title}*\n\n"
                    f"Check your balance with /wallet in @{BOT1_USERNAME}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        # Update admin message
        new_bal = _get_wallet(user_id)
        suffix = "\n\n✅ *APPROVED & DELIVERED*"
        suffix += f"\n📘 Course: {course_title}"
        suffix += f"\n💵 Price: ₹{course_price:.2f}"
        if wallet_used > 0:
            suffix += f"\n💸 Wallet deducted: ₹{wallet_used:.2f}\n🏦 User new balance: ₹{new_bal:.2f}"
        if referrer_id:
            suffix += f"\n🎁 Referrer {referrer_id} earned ₹{credit:.2f}"
        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + suffix,
                parse_mode="Markdown"
            )
        except Exception:
            pass

        await callback.answer("✅ Approved and delivered!")

    # ── REJECT ────────────────────────────────────────────────────────────────
    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()

        if payment_type == "wallet":
            user_msg = (
                "❌ *Wallet payment request rejected.*\n\n"
                "No money was deducted from your wallet.\n"
                "If this is a mistake, please contact support."
            )
        else:
            user_msg = (
                "❌ *Payment could not be verified.*\n\n"
                "Please double-check your payment and re-upload your screenshot.\n"
                "If you need help, contact support."
            )

        await bot.send_message(user_id, user_msg, parse_mode="Markdown")

        try:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n❌ *REJECTED*",
                parse_mode="Markdown"
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

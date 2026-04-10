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
AUTO_DELETE_SECS      = 900    # 15 minutes
REFERRAL_PERCENT      = 25     # % of numeric_price credited to referrer

# ── Bundle (Buy All) pricing ───────────────────────────────────────────────────
BUNDLE_PRICE_INR      = 1499   # ₹1499
BUNDLE_PRICE_USD      = 22     # $22
BUNDLE_COURSE_ID      = "bundle_all"
BUNDLE_DELIVERY_TEXT  = (
    "🎉 *You now have access to the Full Collection!*\n\n"
    "All course materials will be delivered below.\n"
    "_Check each pinned link for access._"
)

# ── CHANGE THIS LINK: This is the main image shown when picking a payment method 
PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg" # <-- Replace with your real Payment Options Image

# ── Interactive Discounts list ────────────────────────────────────────────────
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
    # To add more bundles, add them here using the same format!
]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _auto_delete(chat_id: int, message_id: int, delay: int):
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

def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return float(row.data[0]["wallet_balance"]) if row.data else 0.0

def _add_wallet(user_id: int, amount: float):
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if row.data:
        current = float(row.data[0]["wallet_balance"])
        supabase.table("users").update({"wallet_balance": round(current + amount, 2)}).eq("telegram_user_id", user_id).execute()
    else:
        # Fallback insert so referrers always get paid
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username": "",
            "wallet_balance": round(amount, 2)
        }).execute()

def _deduct_wallet(user_id: int, amount: float) -> bool:
    """
    FIX: Removed the 0.02 tolerance exploit. Now uses strict >= check.
    Returns True and deducts only if user genuinely has enough balance.
    """
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not row.data:
        return False
    current = float(row.data[0]["wallet_balance"])

    # FIX: strict check — no tolerance that could allow negative balance
    if current < amount:
        return False

    new_balance = max(0.0, round(current - amount, 2))
    supabase.table("users").update({"wallet_balance": new_balance}).eq("telegram_user_id", user_id).execute()
    return True

def _pay_referrer(buyer_id: int, numeric_price: float, transaction_id: int):
    """
    FIX: Now accepts transaction_id and checks referral payment against this
    specific transaction to prevent double-payment on multiple purchases.
    Guards are: referral must exist, status != "purchased", and we update
    atomically before crediting wallet to prevent race condition.
    """
    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).execute()
    if not ref_row.data:
        return None, 0
    ref = ref_row.data[0]
    if ref["status"] == "purchased":
        return None, 0

    referrer_id = ref["referrer_id"]
    credit      = round(numeric_price * REFERRAL_PERCENT / 100, 2)

    # FIX: Mark referral as "purchased" FIRST before crediting wallet
    # to prevent a race condition where two concurrent approvals both
    # see status != "purchased" and both credit the referrer.
    update_result = supabase.table("referrals").update(
        {"status": "purchased", "paid_on_transaction_id": transaction_id}
    ).eq("id", ref["id"]).eq("status", "joined").execute()

    # Only credit if the update actually changed a row (atomic guard)
    if not update_result.data:
        return None, 0

    _add_wallet(referrer_id, credit)
    return referrer_id, credit


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _build_course_keyboard(course_id: str, wallet: float) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💳  Buy Now",   callback_data=f"buy:{course_id}")],
        [InlineKeyboardButton(text="🏷  Discounts", callback_data=f"discounts:{course_id}")],
    ]
    if wallet >= 1:
        rows.append([InlineKeyboardButton(
            text=f"💰  Use Wallet  (₹{wallet:.2f} available)",
            callback_data=f"usewallet:{course_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _build_payment_options_keyboard(course_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷  QR Code",          callback_data=f"pay:qr:{course_id}")],
        [InlineKeyboardButton(text="🔵  Paytm / UPI",      callback_data=f"pay:paytm:{course_id}")],
        [InlineKeyboardButton(text="🟦  PayPal",            callback_data=f"pay:paypal:{course_id}")],
        [InlineKeyboardButton(text="🟠  Crypto (USDT)",     callback_data=f"pay:crypto:{course_id}")],
        [InlineKeyboardButton(text="💬  Other Methods",     callback_data=f"pay:others:{course_id}")],
        [InlineKeyboardButton(text="🎁  Refer & Pay",       url=f"https://t.me/{BOT1_USERNAME}?start=refer")],
        [InlineKeyboardButton(text="⬅️  Back to Course",  callback_data=f"backcourse:{course_id}")],
    ])


# ── Handlers ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    user_id   = message.from_user.id

    if not course_id:
        return await message.answer("⚠️ Please use a valid course link to start.")

    # FIX: Prevent direct access to bundle_all via /start — it must go through the discounts menu
    if course_id == BUNDLE_COURSE_ID:
        return await message.answer("⚠️ Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("❌ Course not found or the link is invalid.")

    course = res.data[0]
    wallet = _get_wallet(user_id)

    # FIX: Cancel any existing pending_payment transactions for this user before
    # creating a new one. Prevents accumulation of ghost pending rows that could
    # be exploited with multiple screenshot uploads or wallet button spam.
    supabase.table("transactions").update({"status": "cancelled"}).eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").execute()

    supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0
    }).execute()

    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=(
            f"📘 *{course['title']}*\n\n"
            f"{course['bot2_text']}\n\n"
            f"💵 *Price:* {course['price']}\n\n"
            f"⏳ _This payment window closes in 15 minutes._"
        ),
        reply_markup=_build_course_keyboard(course_id, wallet),
        parse_mode="Markdown"
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id, AUTO_DELETE_SECS))


# ── NEW: Interactive Discounts Menu ────────────────────────────────────────────

@dp.callback_query(F.data.startswith("discounts:"))
async def show_discounts(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]

    if not DISCOUNTS:
        return await callback.answer("🚧 No active discounts right now.", show_alert=True)

    lines = ["🏷 *Available Discounts & Bundles*\n"]
    rows = []
    
    for i, d in enumerate(DISCOUNTS, 1):
        lines.append(f"{i}. {d['detail']}\n")
        # Generate a Buy button specifically for this bundle
        rows.append([InlineKeyboardButton(text=d["button_text"], callback_data=f"buy:{d['id']}")])

    rows.append([InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"backcourse:{course_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await callback.message.edit_caption(
        caption="\n".join(lines),
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("backcourse:"))
async def back_to_course(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
        return await callback.answer()

    course = res.data[0]
    wallet = _get_wallet(callback.from_user.id)

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(
                media=course["bot2_image_id"],
                caption=(
                    f"📘 *{course['title']}*\n\n"
                    f"{course['bot2_text']}\n\n"
                    f"💵 *Price:* {course['price']}\n\n"
                    f"⏳ _This payment window closes in 15 minutes._"
                ),
                parse_mode="Markdown"
            ),
            reply_markup=_build_course_keyboard(course_id, wallet)
        )
    except Exception:
        await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def show_payment_methods(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user_id   = callback.from_user.id

    # FIX: Cancel stale pending transactions and create a fresh one for this course.
    # Previously this only updated the last pending row's course_id, which could
    # silently rebind a partially-wallet-applied transaction to a different course.
    supabase.table("transactions").update({"status": "cancelled"}).eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").execute()

    supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0
    }).execute()

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
            reply_markup=_build_payment_options_keyboard(course_id),
            parse_mode="Markdown"
        )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))
    except Exception:
        await callback.answer("⚠️ General Payment Image link is broken in the code. Update PAYMENT_OPTIONS_IMAGE.", show_alert=True)
    
    await callback.answer()

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
            reply_markup=_build_payment_options_keyboard(course_id)
        )
    except Exception:
        pass
    await callback.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def payment_bundle_intercept(callback: types.CallbackQuery):
    parts     = callback.data.split(":", 2)
    method    = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""

    if course_id == BUNDLE_COURSE_ID:
        return await _show_payment_detail(callback, method, course_id)

    res           = supabase.table("courses").select("price").eq("course_id", course_id).execute()
    single_price  = res.data[0]["price"] if res.data else "?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅  Yes, upgrade me!", callback_data=f"bundleyes:{method}:{course_id}"),
            InlineKeyboardButton(text="❌  No, keep my course", callback_data=f"bundleno:{method}:{course_id}"),
        ]
    ])

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
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("bundleyes:"))
async def bundle_yes(callback: types.CallbackQuery):
    parts     = callback.data.split(":", 2)
    method    = parts[1]
    course_id = parts[2]
    user_id   = callback.from_user.id

    # FIX: Same as show_payment_methods — cancel old pending, create fresh one
    supabase.table("transactions").update({"status": "cancelled"}).eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").execute()

    supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        BUNDLE_COURSE_ID,
        "status":           "pending_payment",
        "wallet_used":      0
    }).execute()

    await _show_payment_detail(callback, method, BUNDLE_COURSE_ID)

@dp.callback_query(F.data.startswith("bundleno:"))
async def bundle_no(callback: types.CallbackQuery):
    parts     = callback.data.split(":", 2)
    method    = parts[1]
    course_id = parts[2]
    await _show_payment_detail(callback, method, course_id)


# ── Payment Details Dictionary ────────────────────────────────────────────────

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

async def _show_payment_detail(callback: types.CallbackQuery, method: str, course_id: str):
    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)

    if course_id == BUNDLE_COURSE_ID:
        price_line = f"💵 *Amount:* ₹{BUNDLE_PRICE_INR:,} / ${BUNDLE_PRICE_USD}"
    else:
        res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
        price_line = f"💵 *Amount:* {res.data[0]['price']}"

    caption = f"{info['text']}\n\n{price_line}"

    back_row = [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"backpay:{course_id}")]
    extra    = info.get("extra_buttons", [])
    keyboard = InlineKeyboardMarkup(inline_keyboard=extra + [back_row])

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=info["image"], caption=caption, parse_mode="Markdown"),
            reply_markup=keyboard
        )
    except Exception:
        await callback.answer(f"⚠️ The image link for {method.upper()} is broken. Please update it in the code.", show_alert=True)
        
    await callback.answer()


# ── Wallet & Screenshot Logic ──────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("usewallet:"))
async def use_wallet(callback: types.CallbackQuery):
    user_id   = callback.from_user.id
    course_id = callback.data.split(":", 1)[1]

    # FIX: Fetch the one specific pending transaction for this exact course,
    # not a broad update across all pending rows.
    tx_res = supabase.table("transactions").select("*").eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").eq("course_id", course_id).execute()

    if not tx_res.data:
        return await callback.answer("⚠️ No active session for this course. Please open the course link again.", show_alert=True)

    transaction = tx_res.data[-1]
    trans_id    = transaction["id"]

    # FIX: Prevent wallet button spam — if wallet_used is already set on this
    # transaction, the user already applied their wallet discount.
    if float(transaction.get("wallet_used") or 0) > 0:
        return await callback.answer("✅ Wallet discount already applied to this order.", show_alert=True)

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await callback.answer("Course not found.", show_alert=True)

    course        = res.data[0]
    numeric_price = float(course.get("numeric_price", 0))
    wallet        = _get_wallet(user_id)

    if wallet <= 0:
        return await callback.answer("⚠️ Your wallet is empty.", show_alert=True)

    discount   = min(wallet, numeric_price)
    amount_due = max(0.0, round(numeric_price - discount, 2))

    # ── CORE FIX: Deduct wallet balance IMMEDIATELY for ALL cases ──────────────
    # Previously, partial-wallet payments (amount_due > 0) never deducted the
    # balance here — they waited for admin approval. This meant a user could:
    #   1. Click "Use Wallet" on course A  → balance untouched, discount recorded
    #   2. Open course B (new pending tx)  → old tx cancelled, wallet_used = 0
    #   3. Click "Use Wallet" on course B  → same balance, discount applied AGAIN
    # Fix: deduct immediately regardless of amount_due. If rejected, we refund.
    # admin_decision must NEVER deduct wallet_used — it is already deducted here.
    if not _deduct_wallet(user_id, discount):
        # Balance changed between reading and deducting (race condition)
        return await callback.answer("⚠️ Wallet balance changed. Please try again.", show_alert=True)

    # Record the discount on the transaction only after successful deduction
    supabase.table("transactions").update({"wallet_used": discount}).eq("id", trans_id).execute()

    if amount_due == 0:
        # Full wallet payment — self-approve immediately, no screenshot needed
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        del_text    = course.get("delivery_text", "✅ Here is your course material.")
        del_file_id = course.get("delivery_file_id")

        if del_file_id:
            sent = await bot.send_document(
                chat_id=user_id, document=del_file_id,
                caption=f"{del_text}\n\n⏳ _This message self-destructs in 15 minutes._",
                parse_mode="Markdown"
            )
        else:
            sent = await bot.send_message(
                chat_id=user_id,
                text=f"{del_text}\n\n⏳ _This message self-destructs in 15 minutes._",
                parse_mode="Markdown", disable_web_page_preview=True
            )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

        referrer_id, credit = _pay_referrer(user_id, numeric_price, trans_id)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"💸 *₹{credit:.2f} added to your wallet!*\n\n"
                    f"One of your referrals just purchased *{course['title']}*.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=(
                "✅ *Payment Complete — Fully Paid with Wallet!*\n\n"
                "Your course has been delivered above. 🎓\n"
                "Enjoy the course!"
            ),
            parse_mode="Markdown"
        )

    else:
        # Partial wallet payment — wallet already deducted, user pays the remainder
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"backcourse:{course_id}")]
        ])
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "💰 *Wallet Discount Applied!*\n\n"
                f"┌ 🎫 Wallet credit used:   *₹{discount:.2f}*\n"
                f"└ 💵 Remaining to pay:     *₹{amount_due:.2f}*\n\n"
                f"Please pay the remaining *₹{amount_due:.2f}* using any payment method "
                "and send your screenshot here.\n\n"
                "⏳ _This window closes in 15 minutes._"
            ),
            reply_markup=back_kb,
            parse_mode="Markdown"
        )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

    await callback.answer()


@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id

    # FIX: Only ever bind a screenshot to one transaction — and explicitly
    # pick the single most recent pending_payment row. Using .execute().data[-1]
    # was correct before, but we now also guard against there being >1 pending row
    # (those should have been cancelled on new course open, but just in case).
    res = supabase.table("transactions").select("*").eq(
        "telegram_user_id", user_id
    ).eq("status", "pending_payment").order("id", desc=True).limit(1).execute()

    if not res.data:
        return await message.answer(
            "⚠️ *No pending payment found.*\n\n"
            "Please open a course link first, then upload your screenshot.",
            parse_mode="Markdown"
        )

    transaction = res.data[0]
    trans_id    = transaction["id"]
    wallet_used = float(transaction.get("wallet_used") or 0.0)

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "📸 *Screenshot received!*\n\n"
        "The admin is reviewing your payment — this usually takes just a few minutes.\n"
        "You'll get a notification here once it's approved. 🔔",
        parse_mode="Markdown"
    )

    wallet_note = f"\n💰 *Wallet credit used:* ₹{wallet_used:.2f}" if wallet_used > 0 else ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅  Approve", callback_data=f"approve_{trans_id}"),
            InlineKeyboardButton(text="❌  Reject",  callback_data=f"reject_{trans_id}")
        ]
    ])

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 *New Payment Screenshot*\n\n"
            f"👤 User ID: `{user_id}`\n"
            f"📘 Course: `{transaction['course_id']}`{wallet_note}"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ── Admin Approve / Reject ─────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, trans_id = callback.data.split("_", 1)

    res = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not res.data:
        return await callback.answer("Transaction not found.", show_alert=True)

    transaction = res.data[0]

    if transaction["status"] in ("approved", "rejected"):
        await callback.answer(
            "⚠️ Already processed — this payment was already handled.",
            show_alert=True
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    user_id     = transaction["telegram_user_id"]
    course_id   = transaction["course_id"]
    wallet_used = float(transaction.get("wallet_used") or 0.0)

    # Rebuild caption safely avoiding markdown crashes
    wallet_note = f"\n💰 *Wallet credit used:* ₹{wallet_used:.2f}" if wallet_used > 0 else ""
    safe_caption = (
        f"💳 *New Payment Screenshot*\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"📘 Course: `{course_id}`{wallet_note}"
    )

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        # Wallet was already deducted immediately in use_wallet for ALL cases
        # (both full and partial payments). Do NOT deduct here — that would
        # double-charge the user. Refund only happens in the reject branch below.

        if course_id == BUNDLE_COURSE_ID:
            numeric_price = float(BUNDLE_PRICE_INR)
            del_text      = BUNDLE_DELIVERY_TEXT
            del_file_id   = None
            course_title  = "Full Collection Bundle"
        else:
            cr            = supabase.table("courses").select("*").eq("course_id", course_id).execute()
            course        = cr.data[0]
            numeric_price = float(course.get("numeric_price", 0))
            del_text      = course.get("delivery_text", "✅ Payment verified! Here is your course material.")
            del_file_id   = course.get("delivery_file_id")
            course_title  = course["title"]

        if del_file_id:
            sent = await bot.send_document(
                chat_id=user_id, document=del_file_id,
                caption=f"{del_text}\n\n⏳ _This message self-destructs in 15 minutes._",
                parse_mode="Markdown"
            )
        else:
            sent = await bot.send_message(
                chat_id=user_id,
                text=f"{del_text}\n\n⏳ _This message self-destructs in 15 minutes._",
                parse_mode="Markdown", disable_web_page_preview=True
            )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

        referrer_id, credit = _pay_referrer(user_id, numeric_price, int(trans_id))
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"💸 *₹{credit:.2f} added to your wallet!*\n\n"
                    f"Your referral just purchased *{course_title}*.\n\n"
                    f"(https://t.me/{BOT1_USERNAME})",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=f"{safe_caption}\n\n✅ *APPROVED & DELIVERED*",
            parse_mode="Markdown"
        )

    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()

        # FIX: Refund wallet_used back to user on rejection so they don't lose balance
        if wallet_used > 0:
            _add_wallet(user_id, wallet_used)

        await bot.send_message(
            user_id,
            "❌ *Payment could not be verified.*\n\n"
            "Please double-check your payment and re-upload your screenshot.\n"
            "If you need help, contact support.",
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(
            caption=f"{safe_caption}\n\n❌ *REJECTED*",
            parse_mode="Markdown"
        )

    await callback.answer()


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Sales Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

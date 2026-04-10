import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from db import supabase

load_dotenv()

BOT_TOKEN        = os.getenv("BOT2_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID"))
BOT1_USERNAME    = os.getenv("BOT1_USERNAME", "YourGatewayBot")
AUTO_DELETE_SECS = 900   # 15 minutes
REFERRAL_PERCENT = 25    # % of numeric_price credited to referrer

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _auto_delete(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return float(row.data[0]["wallet_balance"]) if row.data else 0.0


def _deduct_wallet(user_id: int, amount: float) -> bool:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not row.data:
        return False
    current = float(row.data[0]["wallet_balance"])
    if current < amount:
        return False
    supabase.table("users").update({"wallet_balance": round(current - amount, 2)}).eq("telegram_user_id", user_id).execute()
    return True


def _add_wallet(user_id: int, amount: float):
    row     = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    current = float(row.data[0]["wallet_balance"]) if row.data else 0.0
    supabase.table("users").update({"wallet_balance": round(current + amount, 2)}).eq("telegram_user_id", user_id).execute()


def _pay_referrer(buyer_id: int, numeric_price: float):
    """Credit referrer's wallet when their referred user completes a purchase."""
    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).execute()
    if not ref_row.data:
        return None, 0

    ref = ref_row.data[0]
    if ref["status"] == "purchased":
        return None, 0

    referrer_id = ref["referrer_id"]
    credit      = round(numeric_price * REFERRAL_PERCENT / 100, 2)

    _add_wallet(referrer_id, credit)
    supabase.table("referrals").update({"status": "purchased"}).eq("id", ref["id"]).execute()

    return referrer_id, credit


def _build_course_keyboard(course_id: str, wallet: float) -> InlineKeyboardMarkup:
    """Build the landing page keyboard for a given course."""
    rows = [
        [InlineKeyboardButton(text="💳  Buy Now", callback_data=f"buy_{course_id}")],
    ]
    if wallet >= 1:
        rows.append([InlineKeyboardButton(
            text=f"💰  Use Wallet  (₹{wallet:.2f} available)",
            callback_data=f"usewallet_{course_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── STEP 1: Course landing page ────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    user_id   = message.from_user.id

    if not course_id:
        return await message.answer("⚠️ Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("❌ Course not found or the link is invalid.")

    course = res.data[0]
    wallet = _get_wallet(user_id)

    # Upsert pending transaction
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
            f"⏱ _This payment window closes in 15 minutes._"
        ),
        reply_markup=_build_course_keyboard(course_id, wallet),
        parse_mode="Markdown"
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id, AUTO_DELETE_SECS))


# ── Refer & Pay info screen ────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("referpay_"))
async def show_refer_and_pay(callback: types.CallbackQuery):
    course_id = callback.data.split("_", 1)[1]
    user_id   = callback.from_user.id

    # Re-fetch course so we can show the price
    res = supabase.table("courses").select("title, price, numeric_price").eq("course_id", course_id).execute()
    if not res.data:
        return await callback.answer("Course not found.", show_alert=True)

    course        = res.data[0]
    numeric_price = float(course.get("numeric_price", 0))
    earn_amount   = round(numeric_price * REFERRAL_PERCENT / 100, 2)
    wallet        = _get_wallet(user_id)

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"buy_{course_id}")]
    ])

    await callback.message.edit_caption(
        caption=(
            f"🎁 *Refer & Pay — Earn While You Buy!*\n\n"

            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💡 *How it works:*\n"
            f"1️⃣  Share your referral link (from @{BOT1_USERNAME})\n"
            f"2️⃣  A friend joins and buys any course\n"
            f"3️⃣  You earn *{REFERRAL_PERCENT}%* of their purchase as wallet credits\n"
            f"4️⃣  Use those credits to pay for your own course!\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"

            f"📘 *This course:* {course['title']}\n"
            f"💵 *Price:* {course['price']}\n"
            f"🎯 *You'd earn ₹{earn_amount:.2f}* for each friend who buys this course\n\n"

            f"💰 *Your current wallet balance:* ₹{wallet:.2f}\n\n"

            f"👉 To get your referral link or check your wallet balance,\n"
            f"open @{BOT1_USERNAME} and tap *💼 /wallet*"
        ),
        reply_markup=back_kb,
        parse_mode="Markdown"
    )
    await callback.answer()


# ── Back to course landing ─────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("back_course_"))
async def back_to_course(callback: types.CallbackQuery):
    course_id = callback.data.split("back_course_", 1)[1]
    user_id   = callback.from_user.id

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await callback.answer("Course not found.", show_alert=True)

    course = res.data[0]
    wallet = _get_wallet(user_id)

    await callback.message.edit_caption(
        caption=(
            f"📘 *{course['title']}*\n\n"
            f"{course['bot2_text']}\n\n"
            f"💵 *Price:* {course['price']}\n\n"
            f"⏱ _This payment window closes in 15 minutes._"
        ),
        reply_markup=_build_course_keyboard(course_id, wallet),
        parse_mode="Markdown"
    )
    await callback.answer()


# ── STEP 1.5a: Payment method picker ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_methods(callback: types.CallbackQuery):
    course_id = callback.data.split("buy_", 1)[1]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷  QR Code",          callback_data=f"pay_qr_{course_id}")],
        [InlineKeyboardButton(text="🟣  Paytm / UPI",      callback_data=f"pay_paytm_{course_id}")],
        [InlineKeyboardButton(text="🔵  PayPal",           callback_data=f"pay_paypal_{course_id}")],
        [InlineKeyboardButton(text="🟠  Crypto (USDT)",    callback_data=f"pay_crypto_{course_id}")],
        [InlineKeyboardButton(text="💬  Other Methods",    callback_data=f"pay_others_{course_id}")],
        [InlineKeyboardButton(text="🎁  Refer & Pay",      callback_data=f"referpay_{course_id}")],
        [InlineKeyboardButton(text="⬅️  Back to Course",   callback_data=f"back_course_{course_id}")],
    ])
    await callback.message.edit_caption(
        caption=(
            "🏦 *Choose a Payment Method*\n\n"
            "Select how you'd like to pay below.\n"
            "After paying, send your payment screenshot here.\n\n"
            "⏱ _This window closes in 15 minutes._"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()


# ── STEP 1.5b: Wallet redeem ───────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("usewallet_"))
async def use_wallet(callback: types.CallbackQuery):
    user_id   = callback.from_user.id
    course_id = callback.data.split("usewallet_", 1)[1]

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

    # Update pending transaction with wallet discount
    supabase.table("transactions").update({"wallet_used": discount}) \
        .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if amount_due == 0:
        # Fully covered by wallet — deduct and auto-approve
        _deduct_wallet(user_id, discount)

        latest_tx = supabase.table("transactions").select("id") \
            .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
        if latest_tx.data:
            supabase.table("transactions").update({"status": "approved"}) \
                .eq("id", latest_tx.data[-1]["id"]).execute()

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

        # Pay referrer
        referrer_id, credit = _pay_referrer(user_id, numeric_price)
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
        # Partial discount — still need to pay the remainder
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️  Back to Course", callback_data=f"back_course_{course_id}")]
        ])
        await callback.message.edit_caption(
            caption=(
                f"💰 *Wallet Discount Applied!*\n\n"
                f"┌ 🎫 Wallet credit used:   *₹{discount:.2f}*\n"
                f"└ 💵 Remaining to pay:     *₹{amount_due:.2f}*\n\n"
                f"Please pay the remaining *₹{amount_due:.2f}* using any payment method "
                f"and send your screenshot here.\n\n"
                f"⏱ _This window closes in 15 minutes._"
            ),
            reply_markup=back_kb,
            parse_mode="Markdown"
        )

    await callback.answer()


# ── STEP 1.5c: Individual payment details ─────────────────────────────────────

PAYMENT_METHODS = {
    "qr": {
        "text":  (
            "📷 *QR Code Payment*\n\n"
            "Scan the QR code shown above to complete your payment.\n\n"
            "📸 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "⏱ _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
    },
    "paytm": {
        "text":  (
            "🟣 *Paytm / UPI Payment*\n\n"
            "Send payment to the UPI ID below:\n\n"
            "🔑 UPI ID: `womp@ptyes`\n\n"
            "📸 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "⏱ _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
    },
    "paypal": {
        "text":  (
            "🔵 *PayPal Payment*\n\n"
            "Send payment to the email below:\n\n"
            "📧 Email: `Ankitmallick5790@gmail.com`\n\n"
            "📸 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "⏱ _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
    },
    "crypto": {
        "text":  (
            "🟠 *Crypto Payment — USDT (BEP20)*\n\n"
            "Send USDT to the wallet address below:\n\n"
            "👛 Address:\n`0x1da04f30bdc147612a625b203217f50cdb84e2f6`\n\n"
            "⚠️ _Make sure you're sending on the BEP20 network!_\n\n"
            "📸 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "⏱ _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/T5X40Ys/2a024034c5aa.jpg",
    },
    "others": {
        "text":  (
            "💬 *Other Payment Methods*\n\n"
            "Tap the button below to message the admin directly and arrange payment.\n\n"
            "📸 *Once paid:* send your payment screenshot right here in this chat."
        ),
        "image": "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg",
        "extra_buttons": [[InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/ProSeller_69")]],
    },
}

@dp.callback_query(F.data.startswith("pay_"))
async def show_payment_details(callback: types.CallbackQuery):
    # Format: pay_METHOD_COURSEID
    parts  = callback.data.split("_", 2)   # ["pay", "METHOD", "course_id"]
    method = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""

    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)

    back_row    = [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"buy_{course_id}")]
    extra       = info.get("extra_buttons", [])
    all_rows    = extra + [back_row]
    keyboard    = InlineKeyboardMarkup(inline_keyboard=all_rows)

    await callback.message.edit_media(
        media=InputMediaPhoto(media=info["image"], caption=info["text"], parse_mode="Markdown"),
        reply_markup=keyboard
    )
    await callback.answer()


# ── STEP 2: Screenshot upload ──────────────────────────────────────────────────

@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id

    res = supabase.table("transactions").select("*") \
        .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if not res.data:
        return await message.answer(
            "⚠️ *No pending payment found.*\n\n"
            "Please open a course link first, then upload your screenshot.",
            parse_mode="Markdown"
        )

    transaction = res.data[-1]
    trans_id    = transaction["id"]
    wallet_used = float(transaction.get("wallet_used", 0))

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "📸 *Screenshot received!*\n\n"
        "The admin is reviewing your payment — this usually takes just a few minutes.\n"
        "You'll get a notification here once it's approved. 🔔",
        parse_mode="Markdown"
    )

    wallet_note = f"\n💰 *Wallet credit used:* ₹{wallet_used:.2f}" if wallet_used > 0 else ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅  Approve", callback_data=f"approve_{trans_id}"),
        InlineKeyboardButton(text="❌  Reject",  callback_data=f"reject_{trans_id}")
    ]])

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


# ── STEP 3: Admin approves / rejects ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, trans_id = callback.data.split("_", 1)

    res = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not res.data:
        return await callback.answer("Transaction not found.", show_alert=True)

    transaction = res.data[0]

    # ── Guard: already processed — stop here, remove buttons ──────────────────
    if transaction["status"] in ("approved", "rejected"):
        await callback.answer("⚠️ Already processed — this payment was already handled.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # ── Remove Approve/Reject buttons immediately so rapid taps can't re-fire ──
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    user_id     = transaction["telegram_user_id"]
    course_id   = transaction["course_id"]
    wallet_used = float(transaction.get("wallet_used", 0))

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        if wallet_used > 0:
            _deduct_wallet(user_id, wallet_used)

        cr     = supabase.table("courses").select("*").eq("course_id", course_id).execute()
        course = cr.data[0]

        numeric_price = float(course.get("numeric_price", 0))
        del_text      = course.get("delivery_text", "✅ Payment verified! Here is your course material.")
        del_file_id   = course.get("delivery_file_id")

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

        # Pay referrer (always on full course price, regardless of wallet discount)
        referrer_id, credit = _pay_referrer(user_id, numeric_price)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"💸 *₹{credit:.2f} added to your wallet!*\n\n"
                    f"Your referral just purchased *{course['title']}*.\n\n"
                    f"[Check your wallet →](https://t.me/{BOT1_USERNAME})",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n✅ *APPROVED & DELIVERED*",
            parse_mode="Markdown"
        )

    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()

        await bot.send_message(
            user_id,
            "❌ *Payment could not be verified.*\n\n"
            "Please double-check your payment and re-upload your screenshot.\n"
            "If you need help, contact support.",
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n❌ *REJECTED*",
            parse_mode="Markdown"
        )

    await callback.answer()


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Sales Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

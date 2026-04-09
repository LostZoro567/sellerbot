import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from db import supabase

load_dotenv()

BOT_TOKEN          = os.getenv("BOT2_TOKEN")
ADMIN_ID           = int(os.getenv("ADMIN_ID"))
BOT1_USERNAME      = os.getenv("BOT1_USERNAME", "YourGatewayBot")   # Gateway bot username (no @)
AUTO_DELETE_SECS   = 900   # 15 minutes
REFERRAL_PERCENT   = 25    # % of numeric_price credited to referrer

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
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    current = float(row.data[0]["wallet_balance"]) if row.data else 0.0
    supabase.table("users").update({"wallet_balance": round(current + amount, 2)}).eq("telegram_user_id", user_id).execute()


def _pay_referrer(buyer_id: int, numeric_price: float):
    """Credit referrer's wallet when their referred user completes a purchase."""
    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).execute()
    if not ref_row.data:
        return None, 0

    ref = ref_row.data[0]
    if ref["status"] == "purchased":
        return None, 0   # already paid for a previous purchase; still credit on each purchase

    referrer_id = ref["referrer_id"]
    credit = round(numeric_price * REFERRAL_PERCENT / 100, 2)

    _add_wallet(referrer_id, credit)

    # Mark referral as purchased (first time)
    supabase.table("referrals").update({"status": "purchased"}).eq("id", ref["id"]).execute()

    return referrer_id, credit


# ── STEP 1: Course landing page ────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    user_id   = message.from_user.id

    if not course_id:
        return await message.answer("Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("Course not found or the link is invalid.")

    course  = res.data[0]
    wallet  = _get_wallet(user_id)

    # Upsert pending transaction
    supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0
    }).execute()

    # Build keyboard — show wallet button only if they have balance
    kb_rows = [[InlineKeyboardButton(text="💳 Buy Now", callback_data=f"buy_{course_id}")]]
    if wallet >= 1:
        kb_rows.append([InlineKeyboardButton(
            text=f"💰 Use Wallet (₹{wallet:.2f} available)",
            callback_data=f"usewallet_{course_id}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=(
            f"📘 *{course['title']}*\n\n"
            f"{course['bot2_text']}\n\n"
            f"💵 *Price:* {course['price']}\n\n"
            f"⏳ _This payment window closes in 15 minutes._"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id, AUTO_DELETE_SECS))


# ── STEP 1.5a: Payment method picker ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_methods(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 QR Code",         callback_data="pay_qr")],
        [InlineKeyboardButton(text="🟣 Paytm / UPI",     callback_data="pay_paytm")],
        [InlineKeyboardButton(text="🔵 PayPal",          callback_data="pay_paypal")],
        [InlineKeyboardButton(text="🟠 Crypto (USDT)",   callback_data="pay_crypto")],
        [InlineKeyboardButton(text="💬 Other Methods",   callback_data="pay_others")],
    ])
    await callback.message.edit_caption(
        caption="🏦 *Select your payment method:*\n\n⏳ _Window closes in 15 minutes._",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()


# ── STEP 1.5b: Wallet redeem ───────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("usewallet_"))
async def use_wallet(callback: types.CallbackQuery):
    user_id   = callback.from_user.id
    course_id = callback.data.split("_", 1)[1]

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await callback.answer("Course not found.", show_alert=True)

    course        = res.data[0]
    numeric_price = float(course.get("numeric_price", 0))
    wallet        = _get_wallet(user_id)

    if wallet <= 0:
        return await callback.answer("Your wallet is empty.", show_alert=True)

    discount    = min(wallet, numeric_price)
    amount_due  = max(0.0, round(numeric_price - discount, 2))

    # Update pending transaction with wallet discount
    supabase.table("transactions").update({"wallet_used": discount}).eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if amount_due == 0:
        # Fully covered by wallet — deduct and auto-approve
        _deduct_wallet(user_id, discount)

        # Mark transaction approved
        latest_tx = supabase.table("transactions").select("id").eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
        if latest_tx.data:
            supabase.table("transactions").update({"status": "approved"}).eq("id", latest_tx.data[-1]["id"]).execute()

        # Deliver course
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
                    f"💸 *₹{credit:.2f} added to your wallet!*\n\nOne of your referrals just purchased *{course['title']}*.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=f"✅ *Payment complete using wallet!*\n\nYour course has been delivered. Check the message above.",
            parse_mode="Markdown"
        )
    else:
        # Partial discount
        await callback.message.edit_caption(
            caption=(
                f"💰 *Wallet Discount Applied!*\n\n"
                f"Wallet credit: *₹{discount:.2f}*\n"
                f"Remaining to pay: *₹{amount_due:.2f}*\n\n"
                f"Please pay the remaining amount and upload your screenshot below.\n\n"
                f"⏳ _Window closes in 15 minutes._"
            ),
            parse_mode="Markdown"
        )

    await callback.answer()


# ── STEP 1.5c: Individual payment details ─────────────────────────────────────

PAYMENT_METHODS = {
    "qr": {
        "text":  "📷 *QR Code Payment*\n\nScan the QR code to pay.\n\n📸 Upload your payment screenshot here after paying.\n\n⏳ _Auto-closing soon._",
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
        "kb":    None,
    },
    "paytm": {
        "text":  "🟣 *Paytm / UPI*\n\nUPI ID: `womp@ptyes`\n\n📸 Upload your payment screenshot here after paying.\n\n⏳ _Auto-closing soon._",
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
        "kb":    None,
    },
    "paypal": {
        "text":  "🔵 *PayPal*\n\nSend to: `Ankitmallick5790@gmail.com`\n\n📸 Upload your payment screenshot here after paying.\n\n⏳ _Auto-closing soon._",
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
        "kb":    None,
    },
    "crypto": {
        "text":  "🟠 *Crypto — USDT (BEP20)*\n\nWallet: `0x1da04f30bdc147612a625b203217f50cdb84e2f6`\n\n📸 Upload your payment screenshot here after paying.\n\n⏳ _Auto-closing soon._",
        "image": "https://i.ibb.co/T5X40Ys/2a024034c5aa.jpg",
        "kb":    None,
    },
    "others": {
        "text":  "💬 *Other Payment Methods*\n\nClick below to message the admin directly and arrange payment.\n\n📸 Once paid, upload your screenshot here.",
        "image": "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg",
        "kb":    InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/ProSeller_69")]
        ]),
    },
}

@dp.callback_query(F.data.startswith("pay_"))
async def show_payment_details(callback: types.CallbackQuery):
    method = callback.data.split("_", 1)[1]
    info   = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown method.", show_alert=True)

    await callback.message.edit_media(
        media=InputMediaPhoto(media=info["image"], caption=info["text"], parse_mode="Markdown"),
        reply_markup=info["kb"]
    )
    await callback.answer()


# ── STEP 2: Screenshot upload ──────────────────────────────────────────────────

@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id

    res = supabase.table("transactions").select("*") \
        .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if not res.data:
        return await message.answer("No pending payment found. Please select a course first.")

    transaction = res.data[-1]
    trans_id    = transaction["id"]
    wallet_used = float(transaction.get("wallet_used", 0))

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()
    await message.answer("✅ Screenshot received! Admin is reviewing your payment — usually takes just a few minutes.")

    wallet_note = f"\n💰 *Wallet credit used:* ₹{wallet_used:.2f}" if wallet_used > 0 else ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{trans_id}"),
        InlineKeyboardButton(text="❌ Reject",  callback_data=f"reject_{trans_id}")
    ]])

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 *New Payment Screenshot*\n\n"
            f"User ID: `{user_id}`\n"
            f"Course: `{transaction['course_id']}`{wallet_note}"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ── STEP 3: Admin approves / rejects ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized.", show_alert=True)

    action, trans_id = callback.data.split("_", 1)

    res = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not res.data:
        return await callback.answer("Transaction not found.", show_alert=True)

    transaction = res.data[0]
    user_id     = transaction["telegram_user_id"]
    course_id   = transaction["course_id"]
    wallet_used = float(transaction.get("wallet_used", 0))

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()

        # Deduct wallet if partial discount was used
        if wallet_used > 0:
            _deduct_wallet(user_id, wallet_used)

        # Fetch course
        cr = supabase.table("courses").select("*").eq("course_id", course_id).execute()
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

        # Pay referrer (25% of full course price, regardless of wallet discount)
        referrer_id, credit = _pay_referrer(user_id, numeric_price)
        if referrer_id:
            try:
                bot1_info_link = f"https://t.me/{BOT1_USERNAME}"
                await bot.send_message(
                    referrer_id,
                    f"💸 *₹{credit:.2f} added to your wallet!*\n\n"
                    f"Your referral just purchased *{course['title']}*.\n\n"
                    f"[Check your wallet]({bot1_info_link})",
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
            "❌ *Payment could not be verified.*\n\nPlease double-check and re-upload your screenshot, or contact support.",
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

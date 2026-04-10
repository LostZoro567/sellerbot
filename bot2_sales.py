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

# Image shown on the payment method picker screen.
# Replace this URL with your own banner if you want a custom image.
PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg"

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
    rows = [
        [InlineKeyboardButton(text="\U0001f4b3  Buy Now", callback_data=f"buy_{course_id}")],
    ]
    if wallet >= 1:
        rows.append([InlineKeyboardButton(
            text=f"\U0001f4b0  Use Wallet  (\u20b9{wallet:.2f} available)",
            callback_data=f"usewallet_{course_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_payment_options_keyboard(course_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4f7  QR Code",         callback_data=f"pay_qr_{course_id}")],
        [InlineKeyboardButton(text="\U0001f7e3  Paytm / UPI",     callback_data=f"pay_paytm_{course_id}")],
        [InlineKeyboardButton(text="\U0001f535  PayPal",           callback_data=f"pay_paypal_{course_id}")],
        [InlineKeyboardButton(text="\U0001f7e0  Crypto (USDT)",    callback_data=f"pay_crypto_{course_id}")],
        [InlineKeyboardButton(text="\U0001f4ac  Other Methods",    callback_data=f"pay_others_{course_id}")],
        [InlineKeyboardButton(text="\U0001f381  Refer & Pay",      callback_data=f"referpay_{course_id}")],
        [InlineKeyboardButton(text="\u2b05\ufe0f  Back to Course", callback_data=f"back_course_{course_id}")],
    ])


# ── STEP 1: Course landing page ────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    user_id   = message.from_user.id

    if not course_id:
        return await message.answer("\u26a0\ufe0f Please use a valid course link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("\u274c Course not found or the link is invalid.")

    course = res.data[0]
    wallet = _get_wallet(user_id)

    supabase.table("transactions").insert({
        "telegram_user_id": user_id,
        "course_id":        course_id,
        "status":           "pending_payment",
        "wallet_used":      0
    }).execute()

    # Course message — stays permanently until auto-delete
    sent = await message.answer_photo(
        photo=course["bot2_image_id"],
        caption=(
            f"\U0001f4d8 *{course['title']}*\n\n"
            f"{course['bot2_text']}\n\n"
            f"\U0001f4b5 *Price:* {course['price']}\n\n"
            f"\u23f1 _This payment window closes in 15 minutes._"
        ),
        reply_markup=_build_course_keyboard(course_id, wallet),
        parse_mode="Markdown"
    )
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id, AUTO_DELETE_SECS))


# ── Buy Now — send a SEPARATE payment options message, course msg untouched ────

@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_methods(callback: types.CallbackQuery):
    course_id = callback.data.split("buy_", 1)[1]
    # Do NOT touch the course message — send a brand new payment message
    sent = await bot.send_photo(
        chat_id=callback.from_user.id,
        photo=PAYMENT_OPTIONS_IMAGE,
        caption=(
            "\U0001f3e6 *Choose a Payment Method*\n\n"
            "Select how you'd like to pay below.\n"
            "After paying, send your payment screenshot here.\n\n"
            "\u23f1 _This window closes in 15 minutes._"
        ),
        reply_markup=_build_payment_options_keyboard(course_id),
        parse_mode="Markdown"
    )
    asyncio.create_task(_auto_delete(callback.from_user.id, sent.message_id, AUTO_DELETE_SECS))
    await callback.answer()


# ── Back to Course — delete the payment message only, course msg stays ─────────

@dp.callback_query(F.data.startswith("back_course_"))
async def back_to_course(callback: types.CallbackQuery):
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    await callback.answer()


# ── Back to Payment Options — edit image back in place ────────────────────────

@dp.callback_query(F.data.startswith("back_pay_"))
async def back_to_payment_options(callback: types.CallbackQuery):
    course_id = callback.data.split("back_pay_", 1)[1]
    await callback.message.edit_media(
        media=InputMediaPhoto(
            media=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "\U0001f3e6 *Choose a Payment Method*\n\n"
                "Select how you'd like to pay below.\n"
                "After paying, send your payment screenshot here.\n\n"
                "\u23f1 _This window closes in 15 minutes._"
            ),
            parse_mode="Markdown"
        ),
        reply_markup=_build_payment_options_keyboard(course_id)
    )
    await callback.answer()


# ── Refer & Pay — send user to Bot 1 referral program via deep link ────────────

@dp.callback_query(F.data.startswith("referpay_"))
async def show_refer_and_pay(callback: types.CallbackQuery):
    course_id = callback.data.split("_", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="\U0001f517  Open Referral Program",
            url=f"https://t.me/{BOT1_USERNAME}?start=refer"
        )],
        [InlineKeyboardButton(
            text="\u2b05\ufe0f  Back to Payment Options",
            callback_data=f"back_pay_{course_id}"
        )]
    ])
    await callback.message.edit_caption(
        caption=(
            "\U0001f381 *Refer & Pay*\n\n"
            "Earn wallet credits by sharing your referral link!\n\n"
            f"When a friend joins through your link and buys a course, "
            f"you earn *{REFERRAL_PERCENT}%* of their purchase straight to your wallet.\n\n"
            "Tap the button below to open the full referral program in the main bot \u2014 "
            "grab your link, check your balance, and start earning! \U0001f4b8"
        ),
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


# ── Individual payment method screens — edit image in place ───────────────────

PAYMENT_METHODS = {
    "qr": {
        "text": (
            "\U0001f4f7 *QR Code Payment*\n\n"
            "Scan the QR code above to complete your payment.\n\n"
            "\U0001f4f8 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "\u23f1 _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
    },
    "paytm": {
        "text": (
            "\U0001f7e3 *Paytm / UPI Payment*\n\n"
            "Send payment to the UPI ID below:\n\n"
            "\U0001f511 UPI ID: `womp@ptyes`\n\n"
            "\U0001f4f8 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "\u23f1 _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
    },
    "paypal": {
        "text": (
            "\U0001f535 *PayPal Payment*\n\n"
            "Send payment to the email below:\n\n"
            "\U0001f4e7 Email: `Ankitmallick5790@gmail.com`\n\n"
            "\U0001f4f8 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "\u23f1 _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
    },
    "crypto": {
        "text": (
            "\U0001f7e0 *Crypto Payment \u2014 USDT (BEP20)*\n\n"
            "Send USDT to the wallet address below:\n\n"
            "\U0001f45b Address:\n`0x1da04f30bdc147612a625b203217f50cdb84e2f6`\n\n"
            "\u26a0\ufe0f _Make sure you're sending on the BEP20 network!_\n\n"
            "\U0001f4f8 *Once paid:* send your payment screenshot right here in this chat.\n\n"
            "\u23f1 _This window closes in 15 minutes._"
        ),
        "image": "https://i.ibb.co/T5X40Ys/2a024034c5aa.jpg",
    },
    "others": {
        "text": (
            "\U0001f4ac *Other Payment Methods*\n\n"
            "Tap the button below to message the admin directly and arrange payment.\n\n"
            "\U0001f4f8 *Once paid:* send your payment screenshot right here in this chat."
        ),
        "image": "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg",
        "extra_buttons": [[InlineKeyboardButton(text="\U0001f464 Message Admin", url="https://t.me/ProSeller_69")]],
    },
}

@dp.callback_query(F.data.startswith("pay_"))
async def show_payment_details(callback: types.CallbackQuery):
    parts     = callback.data.split("_", 2)   # ["pay", "METHOD", "course_id"]
    method    = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""

    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)

    back_row = [InlineKeyboardButton(
        text="\u2b05\ufe0f  Back to Payment Options",
        callback_data=f"back_pay_{course_id}"
    )]
    extra    = info.get("extra_buttons", [])
    keyboard = InlineKeyboardMarkup(inline_keyboard=extra + [back_row])

    # Edit image and caption in-place — no delete, no new message
    await callback.message.edit_media(
        media=InputMediaPhoto(media=info["image"], caption=info["text"], parse_mode="Markdown"),
        reply_markup=keyboard
    )
    await callback.answer()


# ── Wallet redeem ──────────────────────────────────────────────────────────────

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
        return await callback.answer("\u26a0\ufe0f Your wallet is empty.", show_alert=True)

    discount   = min(wallet, numeric_price)
    amount_due = max(0.0, round(numeric_price - discount, 2))

    supabase.table("transactions").update({"wallet_used": discount}) \
        .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if amount_due == 0:
        # Fully covered by wallet
        _deduct_wallet(user_id, discount)

        latest_tx = supabase.table("transactions").select("id") \
            .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
        if latest_tx.data:
            supabase.table("transactions").update({"status": "approved"}) \
                .eq("id", latest_tx.data[-1]["id"]).execute()

        del_text    = course.get("delivery_text", "\u2705 Here is your course material.")
        del_file_id = course.get("delivery_file_id")

        if del_file_id:
            sent = await bot.send_document(
                chat_id=user_id, document=del_file_id,
                caption=f"{del_text}\n\n\u23f3 _This message self-destructs in 15 minutes._",
                parse_mode="Markdown"
            )
        else:
            sent = await bot.send_message(
                chat_id=user_id,
                text=f"{del_text}\n\n\u23f3 _This message self-destructs in 15 minutes._",
                parse_mode="Markdown", disable_web_page_preview=True
            )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

        referrer_id, credit = _pay_referrer(user_id, numeric_price)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"\U0001f4b8 *\u20b9{credit:.2f} added to your wallet!*\n\n"
                    f"One of your referrals just purchased *{course['title']}*.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=(
                "\u2705 *Payment Complete \u2014 Fully Paid with Wallet!*\n\n"
                "Your course has been delivered above. \U0001f393\n"
                "Enjoy the course!"
            ),
            parse_mode="Markdown"
        )

    else:
        # Partial — send a new payment message with the discount applied
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2b05\ufe0f  Back to Course", callback_data=f"back_course_{course_id}")]
        ])
        sent = await bot.send_photo(
            chat_id=user_id,
            photo=PAYMENT_OPTIONS_IMAGE,
            caption=(
                "\U0001f4b0 *Wallet Discount Applied!*\n\n"
                f"\u250c \U0001f3ab Wallet credit used:   *\u20b9{discount:.2f}*\n"
                f"\u2514 \U0001f4b5 Remaining to pay:     *\u20b9{amount_due:.2f}*\n\n"
                f"Please pay the remaining *\u20b9{amount_due:.2f}* using any payment method "
                f"and send your screenshot here.\n\n"
                "\u23f1 _This window closes in 15 minutes._"
            ),
            reply_markup=back_kb,
            parse_mode="Markdown"
        )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

    await callback.answer()


# ── Screenshot upload ──────────────────────────────────────────────────────────

@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id

    res = supabase.table("transactions").select("*") \
        .eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()

    if not res.data:
        return await message.answer(
            "\u26a0\ufe0f *No pending payment found.*\n\n"
            "Please open a course link first, then upload your screenshot.",
            parse_mode="Markdown"
        )

    transaction = res.data[-1]
    trans_id    = transaction["id"]
    wallet_used = float(transaction.get("wallet_used", 0))

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "\U0001f4f8 *Screenshot received!*\n\n"
        "The admin is reviewing your payment \u2014 this usually takes just a few minutes.\n"
        "You'll get a notification here once it's approved. \U0001f514",
        parse_mode="Markdown"
    )

    wallet_note = f"\n\U0001f4b0 *Wallet credit used:* \u20b9{wallet_used:.2f}" if wallet_used > 0 else ""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705  Approve", callback_data=f"approve_{trans_id}"),
        InlineKeyboardButton(text="\u274c  Reject",  callback_data=f"reject_{trans_id}")
    ]])

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"\U0001f4b3 *New Payment Screenshot*\n\n"
            f"\U0001f464 User ID: `{user_id}`\n"
            f"\U0001f4d8 Course: `{transaction['course_id']}`{wallet_note}"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# ── Admin approve / reject ─────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("\u26d4 Unauthorized.", show_alert=True)

    action, trans_id = callback.data.split("_", 1)

    res = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not res.data:
        return await callback.answer("Transaction not found.", show_alert=True)

    transaction = res.data[0]

    # Guard: already processed
    if transaction["status"] in ("approved", "rejected"):
        await callback.answer("\u26a0\ufe0f Already processed \u2014 this payment was already handled.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    # Remove buttons immediately to prevent double-tap
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
        del_text      = course.get("delivery_text", "\u2705 Payment verified! Here is your course material.")
        del_file_id   = course.get("delivery_file_id")

        if del_file_id:
            sent = await bot.send_document(
                chat_id=user_id, document=del_file_id,
                caption=f"{del_text}\n\n\u23f3 _This message self-destructs in 15 minutes._",
                parse_mode="Markdown"
            )
        else:
            sent = await bot.send_message(
                chat_id=user_id,
                text=f"{del_text}\n\n\u23f3 _This message self-destructs in 15 minutes._",
                parse_mode="Markdown", disable_web_page_preview=True
            )
        asyncio.create_task(_auto_delete(user_id, sent.message_id, AUTO_DELETE_SECS))

        referrer_id, credit = _pay_referrer(user_id, numeric_price)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"\U0001f4b8 *\u20b9{credit:.2f} added to your wallet!*\n\n"
                    f"Your referral just purchased *{course['title']}*.\n\n"
                    f"[Check your wallet \u2192](https://t.me/{BOT1_USERNAME})",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n\u2705 *APPROVED & DELIVERED*",
            parse_mode="Markdown"
        )

    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()

        await bot.send_message(
            user_id,
            "\u274c *Payment could not be verified.*\n\n"
            "Please double-check your payment and re-upload your screenshot.\n"
            "If you need help, contact support.",
            parse_mode="Markdown"
        )
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n\u274c *REJECTED*",
            parse_mode="Markdown"
        )

    await callback.answer()


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("\u2705 Sales Bot starting\u2026")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.utils.keyboard import InlineKeyboardBuilder
from db import supabase

load_dotenv()

BOT_TOKEN             = os.getenv("BOT2_TOKEN")
ADMIN_ID              = int(os.getenv("ADMIN_ID"))
BOT1_USERNAME         = os.getenv("BOT1_USERNAME", "YourGatewayBot")
DUMP_CHAT_ID          = int(os.getenv("DUMP_CHAT_ID", "-1000000000000"))
AUTO_DELETE_SECS      = 900
REFERRAL_PERCENT      = 25
PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return round(float(row.data[0]["wallet_balance"]), 2) if row.data else 0.0

def _deduct_wallet(user_id: int, amount: float) -> bool:
    amount = round(amount, 2)
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not row.data:
        return False
    current = round(float(row.data[0]["wallet_balance"]), 2)
    if current < amount:
        return False
    new_bal = round(current - amount, 2)
    supabase.table("users").update({"wallet_balance": new_bal}).eq("telegram_user_id", user_id).execute()
    
    verify = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not verify.data:
        return False
    confirmed = round(float(verify.data[0]["wallet_balance"]), 2)
    if confirmed != new_bal:
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
    cr = supabase.table("courses").select("numeric_price").eq("course_id", course_id).execute()
    if not cr.data:
        return 0.0
    val = cr.data[0].get("numeric_price")
    return round(float(val), 2) if val is not None else 0.0

def _get_course_title(course_id: str) -> str:
    cr = supabase.table("courses").select("title").eq("course_id", course_id).execute()
    return cr.data[0]["title"] if cr.data else course_id

def _create_transaction(user_id: int, course_id: str) -> str:
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
    if course_price <= 0:
        return None, 0

    ref_row = supabase.table("referrals").select("*").eq("referred_user_id", buyer_id).execute()
    if not ref_row.data:
        return None, 0

    ref            = ref_row.data[0]
    referrer_id    = ref["referrer_id"]
    ref_id         = ref["id"]
    current_status = ref.get("status", "")

    if current_status == "purchased" or current_status != "joined":
        return None, 0

    credit = round(course_price * REFERRAL_PERCENT / 100, 2)

    supabase.table("referrals").update({
        "status":                 "purchased",
        "paid_on_transaction_id": str(transaction_id),
    }).eq("id", ref_id).execute()

    verify = supabase.table("referrals").select("status").eq("id", ref_id).execute()
    if not verify.data or verify.data[0].get("status") != "purchased":
        return None, 0

    _add_wallet(referrer_id, credit)

    try:
        supabase.table("referral_commissions").insert({
            "referrer_id":    referrer_id,
            "buyer_id":       buyer_id,
            "transaction_id": str(transaction_id),
            "course_id":      course_id,
            "course_price":   course_price,
            "commission_pct": REFERRAL_PERCENT,
            "commission_amt": credit,
        }).execute()
    except Exception:
        pass

    return referrer_id, credit

def _course_keyboard(course_id: str, wallet: float, price: float) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💳  Buy Now", callback_data=f"buy:{course_id}")]]
    
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
        [InlineKeyboardButton(text="⬅️  Back to Item",   callback_data=f"backcourse:{course_id}")],
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
    cr = supabase.table("courses").select("delivery_text, dump_message_ids").eq("course_id", course_id).execute()
    
    if not cr.data:
        await bot.send_message(user_id, "✅ Payment approved! Contact support for your materials.")
        return
        
    del_text = cr.data[0].get("delivery_text") or "✅ Payment verified! Here are your materials:"
    
    sent_text = await bot.send_message(
        chat_id=user_id, 
        text=f"{del_text}\n\n⏳ <i>These files will self-destruct in 15 minutes.</i>",
        parse_mode="HTML", 
        disable_web_page_preview=True
    )
    asyncio.create_task(_auto_delete(user_id, sent_text.message_id))

    dump_ids_str = cr.data[0].get("dump_message_ids")
    if dump_ids_str:
        message_ids = [m.strip() for m in dump_ids_str.split(",") if m.strip()]
        
        for msg_id in message_ids:
            try:
                sent_media = await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=DUMP_CHAT_ID,
                    message_id=int(msg_id)
                )
                asyncio.create_task(_auto_delete(user_id, sent_media.message_id))
                await asyncio.sleep(0.5) 
            except Exception as e:
                print(f"Failed to deliver message {msg_id} from dump channel: {e}")

async def _recovery_notifications(user_id: int, course_id: str, course_title: str):
    await asyncio.sleep(960) 
    check = supabase.table("transactions").select("status").eq(
        "telegram_user_id", user_id
    ).eq("course_id", course_id).in_("status", ["approved", "awaiting_approval"]).execute()
    
    if not check.data:
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="💳 Resume Purchase", callback_data=f"buy:{course_id}"))
        kb.row(InlineKeyboardButton(text="💬 Need Help? Contact Admin", url="https://t.me/YourAdminUsername"))
        
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"👋 <b>Still interested in {course_title}?</b>\n\n"
                "We noticed you didn't finish your checkout. If you had any trouble with the "
                "payment methods or have questions, feel free to reach out to our support team!"
            ),
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )

        await asyncio.sleep(86400)
        check_final = supabase.table("transactions").select("status").eq(
            "telegram_user_id", user_id
        ).eq("course_id", course_id).in_("status", ["approved", "awaiting_approval"]).execute()
        
        if not check_final.data:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    f"✨ <b>Last call for {course_title}!</b>\n\n"
                    "The private access link is still available for you. "
                    "Don't miss out!"
                ),
                reply_markup=kb.as_markup(),
                parse_mode="HTML"
            )

# ══════════════════════════════════════════════════════════════════════════════
# /start — COURSE/BUNDLE DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = (command.args or "").strip()
    user_id   = message.from_user.id

    if not course_id:
        return await message.answer("⚠️ Please use a valid link to start.")

    res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    if not res.data:
        return await message.answer("❌ Item not found or the link is invalid.")
        
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
    asyncio.create_task(_recovery_notifications(user_id, course_id, course["title"]))

# ══════════════════════════════════════════════════════════════════════════════
# BACK TO ITEM
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
# PAYMENT METHOD DETAIL
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
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/YourAdminUsername")]
        ],
    },
}

@dp.callback_query(F.data.startswith("pay:"))
async def payment_method_intercept(callback: types.CallbackQuery):
    parts     = callback.data.split(":", 2)
    method    = parts[1] if len(parts) > 1 else ""
    course_id = parts[2] if len(parts) > 2 else ""
    
    await _show_payment_detail(callback, method, course_id)

async def _show_payment_detail(callback: types.CallbackQuery, method: str, course_id: str):
    info = PAYMENT_METHODS.get(method)
    if not info:
        return await callback.answer("Unknown payment method.", show_alert=True)
    
    res = supabase.table("courses").select("price").eq("course_id", course_id).execute()
    price_line = f"💵 <b>Amount:</b> {res.data[0]['price']}" if res.data else "💵 <b>Amount:</b> ?"
        
    caption  = f"{info['text']}\n\n{price_line}"
    back_row = [InlineKeyboardButton(text="⬅️  Back to Payment Options", callback_data=f"backpay:{course_id}")]
    extra    = info.get("extra_buttons", [])
    
    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=info["image"], caption=caption, parse_mode="HTML"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=extra + [back_row])
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

    course_price = _get_course_price(course_id)
    course_title = _get_course_title(course_id)

    if course_price <= 0:
        return await callback.answer("❌ This item has no price set. Contact admin.", show_alert=True)

    wallet = _get_wallet(user_id)

    if wallet < course_price:
        shortage = round(course_price - wallet, 2)
        return await callback.answer(
            f"❌ Insufficient wallet balance!\n\n"
            f"Your balance:  ₹{wallet:.2f}\n"
            f"Item price:    ₹{course_price:.2f}\n"
            f"You need:      ₹{shortage:.2f} more\n\n"
            f"Refer more friends to earn credits!",
            show_alert=True
        )

    tx = _get_pending_tx(user_id, course_id)
    if tx is None:
        return await callback.answer("⚠️ Session expired. Please open the link again.", show_alert=True)

    if round(float(tx.get("wallet_used") or 0), 2) > 0:
        return await callback.answer("⏳ Your wallet payment is already pending admin approval.\nPlease wait.", show_alert=True)

    supabase.table("transactions").update({
        "wallet_used":  course_price,
        "amount_paid":  course_price,
        "status":       "awaiting_approval",
        "payment_type": "wallet",
    }).eq("id", tx["id"]).execute()

    await callback.message.edit_caption(
        caption=(
            "💰 <b>Wallet Payment Request Sent!</b>\n\n"
            f"📘 Item: <b>{course_title}</b>\n"
            f"💸 Amount: <b>₹{course_price:.2f}</b>\n\n"
            "⏳ Waiting for admin approval.\n"
            "Your wallet will be deducted only after approval.\n"
            "You'll get a notification once it's confirmed. 🔔"
        ),
        parse_mode="HTML"
    )

    await bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            "💰 <b>Wallet Purchase Request</b>\n\n"
            f"👤 User ID:       <code>{user_id}</code>\n"
            f"📘 Item:          <code>{course_id}</code>\n"
            f"📛 Title:         {course_title}\n"
            f"💸 Amount:        <b>₹{course_price:.2f}</b>\n"
            f"🏦 User balance:  <b>₹{wallet:.2f}</b>\n\n"
            "<i>(No screenshot — paying from referral wallet balance)</i>\n\n"
            "✅ Approve = deduct wallet + deliver item\n"
            "❌ Reject = cancel, nothing is deducted"
        ),
        reply_markup=_admin_keyboard(tx["id"]),
        parse_mode="HTML"
    )

    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# SCREENSHOT UPLOAD
# ══════════════════════════════════════════════════════════════════════════════

@dp.message(F.photo)
async def handle_screenshot(message: types.Message):
    user_id = message.from_user.id
    res = supabase.table("transactions").select("*").eq("telegram_user_id", user_id).eq("status", "pending_payment").order("id", desc=True).limit(1).execute()

    if not res.data:
        return await message.answer(
            "⚠️ <b>No pending payment found.</b>\n\n"
            "Please open a course/bundle link first, then upload your screenshot.",
            parse_mode="HTML"
        )

    tx        = res.data[0]
    trans_id  = tx["id"]
    course_id = tx["course_id"]
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer(
        "📸 <b>Screenshot received!</b>\n\n"
        "Admin is reviewing your payment — usually just a few minutes.\n"
        "You'll get a notification once approved. 🔔",
        parse_mode="HTML"
    )

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 <b>New Payment Screenshot</b>\n\n"
            f"👤 User:         @{message.from_user.username or str(user_id)} (<code>{user_id}</code>)\n"
            f"📘 Item:         <b>{_get_course_title(course_id)}</b>\n"
            f"🔑 Item ID:      <code>{course_id}</code>\n"
            f"💵 Price:        <b>₹{course_price:.2f}</b>\n"
            f"🏦 User Wallet:  <b>₹{_get_wallet(user_id):.2f}</b>\n"
            f"🔖 Tx ID:        <code>{trans_id}</code>"
        ),
        reply_markup=_admin_keyboard(trans_id),
        parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN APPROVE / REJECT
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    action, trans_id_str = callback.data.split("_", 1)
    
    res = supabase.table("transactions").select("*").eq("id", trans_id_str).execute()
    if not res.data:
        return await callback.answer("❌ Transaction not found.", show_alert=True)
    tx = res.data[0]

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
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if action == "approve":
        if payment_type == "wallet" and wallet_used > 0:
            if not _deduct_wallet(user_id, wallet_used):
                supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id_str).execute()
                await bot.send_message(
                    user_id,
                    "❌ <b>Wallet payment failed.</b>\n\n"
                    "Your balance was insufficient at approval time.",
                    parse_mode="HTML"
                )
                try:
                    await callback.message.edit_caption(
                        caption=(callback.message.caption or "") + "\n\n❌ <b>REJECTED — WALLET INSUFFICIENT</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return await callback.answer("❌ Wallet insufficient — auto-rejected.", show_alert=True)

        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id_str).execute()
        await _deliver_course(user_id, course_id)
        
        referrer_id, credit = _pay_referrer(user_id, course_price, trans_id_str, course_id)
        if referrer_id:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>Referral Commission Earned!</b>\n\n"
                    f"💸 <b>₹{credit:.2f}</b> added to wallet!\n"
                    f"📘 Item: <b>{_get_course_title(course_id)}</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        suffix  = f"\n\n✅ <b>APPROVED & DELIVERED</b>\n📘 Item: {_get_course_title(course_id)}\n💵 Price: ₹{course_price:.2f}"
        if wallet_used > 0:
            suffix += f"\n💸 Wallet deducted: ₹{wallet_used:.2f}\n🏦 User new balance: ₹{_get_wallet(user_id):.2f}"
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

    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id_str).execute()
        
        if payment_type == "wallet":
            msg = "❌ <b>Wallet payment request rejected.</b>\n\nNo money was deducted."
        else:
            msg = "❌ <b>Payment could not be verified.</b>\n\nPlease re-upload your screenshot."
            
        await bot.send_message(user_id, msg, parse_mode="HTML")
        
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

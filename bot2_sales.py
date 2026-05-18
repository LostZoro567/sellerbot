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
DUMP_CHAT_ID          = int(os.getenv("DUMP_CHAT_ID", "-1003913013704"))
AUTO_DELETE_SECS      = 900    # 15 min — payment windows
DELIVERY_DELETE_SECS  = 3600   # 1 hour — delivered files (gives users time to save)
REFERRAL_PERCENT      = 25
PAYMENT_OPTIONS_IMAGE = "https://i.ibb.co/hRNCTGZc/x.jpg"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# Per-user recovery task tracker — cancels old task when user browses a new course
_pending_recovery: dict[int, asyncio.Task] = {}

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
        [InlineKeyboardButton(text="🧾 QR Code ",        callback_data=f"pay:qr:{course_id}")],
        [InlineKeyboardButton(text="💸 Paytm / UPI",    callback_data=f"pay:paytm:{course_id}")],
        [InlineKeyboardButton(text="🌐 PayPal",          callback_data=f"pay:paypal:{course_id}")],
        [InlineKeyboardButton(text="🪙 Crypto (USDT)",   callback_data=f"pay:crypto:{course_id}")],
        [InlineKeyboardButton(text="💳 Other Methods",   callback_data=f"pay:others:{course_id}")],
        [InlineKeyboardButton(text="🎁 Refer & Pay",    url=f"https://t.me/{BOT1_USERNAME}?start=refer")],
        [InlineKeyboardButton(text="⬅️ Back",   callback_data=f"backcourse:{course_id}")],
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

    # 1. Warning first so user sees it before files appear
    await bot.send_message(
        chat_id=user_id,
        text=(
            "⚠️ <b>WARNING: Self-Destructing Files</b>\n\n"
            "The files below will be <b>automatically deleted in 1 hour</b>. "
            "Please forward them to your <b>Saved Messages</b> or download them immediately.\n\n"
            "<i>Lost access after deletion? Contact our support team.</i>"
        ),
        parse_mode="HTML"
    )

    # 2. Send the intro text
    sent_text = await bot.send_message(
        chat_id=user_id,
        text=del_text,
        parse_mode="HTML",
        disable_web_page_preview=True
    )
    asyncio.create_task(_auto_delete(user_id, sent_text.message_id, DELIVERY_DELETE_SECS))

    # 3. Copy the files
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
                asyncio.create_task(_auto_delete(user_id, sent_media.message_id, DELIVERY_DELETE_SECS))
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Failed to deliver message {msg_id} from dump channel: {e}")

async def _recovery_notifications(user_id: int, course_id: str, course_title: str):
    try:
        await asyncio.sleep(960)
        check = supabase.table("transactions").select("status").eq(
            "telegram_user_id", user_id
        ).eq("course_id", course_id).in_("status", ["approved", "awaiting_approval"]).execute()

        if not check.data:
            kb = InlineKeyboardBuilder()
            kb.row(InlineKeyboardButton(text="💳 Resume Purchase", callback_data=f"buy:{course_id}"))
            kb.row(InlineKeyboardButton(text="💬 Need Help? Contact Admin", url="https://t.me/ProSeller_69"))

            sent = await bot.send_message(
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
                sent2 = await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✨ <b>Last call for {course_title}!</b>\n\n"
                        "The private access link is still available for you. "
                        "Don't miss out!"
                    ),
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML"
                )

    except asyncio.CancelledError:
        # User opened a different course — old notification task silently cancelled
        pass

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
        parse_mode="HTML",
        protect_content=True
    )
    
    asyncio.create_task(_auto_delete(message.chat.id, sent.message_id))

    # Cancel any previous recovery task for this user (they were just browsing)
    old_task = _pending_recovery.pop(user_id, None)
    if old_task and not old_task.done():
        old_task.cancel()
    task = asyncio.create_task(_recovery_notifications(user_id, course_id, course["title"]))
    _pending_recovery[user_id] = task

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
# BUY NOW & 1-CLICK UPSELL
# ══════════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("buy:"))
async def upsell_interceptor(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    user_id   = callback.from_user.id
    
    # 1. Skip upsell ONLY if they are already buying the ultimate "bundle_all"
    if course_id == "bundle_all":
        return await _show_payment_options(callback, course_id)
        
    # 2. Fetch the "bundle_all" details from the database
    res = supabase.table("courses").select("title, price").eq("course_id", "bundle_all").execute()
    
    # 3. If "bundle_all" hasn't been created yet, skip the upsell
    if not res.data:
        return await _show_payment_options(callback, course_id)
        
    bundle_all = res.data[0]
    bundle_price = bundle_all.get("price", "₹1499")
    bundle_title = bundle_all.get("title", "The Ultimate Collection")
    
    # 4. Create the side-by-side Yes/No buttons
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="❌ No", callback_data=f"continue_pay:{course_id}"),
        InlineKeyboardButton(text="✅ Yes", callback_data="upgrade_to_all")
    )
    
    # 5. SEND THE UPSELL OFFER
    sent_msg = await bot.send_message(
        chat_id=user_id,
        text=(
            "<b>Special Offer Available ✅</b>\n\n"
            f"Instead of buying just one item, you can unlock <b>{bundle_title}</b> with ALL our files for only <b>{bundle_price}</b>!\n\n𝐑𝐞𝐠𝐮𝐥𝐚𝐫 𝐏𝐫𝐢𝐜𝐞 : <del>3,999₹ / 60$</del>\n𝐎𝐟𝐟𝐞𝐫 𝐏𝐫𝐢𝐜𝐞 : 1,499₹ / 22$\n\n"
            "Would you like to Buy All VIP Files?"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    asyncio.create_task(_auto_delete(user_id, sent_msg.message_id))
    await callback.answer()

@dp.callback_query(F.data == "upgrade_to_all")
async def process_upgrade_to_all(callback: types.CallbackQuery):
    # Delete the Yes/No message so it doesn't clutter the chat
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    
    # Send them directly to the payment options for bundle_all
    await _show_payment_options(callback, "bundle_all")

@dp.callback_query(F.data.startswith("continue_pay:"))
async def bypass_upsell(callback: types.CallbackQuery):
    course_id = callback.data.split(":", 1)[1]
    
    # Delete the Yes/No message
    await _safe_delete(callback.message.chat.id, callback.message.message_id)
    
    # Show the payment options directly for the original item
    await _show_payment_options(callback, course_id)

async def _show_payment_options(callback: types.CallbackQuery, course_id: str):
    user_id = callback.from_user.id
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
                "Select how you'd like to pay below.\n\n"
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
                    "Select how you'd like to pay below.\n\n"
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
            "🧾 <b>QR Code Payment</b>\n\n"
            "Scan the QR code above to complete your payment.\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg",
    },
    "paytm": {
        "text": (
            "💸 <b>Paytm / UPI Payment</b>\n\n"
            "Send payment to the UPI ID below:\n\n"
            "🔑 UPI ID: <code>womp@ptyes</code>\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg",
    },
    "paypal": {
        "text": (
            "🌐 <b>PayPal Payment</b>\n\n"
            "Send payment to:\n\n"
            "📧 <code>Ankitmallick5790@gmail.com</code>\n\n"
            "📸 <b>Once paid:</b> send your payment screenshot right here.\n\n"
            "⏳ <i>Window closes in 15 minutes.</i>"
        ),
        "image": "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg",
    },
    "crypto": {
        "text": (
            "🪙 <b>Crypto Payment — USDT (BEP20)</b>\n\n"
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
            "💳 <b>Other Payment Methods</b>\n\n"
            "Message the admin directly for other payment methods.\n\n"
        ),
        "image": "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg",
        "extra_buttons": [
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/ProSeller_69")]
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

    tx           = res.data[0]
    trans_id     = tx["id"]
    course_id    = tx["course_id"]
    course_price = round(float(tx.get("amount_paid") or 0), 2)
    if course_price <= 0:
        course_price = _get_course_price(course_id)

    # ── Already owns this course? Warn admin but still forward ───────────────
    already_owned = supabase.table("transactions").select("id").eq(
        "telegram_user_id", user_id
    ).eq("course_id", course_id).eq("status", "approved").execute()
    already_owned_warning = ""
    if already_owned.data:
        already_owned_warning = "\n\n♻️ <b>NOTE: User already owns this course — likely lost access to files.</b>"

    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    # Auto-reject if admin doesn't respond within 6 hours
    async def _approval_timeout(tid: str, uid: int):
        await asyncio.sleep(21600)  # 6 hours
        check = supabase.table("transactions").select("status").eq("id", tid).execute()
        if check.data and check.data[0]["status"] == "awaiting_approval":
            supabase.table("transactions").update({"status": "rejected"}).eq("id", tid).execute()
            try:
                await bot.send_message(
                    uid,
                    "⏰ <b>Payment review timed out.</b>\n\n"
                    "Your screenshot was not reviewed within 6 hours.\n"
                    "Please resubmit your payment screenshot to continue.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
    asyncio.create_task(_approval_timeout(str(trans_id), user_id))

    await message.answer(
        "✅ Payment screenshot received successfully.\n\n"
        "⏳ Approval Time:\n"
        "• Usually within 30–40 seconds\n\n"
        "➧ Note:\n"
        "During night time or busy hours (maybe I am sleeping 😴), approval may take longer.\n\n"
        "Thank you for your patience ❤️\n\n"
        "➧ Important:\n"
        "After receiving the files, please forward them to your Saved Messages.\n\n"
        "⚠️ Files are automatically deleted after 1 hour.",
        parse_mode="HTML"
    )

    # ── Build purchase history for fraud cross-check ───────────────────────
    history_rows = supabase.table("transactions").select("course_id, status, amount_paid").eq(
        "telegram_user_id", user_id
    ).in_("status", ["approved", "awaiting_approval", "redelivered"]).execute().data

    if history_rows:
        history_lines = []
        for h in history_rows:
            status_icon = {"approved": "✅", "awaiting_approval": "⏳", "redelivered": "♻️"}.get(h["status"], "❓")
            amt = f"₹{float(h.get('amount_paid') or 0):.0f}"
            is_current = "  ← <b>this</b>" if h["course_id"] == course_id and h["status"] == "awaiting_approval" else ""
            history_lines.append(f"  {status_icon} <code>{h['course_id']}</code>  {amt}{is_current}")
        history_block = "\n\n🗂 <b>Purchase History:</b>\n" + "\n".join(history_lines)
    else:
        history_block = "\n\n🗂 <b>Purchase History:</b> First purchase"

    # Check for other awaiting_approval transactions for same course (duplicate screenshots)
    duplicate_pending = supabase.table("transactions").select("id").eq(
        "telegram_user_id", user_id
    ).eq("course_id", course_id).eq("status", "awaiting_approval").neq("id", trans_id).execute()
    fraud_warning = ""
    if duplicate_pending.data:
        fraud_warning = (
            "\n\n🚨 <b>DUPLICATE ALERT:</b> This user already has "
            f"<b>{len(duplicate_pending.data)}</b> other pending screenshot(s) "
            "for this same course. Likely resubmitting."
        )

    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=(
            f"💳 <b>New Payment Screenshot</b>\n\n"
            f"👤 User:    @{message.from_user.username or str(user_id)} (<code>{user_id}</code>)\n"
            f"📘 Item:    <b>{_get_course_title(course_id)}</b>\n"
            f"🔑 ID:      <code>{course_id}</code>\n"
            f"💵 Price:   <b>₹{course_price:.2f}</b>\n"
            f"🏦 Wallet:  <b>₹{_get_wallet(user_id):.2f}</b>\n"
            f"🔖 Tx ID:   <code>{trans_id}</code>"
            f"{already_owned_warning}"
            f"{history_block}"
            f"{fraud_warning}"
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

    # Remove buttons instantly so you cannot double-click
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

        # ── Duplicate guard: already approved for this course? ────────────────
        already_approved = supabase.table("transactions").select("id").eq(
            "telegram_user_id", user_id
        ).eq("course_id", course_id).eq("status", "approved").execute()

        if already_approved.data:
            await callback.answer("⚠️ Already purchased — re-delivering, stats unchanged.")
            supabase.table("transactions").update({"status": "redelivered"}).eq("id", trans_id_str).execute()
            await _deliver_course(user_id, course_id)
            try:
                await callback.message.edit_caption(
                    caption=(callback.message.caption or "") +
                    "\n\n♻️ <b>RE-DELIVERED — User already owned this course.</b>\n"
                    "<i>Not counted in sales stats.</i>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        await callback.answer("✅ Approving and delivering files... (This may take a moment)")

        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id_str).execute()

        # Now the bot takes its time safely delivering files
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

    elif action == "reject":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Fake Payment",         callback_data=f"rr_fake_{trans_id_str}")],
            [InlineKeyboardButton(text="📁 Wrong Files Selected", callback_data=f"rr_wrong_{trans_id_str}")],
            [InlineKeyboardButton(text="✅ Already Approved",     callback_data=f"rr_dupe_{trans_id_str}")],
        ])
        await callback.answer("Select a reject reason below.")
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"🔴 <b>Rejecting Tx</b> <code>{trans_id_str}</code>\n\nSelect reason to send to user:",
            reply_markup=kb,
            reply_to_message_id=callback.message.message_id,
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("rr_"))
async def reject_reason_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    # rr_<reason>_<trans_id>
    parts        = callback.data.split("_", 2)
    reason_key   = parts[1]
    trans_id_str = parts[2]

    res = supabase.table("transactions").select("*").eq("id", trans_id_str).execute()
    if not res.data:
        return await callback.answer("❌ Transaction not found.", show_alert=True)
    tx      = res.data[0]
    user_id = tx["telegram_user_id"]

    reason_map = {
        "fake":  ("💸 Fake Payment",         "Your payment screenshot could not be verified. If this is a mistake, please contact support."),
        "wrong": ("📁 Wrong Files Selected",  "It looks like you selected the wrong course. Please go back, select the correct course, and resubmit."),
        "dupe":  ("✅ Already Approved",      "This payment was already approved previously. Your files were already delivered. Contact support if you lost access."),
    }
    reason_label, user_msg = reason_map.get(reason_key, ("Unknown", "Your payment was rejected. Please contact support."))

    supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id_str).execute()

    await bot.send_message(
        user_id,
        f"❌ <b>Payment Rejected — {reason_label}</b>\n\n"
        f"{user_msg}",
        parse_mode="HTML"
    )

    await callback.answer("✅ Rejected.")

    # Delete the reason picker message
    try:
        await callback.message.delete()
    except Exception:
        pass

    # Update the original photo caption (picker was sent as reply to it)
    original = callback.message.reply_to_message
    if original:
        try:
            new_caption = (original.caption or "") + f"\n\n❌ <b>REJECTED — {reason_label}</b>"
            await original.edit_caption(caption=new_caption, reply_markup=None, parse_mode="HTML")
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# ENTRY
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

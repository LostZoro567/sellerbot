import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from db import supabase

load_dotenv()

BOT_TOKEN = os.getenv("BOT2_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
AUTO_DELETE_SECONDS = 900  # 15 minutes

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- HELPER: Background Auto-Delete Task ---
async def auto_delete_message(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Failed to auto-delete message {message_id}: {e}")

# ==========================================
# STEP 1: USER ARRIVES & FETCHES COURSE
# ==========================================
@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    
    if not course_id:
        return await message.answer("Please start this bot using a valid course link.")

    response = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    
    if response.data:
        course = response.data[0]
        
        supabase.table("transactions").insert({
            "telegram_user_id": message.from_user.id,
            "course_id": course_id,
            "status": "pending_payment"
        }).execute()

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy Now", callback_data=f"buy_{course_id}")]
        ])

        sent_msg = await message.answer_photo(
            photo=course['bot2_image_id'],
            caption=f"📘 **{course['title']}**\n\n{course['bot2_text']}\n\n**Price:** {course['price']}\n\n⏳ *This payment window will auto-close in 15 minutes.*",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
        asyncio.create_task(auto_delete_message(message.chat.id, sent_msg.message_id, AUTO_DELETE_SECONDS))

    else:
        await message.answer("Course not found or invalid selection.")

# ==========================================
# STEP 1.5: MULTI-STEP PAYMENT MENU
# ==========================================
@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_methods(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 QR Code", callback_data="pay_qr")],
        [InlineKeyboardButton(text="🟣 Paytm / UPI", callback_data="pay_paytm")],
        [InlineKeyboardButton(text="🔵 PayPal", callback_data="pay_paypal")],
        [InlineKeyboardButton(text="🟠 Crypto (USDT)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="💬 Others", callback_data="pay_others")]
    ])
    
    await callback.message.edit_caption(
        caption="🏦 **Select your preferred payment method:**\n\n⏳ *This window auto-closes in 15 minutes.*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def show_specific_payment_details(callback: types.CallbackQuery):
    method = callback.data.split("_")[1]
    
    keyboard = None 
    
    # NOTE: Replace all fake Telegraph URLs and payment details with your actual ones!
    if method == "qr":
        text = "📷 **QR Code Selected**\n\nScan the QR code image to pay.\n\n📸 *Upload your screenshot into this chat after payment.*\n\n⏳ *Auto-closing soon.*"
        image_url = "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg"
        
    elif method == "paytm":
        text = "🟣 **Paytm / UPI Selected**\n\nSend payment to: `womp@ptyes`\n\n📸 *Upload your screenshot into this chat after payment.*\n\n⏳ *Auto-closing soon.*"
        image_url = "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg"
        
    elif method == "paypal":
        text = "🔵 **PayPal Selected**\n\nSend payment to: `Ankitmallick5790@gmail.com`\n\n📸 *Upload your screenshot into this chat after payment.*\n\n⏳ *Auto-closing soon.*"
        image_url = "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg"
        
    elif method == "crypto":
        text = "🟠 **Crypto (USDT BEP20) Selected**\n\nWallet Address: `0x1da04f30bdc147612a625b203217f50cdb84e2f6`\n\n📸 *Upload your screenshot into this chat after payment.*\n\n⏳ *Auto-closing soon.*"
        image_url = "https://i.ibb.co/T5X40Ys/2a024034c5aa.jpg"
        
    elif method == "others":
        text = "💬 **Other Payment Methods**\n\nFor alternative methods, please click the button below to message me directly.\n\n📸 *Once we agree on a payment and you pay, upload the screenshot right here.*"
        # You can use a picture of a headset/support icon for this one
        image_url = "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg" 
        
        # This generates a button linking straight to your personal Telegram DM!
        # Change 'YourPersonalUsername' to your actual Telegram handle (without the @ symbol)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/ProSeller_69")]
        ])

    new_media = InputMediaPhoto(
        media=image_url, 
        caption=text, 
        parse_mode="Markdown"
    )

    # We pass the keyboard here. If it's normal payment, it removes the buttons. 
    # If it's 'Others', it attaches the URL button.
    await callback.message.edit_media(media=new_media, reply_markup=keyboard)
    await callback.answer()

# ==========================================
# STEP 2: USER UPLOADS SCREENSHOT
# ==========================================
@dp.message(F.photo)
async def handle_payment_screenshot(message: types.Message):
    user_id = message.from_user.id
    
    response = supabase.table("transactions").select("*").eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
    
    if not response.data:
        return await message.answer("You don't have any pending payments. Please select a course first.")

    transaction = response.data[-1] 
    trans_id = transaction['id']
    
    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer("Payment screenshot received! Please wait while the admin verifies it.")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{trans_id}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{trans_id}")
        ]
    ])
    
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=message.photo[-1].file_id,
        caption=f"New payment from {user_id} for {transaction['course_id']}.",
        reply_markup=keyboard
    )

# ==========================================
# STEP 3: ADMIN VERIFIES & AUTO-DELIVERS
# ==========================================
@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    action, trans_id = callback.data.split("_")
    
    response = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not response.data:
        return await callback.answer("Transaction not found.")
        
    transaction = response.data[0]
    user_id = transaction['telegram_user_id']
    course_id = transaction['course_id']

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()
        
        course_response = supabase.table("courses").select("*").eq("course_id", course_id).execute()
        course = course_response.data[0]
        
        del_text = course.get('delivery_text', f"✅ Payment verified! Here is your access for {course_id}.")
        del_file_id = course.get('delivery_file_id')
        
        if del_file_id:
            course_delivery_msg = await bot.send_document(
                chat_id=user_id, 
                document=del_file_id, 
                caption=f"{del_text}\n\n⏳ *This message will self-destruct in 15 minutes.*",
                parse_mode="Markdown"
            )
        else:
            course_delivery_msg = await bot.send_message(
                chat_id=user_id, 
                text=f"{del_text}\n\n⏳ *This message will self-destruct in 15 minutes.*",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        
        asyncio.create_task(auto_delete_message(user_id, course_delivery_msg.message_id, AUTO_DELETE_SECONDS))
        
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ APPROVED & DELIVERED")
        
    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()
        await bot.send_message(user_id, "❌ Your payment could not be verified. Please contact support.")
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ REJECTED")

async def main():
    print("Starting Sales Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

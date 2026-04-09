import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import supabase

load_dotenv()

BOT_TOKEN = os.getenv("BOT2_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
AUTO_DELETE_SECONDS = 900  # 15 minutes = 900 seconds

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- HELPER: Background Auto-Delete Task ---
async def auto_delete_message(chat_id: int, message_id: int, delay: int):
    """Sleeps for 'delay' seconds, then deletes the message."""
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

        # Save the sent message as a variable so we know its ID
        sent_msg = await message.answer_photo(
            photo=course['bot2_image_id'],
            caption=f"📘 **{course['title']}**\n\n{course['bot2_text']}\n\n**Price:** {course['price']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
        # Start the 15-minute destruction timer in the background
        asyncio.create_task(auto_delete_message(message.chat.id, sent_msg.message_id, AUTO_DELETE_SECONDS))

    else:
        await message.answer("Course not found or invalid selection.")

# ==========================================
# STEP 1.5: MULTI-STEP PAYMENT MENU
# ==========================================
@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_methods(callback: types.CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 PayPal", callback_data="pay_paypal")],
        [InlineKeyboardButton(text="🟣 Paytm / UPI", callback_data="pay_paytm")],
        [InlineKeyboardButton(text="🟠 Crypto (USDT)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🏦 Bank Transfer", callback_data="pay_bank")]
    ])
    
    await callback.message.edit_caption(
        caption="🏦 **Select your preferred payment method:**",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def show_specific_payment_details(callback: types.CallbackQuery):
    method = callback.data.split("_")[1]
    
    if method == "paypal":
        text = "🔵 **PayPal Selected**\n\nSend payment to: `paypal.me/YourName`\n\n📸 *Upload your screenshot into this chat after payment.*"
    elif method == "paytm":
        text = "🟣 **Paytm / UPI Selected**\n\nSend payment to: `your_upi_id@ybl`\n\n📸 *Upload your screenshot into this chat after payment.*"
    elif method == "crypto":
        text = "🟠 **Crypto (USDT TRC20) Selected**\n\nWallet Address: `YourWalletAddressHere`\n\n📸 *Upload your screenshot into this chat after payment.*"
    elif method == "bank":
        text = "🏦 **Bank Transfer Selected**\n\nAccount: `123456789`\nIFSC: `ABCD0123`\n\n📸 *Upload your screenshot into this chat after payment.*"

    # NOTE: Because we are EDITING the original message, the 15-minute timer from Step 1 will still successfully delete this payment screen too!
    await callback.message.edit_caption(caption=text, parse_mode="Markdown")
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
# STEP 3: ADMIN VERIFIES & AUTO-DELETES DELIVERY
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
        
        # Save the delivered course message as a variable
        course_delivery_msg = await bot.send_message(user_id, f"✅ Payment verified! Here is your access link/file for {course_id}.")
        
        # Start the 15-minute destruction timer for the final delivery message
        asyncio.create_task(auto_delete_message(user_id, course_delivery_msg.message_id, AUTO_DELETE_SECONDS))
        
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n✅ APPROVED")
        
    elif action == "reject":
        supabase.table("transactions").update({"status": "rejected"}).eq("id", trans_id).execute()
        await bot.send_message(user_id, "❌ Your payment could not be verified. Please contact support.")
        await callback.message.edit_caption(caption=f"{callback.message.caption}\n\n❌ REJECTED")

async def main():
    print("Starting Sales Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

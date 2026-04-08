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

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================================
# STEP 1: USER ARRIVES & FETCHES COURSE
# ==========================================
@dp.message(CommandStart())
async def handle_course_selection(message: types.Message, command: CommandObject):
    course_id = command.args
    
    if not course_id:
        await message.answer("Please start this bot using a valid course link.")
        return

    # Fetch specific course details from DB
    response = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    
    if response.data:
        course = response.data[0]
        
        # Log the pending transaction
        supabase.table("transactions").insert({
            "telegram_user_id": message.from_user.id,
            "course_id": course_id,
            "status": "pending_payment"
        }).execute()

        # Create the Buy button
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Buy Now", callback_data=f"buy_{course_id}")]
        ])

        # Send dynamic content using the URL from the database
        await message.answer_photo(
            photo=course['bot2_image_id'],
            caption=f"📘 **{course['title']}**\n\n{course['bot2_text']}\n\n**Price:** {course['price']}",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    else:
        await message.answer("Course not found or invalid selection.")

# ==========================================
# STEP 1.5: USER CLICKS "BUY NOW"
# ==========================================
@dp.callback_query(F.data.startswith("buy_"))
async def show_payment_options(callback: types.CallbackQuery):
    # Edit these payment details to match your own
    payment_menu = (
        "🏦 **Select a Payment Method**\n\n"
        "1️⃣ **UPI / Paytm:** `your_upi_id@ybl`\n"
        "2️⃣ **PayPal:** `paypal.me/YourName`\n"
        "3️⃣ **Bank Transfer:** `Acc: 123456789 | IFSC: ABCD0123`\n"
        "4️⃣ **Crypto (USDT TRC20):** `YourWalletAddressHere`\n\n"
        "📸 *After sending the payment to one of the options above, please upload the screenshot directly into this chat.*"
    )
    
    await callback.message.edit_caption(
        caption=payment_menu,
        parse_mode="Markdown"
    )
    await callback.answer()

# ==========================================
# STEP 2: USER UPLOADS SCREENSHOT
# ==========================================
@dp.message(F.photo)
async def handle_payment_screenshot(message: types.Message):
    user_id = message.from_user.id
    
    # Check for pending transaction
    response = supabase.table("transactions").select("*").eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
    
    if not response.data:
        await message.answer("You don't have any pending payments or you haven't selected a course.")
        return

    transaction = response.data[-1] 
    trans_id = transaction['id']
    
    # Update status to awaiting approval
    supabase.table("transactions").update({"status": "awaiting_approval"}).eq("id", trans_id).execute()

    await message.answer("Payment screenshot received! Please wait while the admin verifies it.")

    # Forward to Admin
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
# STEP 3: ADMIN VERIFIES
# ==========================================
@dp.callback_query(F.data.startswith("approve_") | F.data.startswith("reject_"))
async def admin_decision(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    action, trans_id = callback.data.split("_")
    
    # Fetch transaction details
    response = supabase.table("transactions").select("*").eq("id", trans_id).execute()
    if not response.data:
        await callback.answer("Transaction not found.")
        return
        
    transaction = response.data[0]
    user_id = transaction['telegram_user_id']
    course_id = transaction['course_id']

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()
        
        # You can customize this message to send a specific file or link based on the course_id
        await bot.send_message(user_id, f"✅ Payment verified! Here is your access link/file for {course_id}.")
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

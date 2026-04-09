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
BOT1_USERNAME = os.getenv("BOT1_USERNAME", "YourGatewayBotUsername") # Fallback if missing
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
    payload = command.args
    
    if not payload:
        return await message.answer("Please start this bot using a valid course link.")

    course_id = payload
    referrer_id = None

    if "-ref" in payload:
        course_id, ref_str = payload.split("-ref")
        referrer_id = int(ref_str)

    response = supabase.table("courses").select("*").eq("course_id", course_id).execute()
    
    if response.data:
        course = response.data[0]
        user_id = message.from_user.id
        
        # Ensure user has a wallet profile
        user_res = supabase.table("users").select("*").eq("telegram_user_id", user_id).execute()
        if not user_res.data:
            supabase.table("users").insert({"telegram_user_id": user_id, "balance": 0}).execute()
        
        # Log pending transaction
        supabase.table("transactions").insert({
            "telegram_user_id": user_id,
            "course_id": course_id,
            "status": "pending_payment",
            "referrer_id": referrer_id
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
    course_id = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    user_res = supabase.table("users").select("balance").eq("telegram_user_id", user_id).execute()
    balance = float(user_res.data[0]['balance']) if user_res.data else 0
    
    course_res = supabase.table("courses").select("price_numeric").eq("course_id", course_id).execute()
    price_numeric = float(course_res.data[0]['price_numeric']) if course_res.data else 999999

    buttons = [
        [InlineKeyboardButton(text="📷 QR Code", callback_data="pay_qr")],
        [InlineKeyboardButton(text="🟣 Paytm / UPI", callback_data="pay_paytm")],
        [InlineKeyboardButton(text="🔵 PayPal", callback_data="pay_paypal")],
        [InlineKeyboardButton(text="🟠 Crypto (USDT)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="💬 Others", callback_data="pay_others")]
    ]
    
    # If they can afford it with wallet, slide this button to the top
    if balance >= price_numeric:
        buttons.insert(0, [InlineKeyboardButton(text="💰 Pay with Wallet", callback_data="pay_wallet")])
        
    # The stealth referral button
    buttons.append([InlineKeyboardButton(text="🎁 Can't afford it?", callback_data="pay_referral")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_caption(
        caption=f"🏦 **Select your preferred payment method:**\n💳 **Wallet Balance:** ₹{balance}\n\n⏳ *This window auto-closes in 15 minutes.*",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def show_specific_payment_details(callback: types.CallbackQuery):
    method = callback.data.split("_")[1]
    user_id = callback.from_user.id
    keyboard = None 
    
    if method == "wallet":
        trans_res = supabase.table("transactions").select("*").eq("telegram_user_id", user_id).eq("status", "pending_payment").execute()
        if not trans_res.data:
            return await callback.answer("No pending transaction found. Please restart.", show_alert=True)
            
        transaction = trans_res.data[-1]
        course_id = transaction['course_id']
        trans_id = transaction['id']
        
        course_res = supabase.table("courses").select("*").eq("course_id", course_id).execute()
        course = course_res.data[0]
        price_numeric = float(course['price_numeric'])
        
        user_res = supabase.table("users").select("balance").eq("telegram_user_id", user_id).execute()
        balance = float(user_res.data[0]['balance'])
        
        if balance >= price_numeric:
            new_balance = balance - price_numeric
            supabase.table("users").update({"balance": new_balance}).eq("telegram_user_id", user_id).execute()
            supabase.table("transactions").update({"status": "approved_wallet"}).eq("id", trans_id).execute()
            
            await callback.message.delete()
            
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
            return await callback.answer("✅ Paid with wallet! Course delivered.", show_alert=True)
        else:
            return await callback.answer("❌ Insufficient wallet balance.", show_alert=True)

    elif method == "referral":
        ref_link = f"https://t.me/{BOT1_USERNAME}?start=ref_{user_id}"
        text = (
            "🎁 **Earn This For Free!**\n\n"
            "Share your unique Gateway link below. When someone joins and buys ANY course, "
            "you instantly get **25%** of their purchase amount added to your wallet.\n\n"
            "Once your wallet balance covers the price, a '💰 Pay with Wallet' button will automatically appear here!\n\n"
            f"🔗 **Your Link:**\n`{ref_link}`\n\n"
            "⏳ *Auto-closing soon.*"
        )
        image_url = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg" # Change to a gift box image
        
    elif method == "qr":
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
        image_url = "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg" 
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Message Admin", url="https://t.me/ProSeller_69")]
        ])

    new_media = InputMediaPhoto(media=image_url, caption=text, parse_mode="Markdown")
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
# STEP 3: ADMIN VERIFIES & PAYOUT
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
    referrer_id = transaction.get('referrer_id')

    if action == "approve":
        supabase.table("transactions").update({"status": "approved"}).eq("id", trans_id).execute()
        
        course_response = supabase.table("courses").select("*").eq("course_id", course_id).execute()
        course = course_response.data[0]
        
        # --- REFERRAL PAYOUT LOGIC ---
        if referrer_id:
            price_num = float(course.get('price_numeric', 0))
            if price_num > 0:
                commission = price_num * 0.25
                ref_res = supabase.table("users").select("balance").eq("telegram_user_id", referrer_id).execute()
                
                if ref_res.data:
                    current_balance = float(ref_res.data[0]['balance'])
                    supabase.table("users").update({"balance": current_balance + commission}).eq("telegram_user_id", referrer_id).execute()
                    
                    try:
                        await bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 **Referral Bonus!**\n\nSomeone purchased a course using your link! **₹{commission}** has been added to your wallet."
                        )
                    except:
                        pass # Referrer might have blocked the bot
        # -----------------------------

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

import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError
from db import supabase

load_dotenv()

BOT_TOKEN = os.getenv("BOT1_TOKEN")
SECRET_CODE = os.getenv("SECRET_INVITE_CODE")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME = "ExclusiveCollectionVIP_bot" # Replace with your actual Bot 2 username (no @)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- FSM States ---
class AddCourseFSM(StatesGroup):
    waiting_for_course_id = State()
    waiting_for_title = State()
    waiting_for_price = State()
    waiting_for_price_numeric = State() # NEW STATE
    waiting_for_bot2_text = State()
    waiting_for_bot2_image = State()
    waiting_for_delivery_content = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()

# ==========================================
# USER FLOW: DYNAMIC LOBBY & REFERRALS
# ==========================================
@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    args = command.args or ""
    referrer_id = None
    is_authorized = False

    if args == SECRET_CODE:
        is_authorized = True
    elif args.startswith("ref_"):
        is_authorized = True
        referrer_id = args.split("_")[1]

    if is_authorized:
        response = supabase.table("courses").select("course_id, title").execute()
        courses = response.data
        
        builder = InlineKeyboardBuilder()
        for course in courses:
            payload = course['course_id']
            if referrer_id:
                payload += f"-ref{referrer_id}"
                
            builder.row(InlineKeyboardButton(
                text=f"📘 {course['title']}", 
                url=f"https://t.me/{BOT2_USERNAME}?start={payload}"
            ))
            
        await message.answer_photo(
            photo="https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg", 
            caption="Welcome to the private portal! Select a course below.",
            reply_markup=builder.as_markup()
        )
    else:
        await message.answer("Welcome 👋🏻")

# ==========================================
# ADMIN FLOW: ADD NEW COURSE
# ==========================================
@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠️ Let's add a new course.\n\nFirst, type a unique internal ID (e.g., course_7, python_basics):")
    await state.set_state(AddCourseFSM.waiting_for_course_id)

@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    await state.update_data(course_id=message.text.strip().lower())
    await message.answer("Got it. What is the display Title of the course? (e.g., Master Python 2024)")
    await state.set_state(AddCourseFSM.waiting_for_title)

@dp.message(AddCourseFSM.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Great. What is the display price text? (e.g., ₹400 or $15)")
    await state.set_state(AddCourseFSM.waiting_for_price)

@dp.message(AddCourseFSM.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await message.answer("Now, enter the EXACT NUMERIC price (e.g., 400). This will be used by the bot to mathematically calculate the 25% referral commissions.")
    await state.set_state(AddCourseFSM.waiting_for_price_numeric)

@dp.message(AddCourseFSM.waiting_for_price_numeric)
async def process_price_numeric(message: types.Message, state: FSMContext):
    try:
        price_num = float(message.text)
        await state.update_data(price_numeric=price_num)
        await message.answer("Now, type the description text that Bot 2 will send when the user wants to buy this course:")
        await state.set_state(AddCourseFSM.waiting_for_bot2_text)
    except ValueError:
        await message.answer("Please enter a valid pure number (e.g., 400 or 15).")

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text)
    await message.answer("Almost done! Paste a **Public Image URL** (Telegraph link) for the course display.\n\nExample: `https://telegra.ph/file/abcd123.jpg`")
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    image_url = message.text.strip()
    await state.update_data(bot2_image_id=image_url)
    
    await message.answer("Final Step! 🎁\n\nSend the actual course material the user will receive after buying.\n\nYou can send a **Text Message** (with Google Drive links/passwords) OR upload a **Single File** (like a .zip or .pdf) with a caption.")
    await state.set_state(AddCourseFSM.waiting_for_delivery_content)

@dp.message(AddCourseFSM.waiting_for_delivery_content)
async def process_delivery_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    delivery_text = message.text or message.caption or "✅ Payment verified! Here is your course material."
    
    delivery_file_id = None
    if message.document:
        delivery_file_id = message.document.file_id
    elif message.video:
        delivery_file_id = message.video.file_id
        
    try:
        supabase.table("courses").insert({
            "course_id": data['course_id'],
            "title": data['title'],
            "price": data['price'],
            "price_numeric": data['price_numeric'],
            "bot2_text": data['bot2_text'],
            "bot2_image_id": data['bot2_image_id'],
            "delivery_text": delivery_text,
            "delivery_file_id": delivery_file_id
        }).execute()
        
        await message.answer(f"✅ Success! Course '{data['title']}' has been added to the database and is ready to sell.")
    except Exception as e:
        await message.answer(f"❌ Error saving to database: {e}")
        
    await state.clear()

# ==========================================
# ADMIN TOOL: BROADCAST & AUTO-CLEANUP
# ==========================================
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("📢 **Broadcast Mode Started**\n\nSend the message (Text, Photo, or Video) that you want to send to all users. \n\n*Note: Dead accounts will be automatically cleaned from the database.*")
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def execute_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("⏳ Broadcast initiating... collecting user data.")

    response = supabase.table("transactions").select("telegram_user_id").execute()
    unique_users = set([row['telegram_user_id'] for row in response.data])

    success_count = 0
    fail_count = 0

    for user_id in unique_users:
        try:
            await message.copy_to(chat_id=user_id)
            success_count += 1
            await asyncio.sleep(0.05) 
        except TelegramForbiddenError:
            fail_count += 1
            supabase.table("transactions").delete().eq("telegram_user_id", user_id).execute()
        except Exception:
            fail_count += 1

    await status_msg.edit_text(
        f"✅ **Broadcast & Cleanup Complete!**\n\n"
        f"📢 Successfully delivered to: **{success_count}** users\n"
        f"🗑️ Dead accounts deleted from DB: **{fail_count}**"
    )

async def main():
    print("Starting Gateway Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

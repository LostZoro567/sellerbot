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
BOT2_USERNAME = "ExclusiveCollectionVIP_bot" # Replace with your actual Bot 2 username

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- FSM States ---
class AddCourseFSM(StatesGroup):
    waiting_for_course_id = State()
    waiting_for_title = State()
    waiting_for_price = State()
    waiting_for_bot2_text = State()
    waiting_for_bot2_image = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()

# ==========================================
# USER FLOW: DYNAMIC LOBBY
# ==========================================
@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    if command.args == SECRET_CODE:
        response = supabase.table("courses").select("course_id, title").execute()
        courses = response.data
        
        builder = InlineKeyboardBuilder()
        for course in courses:
            builder.row(InlineKeyboardButton(
                text=f"📘 {course['title']}", 
                url=f"https://t.me/{BOT2_USERNAME}?start={course['course_id']}"
            ))
            
        await message.answer_photo(
            photo="https://telegra.ph/file/your_welcome_image.jpg", 
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
    await message.answer("Great. What is the price? (e.g., ₹400 or $15)")
    await state.set_state(AddCourseFSM.waiting_for_price)

@dp.message(AddCourseFSM.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text)
    await message.answer("Now, type the description text that Bot 2 will send when the user wants to buy this course:")
    await state.set_state(AddCourseFSM.waiting_for_bot2_text)

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text)
    await message.answer("Almost done! Finally, paste a **Public Image URL** (like a Telegraph link).\n\nExample: `https://telegra.ph/file/abcd123.jpg`")
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    image_url = message.text.strip()
    data = await state.get_data()
    try:
        supabase.table("courses").insert({
            "course_id": data['course_id'],
            "title": data['title'],
            "price": data['price'],
            "bot2_text": data['bot2_text'],
            "bot2_image_id": image_url
        }).execute()
        await message.answer(f"✅ Success! Course '{data['title']}' has been added to the database.")
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

    # Get all unique users from the transactions table
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

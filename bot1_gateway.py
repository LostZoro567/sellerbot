import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from db import supabase

load_dotenv()

BOT_TOKEN = os.getenv("BOT1_TOKEN")
SECRET_CODE = os.getenv("SECRET_INVITE_CODE")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME = "YourBot2Username" # Replace with your actual Bot 2 username (without the @)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- FSM States for Admin Panel ---
class AddCourseFSM(StatesGroup):
    waiting_for_course_id = State()
    waiting_for_title = State()
    waiting_for_price = State()
    waiting_for_bot2_text = State()
    waiting_for_bot2_image = State()

# ==========================================
# USER FLOW: DYNAMIC LOBBY
# ==========================================
@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    if command.args == SECRET_CODE:
        # Fetch all active courses from Supabase
        response = supabase.table("courses").select("course_id, title").execute()
        courses = response.data
        
        # Build the inline keyboard dynamically
        builder = InlineKeyboardBuilder()
        for course in courses:
            builder.row(InlineKeyboardButton(
                text=f"📘 {course['title']}", 
                url=f"https://t.me/{BOT2_USERNAME}?start={course['course_id']}"
            ))
            
        await message.answer_photo(
            photo="https://example.com/welcome_image.jpg", # Replace with actual URL or file ID
            caption="Welcome to the private portal! Select a course below.",
            reply_markup=builder.as_markup()
        )
    else:
        await message.answer("Access Restricted. Please use an official invite link.")

# ==========================================
# ADMIN FLOW: ADD NEW COURSE
# ==========================================
@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return # Ignore non-admins silently
        
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
    await message.answer("Almost done! Finally, send the Image (Photo) that Bot 2 should display with the text.")
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image, F.photo)
async def process_bot2_image(message: types.Message, state: FSMContext):
    # Get highest resolution photo file_id
    file_id = message.photo[-1].file_id 
    data = await state.get_data()
    
    try:
        supabase.table("courses").insert({
            "course_id": data['course_id'],
            "title": data['title'],
            "price": data['price'],
            "bot2_text": data['bot2_text'],
            "bot2_image_id": file_id
        }).execute()
        
        await message.answer(f"✅ Success! Course '{data['title']}' has been added to the database.")
    except Exception as e:
        await message.answer(f"❌ Error saving to database: {e}")
        
    await state.clear()

async def main():
    print("Starting Gateway Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

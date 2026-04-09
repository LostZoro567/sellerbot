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

BOT_TOKEN   = os.getenv("BOT1_TOKEN")
SECRET_CODE = os.getenv("SECRET_INVITE_CODE")
ADMIN_ID    = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")

REFERRAL_PERCENT = 25   # % of purchase price credited to referrer
WELCOME_PHOTO    = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── FSM States ─────────────────────────────────────────────────────────────────

class AddCourseFSM(StatesGroup):
    waiting_for_course_id       = State()
    waiting_for_title           = State()
    waiting_for_price           = State()
    waiting_for_numeric_price   = State()
    waiting_for_bot2_text       = State()
    waiting_for_bot2_image      = State()
    waiting_for_delivery_content = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ensure_user(user_id: int, username: str | None = None):
    """Insert user row if not present (upsert-safe)."""
    existing = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username": username or "",
            "wallet_balance": 0
        }).execute()


def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return float(row.data[0]["wallet_balance"]) if row.data else 0.0


def _add_wallet(user_id: int, amount: float):
    current = _get_wallet(user_id)
    supabase.table("users").update({"wallet_balance": round(current + amount, 2)}).eq("telegram_user_id", user_id).execute()


def _deduct_wallet(user_id: int, amount: float) -> bool:
    current = _get_wallet(user_id)
    if current < amount:
        return False
    supabase.table("users").update({"wallet_balance": round(current - amount, 2)}).eq("telegram_user_id", user_id).execute()
    return True


# ── /start (user flow + referral tracking) ─────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    user_id  = message.from_user.id
    username = message.from_user.username
    args     = command.args or ""

    # ── Referral link format: SECRET_CODE:ref_USERID ──
    if ":" in args:
        code, ref_part = args.split(":", 1)
        referrer_id = int(ref_part.replace("ref_", "")) if ref_part.startswith("ref_") else None
    else:
        code        = args
        referrer_id = None

    if code != SECRET_CODE:
        return await message.answer("Welcome 👋🏻\n\nYou need a valid invite link to access the catalog.")

    # Register user
    _ensure_user(user_id, username)

    # Log referral (only once per new user)
    if referrer_id and referrer_id != user_id:
        existing_ref = supabase.table("referrals").select("id").eq("referred_user_id", user_id).execute()
        if not existing_ref.data:
            _ensure_user(referrer_id)
            supabase.table("referrals").insert({
                "referrer_id": referrer_id,
                "referred_user_id": user_id,
                "status": "joined"
            }).execute()
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 *Someone joined using your referral link!*\n\n"
                    f"You'll earn *{REFERRAL_PERCENT}%* wallet credit when they make a purchase.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    # Build course menu
    courses = supabase.table("courses").select("course_id, title").execute().data
    builder = InlineKeyboardBuilder()
    for c in courses:
        builder.row(InlineKeyboardButton(
            text=f"📘 {c['title']}",
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
        ))

    wallet = _get_wallet(user_id)
    wallet_note = f"\n\n💰 *Your wallet balance: ₹{wallet:.2f}*" if wallet > 0 else ""

    await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=f"Welcome to the private portal! 🎓\nSelect a course below.{wallet_note}",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ── /wallet ─────────────────────────────────────────────────────────────────────

@dp.message(Command("wallet"))
async def cmd_wallet(message: types.Message):
    user_id = message.from_user.id
    _ensure_user(user_id, message.from_user.username)

    balance    = _get_wallet(user_id)
    ref_count  = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).execute().data)
    paid_refs  = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).eq("status", "purchased").execute().data)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Get My Referral Link", callback_data="get_referral_link"))

    await message.answer(
        f"💼 *Your Wallet*\n\n"
        f"Balance: *₹{balance:.2f}*\n"
        f"Total Referrals: *{ref_count}*\n"
        f"Referrals who purchased: *{paid_refs}*\n\n"
        f"📌 Share your referral link and earn *{REFERRAL_PERCENT}%* of every purchase your friends make — credited straight to your wallet!\n\n"
        f"_Use your wallet balance as a discount on your next purchase._",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "get_referral_link")
async def send_referral_link(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={SECRET_CODE}:ref_{user_id}"

    await callback.message.answer(
        f"🔗 *Your Personal Referral Link:*\n\n`{ref_link}`\n\n"
        f"Share this with friends. When they buy a course, you instantly earn *{REFERRAL_PERCENT}%* of their purchase as wallet credits! 💸",
        parse_mode="Markdown"
    )
    await callback.answer()


# ── ADMIN: /addnew ──────────────────────────────────────────────────────────────

@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 *Add New Course*\n\nStep 1/6 — Enter a unique internal ID\n_(e.g. `python_basics`, `course_7`)_", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_course_id)

@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    await state.update_data(course_id=message.text.strip().lower().replace(" ", "_"))
    await message.answer("Step 2/6 — Enter the *display title*\n_(e.g. Master Python 2024)_", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_title)

@dp.message(AddCourseFSM.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer("Step 3/6 — Enter the *display price* shown to users\n_(e.g. `₹400` or `$15`)_", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_price)

@dp.message(AddCourseFSM.waiting_for_price)
async def process_price(message: types.Message, state: FSMContext):
    await state.update_data(price=message.text.strip())
    await message.answer("Step 4/6 — Enter the *numeric price* in ₹ (used for referral calculations)\n_(e.g. `400`)_", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_numeric_price)

@dp.message(AddCourseFSM.waiting_for_numeric_price)
async def process_numeric_price(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace("$", ""))
    except ValueError:
        return await message.answer("❌ Please enter a valid number (e.g. `400`).", parse_mode="Markdown")
    await state.update_data(numeric_price=numeric)
    await message.answer("Step 5/6 — Enter the *sales description* Bot 2 will show buyers:", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_bot2_text)

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer("Step 6a/6 — Paste a *public image URL* for the course thumbnail:\n_(e.g. `https://telegra.ph/file/abc.jpg`)_", parse_mode="Markdown")
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "Step 6b/6 — Send the *course delivery content*:\n\n"
        "• A *text message* (with Google Drive links, passwords, etc.)\n"
        "• Or a *file* (`.zip`, `.pdf`) with an optional caption",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_delivery_content)

@dp.message(AddCourseFSM.waiting_for_delivery_content)
async def process_delivery_content(message: types.Message, state: FSMContext):
    data = await state.get_data()

    delivery_text    = message.text or message.caption or "✅ Payment verified! Here is your course material."
    delivery_file_id = None

    if message.document:
        delivery_file_id = message.document.file_id
    elif message.video:
        delivery_file_id = message.video.file_id

    try:
        supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    delivery_text,
            "delivery_file_id": delivery_file_id,
        }).execute()
        await message.answer(f"✅ *Course Added!*\n\n_{data['title']}_ is now live and ready to sell.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Database error: `{e}`", parse_mode="Markdown")

    await state.clear()


# ── ADMIN: /broadcast ──────────────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📢 *Broadcast Mode*\n\nSend the message (text, photo, or video) to deliver to all users.\n\n"
        "_Dead accounts are auto-removed from the database._",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def execute_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("⏳ Collecting user data…")

    rows         = supabase.table("transactions").select("telegram_user_id").execute().data
    unique_users = {r["telegram_user_id"] for r in rows}

    success = fail = 0
    for uid in unique_users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            supabase.table("transactions").delete().eq("telegram_user_id", uid).execute()
        except Exception:
            fail += 1

    await status_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📢 Delivered: *{success}* users\n"
        f"🗑 Dead accounts removed: *{fail}*",
        parse_mode="Markdown"
    )


# ── ADMIN: /stats ──────────────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    total_users   = len(supabase.table("users").select("telegram_user_id").execute().data)
    total_sales   = len(supabase.table("transactions").select("id").eq("status", "approved").execute().data)
    total_refs    = len(supabase.table("referrals").select("id").execute().data)
    paid_refs     = len(supabase.table("referrals").select("id").eq("status", "purchased").execute().data)
    total_courses = len(supabase.table("courses").select("course_id").execute().data)

    await message.answer(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: *{total_users}*\n"
        f"✅ Approved Sales: *{total_sales}*\n"
        f"📚 Courses Available: *{total_courses}*\n"
        f"🔗 Total Referrals: *{total_refs}*\n"
        f"💰 Referrals → Purchase: *{paid_refs}*",
        parse_mode="Markdown"
    )


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Gateway Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

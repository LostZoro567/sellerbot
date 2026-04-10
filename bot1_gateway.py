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

BOT_TOKEN        = os.getenv("BOT1_TOKEN")
SECRET_CODE      = os.getenv("SECRET_INVITE_CODE")
ADMIN_ID         = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME    = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")
REFERRAL_PERCENT = 25
WELCOME_PHOTO    = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg"

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()


# ── FSM States ─────────────────────────────────────────────────────────────────

class AddCourseFSM(StatesGroup):
    waiting_for_course_id        = State()
    waiting_for_title            = State()
    waiting_for_price_inr        = State()
    waiting_for_price_usd        = State()
    waiting_for_bot2_text        = State()
    waiting_for_bot2_image       = State()
    waiting_for_delivery_content = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ensure_user(user_id: int, username=None):
    existing = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username":         username or "",
            "wallet_balance":   0
        }).execute()


def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return float(row.data) if row.data else 0.0


def _add_wallet(user_id: int, amount: float):
    current = _get_wallet(user_id)
    supabase.table("users").update({"wallet_balance": round(current + amount, 2)}).eq("telegram_user_id", user_id).execute()


def _deduct_wallet(user_id: int, amount: float) -> bool:
    current = _get_wallet(user_id)
    if current < amount:
        return False
    supabase.table("users").update({"wallet_balance": round(current - amount, 2)}).eq("telegram_user_id", user_id).execute()
    return True


# ── Shared helper: full referral program screen ────────────────────────────────

async def _send_referral_info(user_id: int, username, target: types.Message):
    _ensure_user(user_id, username)

    balance   = _get_wallet(user_id)
    ref_count = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).execute().data)
    paid_refs = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).eq("status", "purchased").execute().data)

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={SECRET_CODE}-ref-{user_id}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗  Copy My Referral Link", callback_data="get_referral_link"))

    await target.answer(
        "🎁 *Referral Program*\n\n"
        f"┌ 💰 Wallet Balance:         *₹{balance:.2f}*\n"
        f"├ 👥 Friends Referred:        *{ref_count}*\n"
        f"└ 🛍 Friends Who Purchased:   *{paid_refs}*\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💡 *How it works:*\n"
        "1️⃣  Share your referral link with friends\n"
        "2️⃣  They join the private portal through your link\n"
        f"3️⃣  When they buy a course, you earn *{REFERRAL_PERCENT}%* of the price as wallet credits\n"
        "4️⃣  Use those credits as a discount on your own purchases!\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        "_Tap the link above to copy it, then share it anywhere!_",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ── /start ─────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    user_id  = message.from_user.id
    username = message.from_user.username
    args     = command.args or ""

    if args == "refer":
        return await _send_referral_info(user_id, username, message)

    referrer_id = None
    if "-ref-" in args:
        code, ref_part = args.split("-ref-", 1)
        try:
            referrer_id = int(ref_part)
        except ValueError:
            referrer_id = None
    else:
        code = args

    if code != SECRET_CODE:
        return await message.answer(
            "👋 *Welcome!*\n\n"
            "You need a valid invite link to access the private catalog.\n\n"
            "_Ask a friend who's already inside to share their referral link with you._",
            parse_mode="Markdown"
        )

    _ensure_user(user_id, username)

    if referrer_id and referrer_id != user_id:
        existing_ref = supabase.table("referrals").select("id").eq("referred_user_id", user_id).execute()
        if not existing_ref.data:
            _ensure_user(referrer_id)
            supabase.table("referrals").insert({
                "referrer_id":      referrer_id,
                "referred_user_id": user_id,
                "status":           "joined"
            }).execute()
            try:
                await bot.send_message(
                    referrer_id,
                    "🎉 *Someone just joined using your referral link!*\n\n"
                    f"You'll earn *{REFERRAL_PERCENT}%* wallet credit the moment they make a purchase. 💸",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    courses = supabase.table("courses").select("course_id, title").execute().data
    builder = InlineKeyboardBuilder()
    for c in courses:
        # Exclude the bundle_all from listing directly in start menu
        if c != 'bundle_all':
            builder.row(InlineKeyboardButton(
                text=f"📘 {c}",
                url=f"https://t.me/{BOT2_USERNAME}?start={c}"
            ))

    wallet      = _get_wallet(user_id)
    wallet_note = f"\n\n💰 *Wallet Balance:* ₹{wallet:.2f}" if wallet > 0 else ""

    await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=(
            "🎓 *Welcome to the Private Portal!*\n\n"
            f"Browse the courses below and tap one to view details & purchase.{wallet_note}"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ── /wallet ─────────────────────────────────────────────────────────────────────

@dp.message(Command("wallet"))
async def cmd_wallet(message: types.Message):
    user_id = message.from_user.id
    _ensure_user(user_id, message.from_user.username)

    balance   = _get_wallet(user_id)
    ref_count = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).execute().data)
    paid_refs = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).eq("status", "purchased").execute().data)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Get My Referral Link", callback_data="get_referral_link"))

    await message.answer(
        "💼 *Your Wallet*\n\n"
        f"┌ 💰 Balance:                *₹{balance:.2f}*\n"
        f"├ 👥 Total Referrals:        *{ref_count}*\n"
        f"└ 🛍 Referrals Purchased:    *{paid_refs}*\n\n"
        f"📌 *How it works:*\n"
        f"Share your referral link → a friend joins → they buy a course → you instantly earn *{REFERRAL_PERCENT}%* of their purchase as wallet credits!\n\n"
        "_Your wallet balance can be used as a discount on your next purchase._\n\n"
        "_For the full referral program, use /refer_",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ── /refer ─────────────────────────────────────────────────────────────────────

@dp.message(Command("refer"))
async def cmd_refer(message: types.Message):
    await _send_referral_info(message.from_user.id, message.from_user.username, message)


# ── Referral link callback ──────────────────────────────────────────────────────

@dp.callback_query(F.data == "get_referral_link")
async def send_referral_link(callback: types.CallbackQuery):
    user_id  = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={SECRET_CODE}-ref-{user_id}"

    await callback.message.answer(
        "🔗 *Your Personal Referral Link*\n\n"
        f"`{ref_link}`\n\n"
        "📤 Share this with friends!\n"
        f"When they buy a course, you instantly earn *{REFERRAL_PERCENT}%* of their purchase straight into your wallet. 💸\n\n"
        "_Tap the link above to copy it._",
        parse_mode="Markdown"
    )
    await callback.answer()


# ── ADMIN: /addnew ──────────────────────────────────────────────────────────────

@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 *Add New Course — Step 1 of 7*\n\n"
        "Enter a unique *internal ID* for this course.\n"
        "_(Use lowercase letters/numbers only, e.g. `python_basics`, `course_7`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_course_id)

@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    await state.update_data(course_id=message.text.strip().lower().replace(" ", "_"))
    await message.answer(
        "✅ ID saved!\n\n"
        "🛠 *Step 2 of 7 — Display Title*\n\n"
        "Enter the title users will see.\n_(e.g. `Master Python 2024`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_title)

@dp.message(AddCourseFSM.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "✅ Title saved!\n\n"
        "🛠 *Step 3 of 7 — Price (INR)*\n\n"
        "Enter the price in *₹* as a plain number. This is used for referral math.\n_(e.g. `400`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_inr)

@dp.message(AddCourseFSM.waiting_for_price_inr)
async def process_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer(
            "❌ That doesn't look like a number. Please enter something like `400`.",
            parse_mode="Markdown"
        )
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 *Step 4 of 7 — Price (USD)*\n\n"
        "Enter the price in *$* as a plain number. I will combine this with INR for the display price.\n_(e.g. `15`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_usd)

@dp.message(AddCourseFSM.waiting_for_price_usd)
async def process_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer(
            "❌ That doesn't look like a number. Please enter something like `15`.",
            parse_mode="Markdown"
        )
    
    data = await state.get_data()
    numeric_inr = data
    
    # Format string (e.g. ₹400 / $15)
    display_price = f"₹{numeric_inr:g} / ${usd_val:g}"
    await state.update_data(price=display_price)
    
    await message.answer(
        f"✅ Display price saved as: *{display_price}*\n\n"
        "🛠 *Step 5 of 7 — Sales Description*\n\n"
        "Enter the sales text Bot 2 will show buyers when they view this course:",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_text)

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer(
        "✅ Description saved!\n\n"
        "🛠 *Step 6 of 7 — Course Thumbnail URL*\n\n"
        "Paste a public image URL for the course thumbnail.\n"
        "_(e.g. `https://telegra.ph/file/abc.jpg`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        "🛠 *Step 7 of 7 — Course Delivery Content*\n\n"
        "Send what the buyer receives after payment:\n\n"
        "• A *text message* (e.g. Google Drive links, passwords, notes)\n"
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
            "course_id":        data,
            "title":            data,
            "price":            data,
            "numeric_price":    data,
            "bot2_text":        data,
            "bot2_image_id":    data,
            "delivery_text":    delivery_text,
            "delivery_file_id": delivery_file_id,
        }).execute()
        await message.answer(
            "🎉 *Course Added Successfully!*\n\n"
            f"📘 *{data}* is now live and ready to sell.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"❌ *Database error:*\n\n`{e}`", parse_mode="Markdown")

    await state.clear()


# ── ADMIN: /broadcast ──────────────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📢 *Broadcast Mode*\n\n"
        "Send the message you want delivered to all users.\n"
        "Supports text, photos, and videos.\n\n"
        "⚠️ _Inactive (blocked) accounts are automatically removed from the database._",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def execute_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("⏳ Collecting user list…")

    rows         = supabase.table("transactions").select("telegram_user_id").execute().data
    unique_users = {r for r in rows}

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
        "✅ *Broadcast Complete!*\n\n"
        f"📬 Delivered to:          *{success}* users\n"
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
        "📊 *Bot Statistics*\n\n"
        f"👥 Total Users:          *{total_users}*\n"
        f"✅ Approved Sales:       *{total_sales}*\n"
        f"📚 Courses Available:    *{total_courses}*\n"
        f"🔗 Total Referrals:      *{total_refs}*\n"
        f"💰 Referrals → Purchase: *{paid_refs}*",
        parse_mode="Markdown"
    )


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Gateway Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

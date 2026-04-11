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

def _ensure_user(user_id: int, username: str = ""):
    """Insert user if not exists. Safe to call multiple times."""
    existing = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "telegram_user_id": user_id,
            "username":         username or "",
            "wallet_balance":   0.0,
        }).execute()
    elif username:
        # Keep username up to date
        supabase.table("users").update({"username": username}).eq("telegram_user_id", user_id).execute()


def _get_wallet(user_id: int) -> float:
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    return round(float(row.data[0]["wallet_balance"]), 2) if row.data else 0.0


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
            "username":         "",
            "wallet_balance":   amount,
        }).execute()


def _deduct_wallet(user_id: int, amount: float) -> bool:
    amount = round(amount, 2)
    row = supabase.table("users").select("wallet_balance").eq("telegram_user_id", user_id).execute()
    if not row.data:
        return False
    current = round(float(row.data[0]["wallet_balance"]), 2)
    if current < amount:
        return False
    new_balance = round(current - amount, 2)
    supabase.table("users").update({"wallet_balance": new_balance}).eq("telegram_user_id", user_id).execute()
    return True


# ── Shared helper: full referral program screen ────────────────────────────────

async def _send_referral_info(user_id: int, username: str, target: types.Message):
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


# ── Callback: copy referral link ───────────────────────────────────────────────

@dp.callback_query(F.data == "get_referral_link")
async def cb_get_referral_link(callback: types.CallbackQuery):
    user_id  = callback.from_user.id
    username = callback.from_user.username or ""
    _ensure_user(user_id, username)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={SECRET_CODE}-ref-{user_id}"
    await callback.answer(f"Your link:\n{ref_link}", show_alert=True)


# ── /start ─────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    user_id  = message.from_user.id
    username = message.from_user.username or ""
    args     = command.args or ""

    # ?start=refer → show referral dashboard
    if args == "refer":
        return await _send_referral_info(user_id, username, message)

    # Parse referral code: SECRET-ref-REFERRER_ID
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

    # Record referral — validate referrer exists, prevent self-referral & duplicates
    if referrer_id and referrer_id != user_id:
        referrer_exists = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", referrer_id).execute()
        if referrer_exists.data:
            existing_ref = supabase.table("referrals").select("id").eq("referred_user_id", user_id).execute()
            if not existing_ref.data:
                supabase.table("referrals").insert({
                    "referrer_id":      referrer_id,
                    "referred_user_id": user_id,
                    "status":           "joined",
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

    # Build course list
    courses = supabase.table("courses").select("course_id, title").execute().data or []
    builder = InlineKeyboardBuilder()
    for c in courses:
        if c["course_id"] != "bundle_all":
            builder.row(InlineKeyboardButton(
                text=f"📘 {c['title']}",
                url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
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
    _ensure_user(user_id, message.from_user.username or "")

    balance   = _get_wallet(user_id)
    ref_count = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).execute().data)
    paid_refs = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).eq("status", "purchased").execute().data)

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={SECRET_CODE}-ref-{user_id}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗  Copy My Referral Link", callback_data="get_referral_link"))

    await message.answer(
        "💰 *Your Wallet*\n\n"
        f"┌ 💵 Balance:              *₹{balance:.2f}*\n"
        f"├ 👥 Friends Referred:     *{ref_count}*\n"
        f"└ 🛍 Friends Who Bought:   *{paid_refs}*\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        "_Share this link — earn 25% commission on every purchase your referral makes!_",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )


# ── ADMIN: /addcourse ──────────────────────────────────────────────────────────

@dp.message(Command("addcourse"))
async def cmd_addcourse(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 *Add New Course — Step 1 of 7*\n\n"
        "Enter a unique *Course ID* (no spaces, lowercase).\n_(e.g. `python_basics`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_course_id)


@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    course_id = message.text.strip().lower().replace(" ", "_")
    existing = supabase.table("courses").select("course_id").eq("course_id", course_id).execute()
    if existing.data:
        return await message.answer(
            f"❌ Course ID `{course_id}` already exists. Choose a different one.",
            parse_mode="Markdown"
        )
    await state.update_data(course_id=course_id)
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
        "Enter the price in *₹* as a plain number. This is the numeric price used for referral math.\n_(e.g. `400`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_inr)


@dp.message(AddCourseFSM.waiting_for_price_inr)
async def process_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
        if numeric <= 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "❌ That doesn't look like a valid price. Enter a positive number like `400`.",
            parse_mode="Markdown"
        )
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 *Step 4 of 7 — Price (USD)*\n\n"
        "Enter the price in *$* as a plain number.\n_(e.g. `15`)_",
        parse_mode="Markdown"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_usd)


@dp.message(AddCourseFSM.waiting_for_price_usd)
async def process_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
        if usd_val <= 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "❌ That doesn't look like a valid price. Enter a positive number like `15`.",
            parse_mode="Markdown"
        )
    data        = await state.get_data()
    numeric_inr = data.get("numeric_price", 0)
    display_price = f"₹{int(numeric_inr)} / ${int(usd_val)}"
    await state.update_data(price=display_price, price_usd=usd_val)
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
            "course_id":        data["course_id"],
            "title":            data["title"],
            "price":            data["price"],          # display string e.g. "₹400 / $15"
            "numeric_price":    data["numeric_price"],  # float for referral math
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    delivery_text,
            "delivery_file_id": delivery_file_id,
        }).execute()
        await message.answer(
            "🎉 *Course Added Successfully!*\n\n"
            f"📘 *{data['title']}* is now live and ready to sell.",
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

    rows         = supabase.table("users").select("telegram_user_id").execute().data or []
    unique_users = {r["telegram_user_id"] for r in rows}

    success = fail = 0
    for uid in unique_users:
        try:
            await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            # Clean up dead user from all tables
            supabase.table("transactions").delete().eq("telegram_user_id", uid).execute()
            supabase.table("referrals").delete().eq("referred_user_id", uid).execute()
            supabase.table("referrals").delete().eq("referrer_id", uid).execute()
            supabase.table("users").delete().eq("telegram_user_id", uid).execute()
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

    total_users   = len(supabase.table("users").select("telegram_user_id").execute().data or [])
    total_sales   = len(supabase.table("transactions").select("id").eq("status", "approved").execute().data or [])
    total_refs    = len(supabase.table("referrals").select("id").execute().data or [])
    paid_refs     = len(supabase.table("referrals").select("id").eq("status", "purchased").execute().data or [])
    total_courses = len(supabase.table("courses").select("course_id").execute().data or [])

    # Calculate total revenue from approved transactions
    revenue_rows = supabase.table("transactions").select("amount_paid").eq("status", "approved").execute().data or []
    total_revenue = sum(float(r.get("amount_paid") or 0) for r in revenue_rows)

    await message.answer(
        "📊 *Bot Statistics*\n\n"
        f"👥 Total Users:           *{total_users}*\n"
        f"✅ Approved Sales:        *{total_sales}*\n"
        f"💵 Total Revenue:         *₹{total_revenue:.2f}*\n"
        f"📚 Courses Available:     *{total_courses}*\n"
        f"🔗 Total Referrals:       *{total_refs}*\n"
        f"💰 Referrals → Purchase:  *{paid_refs}*",
        parse_mode="Markdown"
    )


# ── ADMIN: /listcourses ────────────────────────────────────────────────────────

@dp.message(Command("listcourses"))
async def cmd_listcourses(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    courses = supabase.table("courses").select("course_id, title, price, numeric_price").execute().data or []
    if not courses:
        return await message.answer("📭 No courses in the database yet.")
    lines = ["📚 *All Courses*\n"]
    for c in courses:
        lines.append(f"• `{c['course_id']}` — *{c['title']}* — {c['price']} (₹{c['numeric_price']})")
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── ADMIN: /deletecourse ───────────────────────────────────────────────────────

@dp.message(Command("deletecourse"))
async def cmd_deletecourse(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer(
            "Usage: `/deletecourse <course_id>`", parse_mode="Markdown"
        )
    course_id = parts[1].strip()
    res = supabase.table("courses").delete().eq("course_id", course_id).execute()
    if res.data:
        await message.answer(f"🗑 Course `{course_id}` deleted.", parse_mode="Markdown")
    else:
        await message.answer(f"❌ Course `{course_id}` not found.", parse_mode="Markdown")


# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Gateway Bot starting…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

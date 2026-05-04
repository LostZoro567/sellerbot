import asyncio
import time
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError
from db import supabase

load_dotenv()

BOT_TOKEN        = os.getenv("BOT1_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME    = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")
REFERRAL_PERCENT = 25
WELCOME_PHOTO    = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg"
AUTO_DELETE_SECS = 1800   # 30 minutes

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── FSM States ─────────────────────────────────────────────────────────────────

class AddCourseFSM(StatesGroup):
    waiting_for_course_id        = State()
    waiting_for_button_text      = State()
    waiting_for_title            = State()
    waiting_for_price_inr        = State()
    waiting_for_price_usd        = State()
    waiting_for_bot2_text        = State()
    waiting_for_bot2_image       = State()
    waiting_for_dump_ids         = State()

class AddBundleFSM(StatesGroup):
    waiting_for_bundle_id        = State()
    waiting_for_button_text      = State()
    waiting_for_title            = State()
    waiting_for_price_inr        = State()
    waiting_for_price_usd        = State()
    waiting_for_bot2_text        = State()
    waiting_for_bot2_image       = State()
    waiting_for_dump_ids         = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()

class DBroadcastFSM(StatesGroup):
    waiting_for_video       = State()
    waiting_for_button_text = State()

class RejectFSM(StatesGroup):
    waiting_for_reason = State()

# ── Helpers ────────────────────────────────────────────────────────────────────

async def _auto_delete(chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECS):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

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
    return float(row.data[0]["wallet_balance"]) if row.data else 0.0

async def _send_referral_info(user_id: int, username, target: types.Message):
    _ensure_user(user_id, username)

    balance   = _get_wallet(user_id)
    ref_count = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).execute().data)
    paid_refs = len(supabase.table("referrals").select("id").eq("referrer_id", user_id).eq("status", "purchased").execute().data)

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref-{user_id}"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗  Copy My Referral Link", callback_data="get_referral_link"))

    await target.answer(
        "🎁 <b>Referral Program</b>\n\n"
        f"┌ 💰 Wallet Balance:         <b>₹{balance:.2f}</b>\n"
        f"├ 👥 Friends Referred:        <b>{ref_count}</b>\n"
        f"└ 🛍 Friends Who Purchased:   <b>{paid_refs}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How it works:</b>\n"
        "1️⃣  Share your referral link with friends\n"
        "2️⃣  They join the private portal through your link\n"
        f"3️⃣  When they buy a course, you earn <b>{REFERRAL_PERCENT}%</b> of the price as wallet credits\n"
        "4️⃣  Use those credits as a discount on your own purchases!\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🔗 <b>Your Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "<i>Tap the link above to copy it, then share it anywhere!</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# ── /start & Bundle Menus ─────────────────────────────────────────────────────

@dp.message(CommandStart())
async def handle_start(message: types.Message, command: CommandObject):
    user_id  = message.from_user.id
    username = message.from_user.username
    args     = command.args or ""

    if args == "refer":
        return await _send_referral_info(user_id, username, message)

    referrer_id = None
    if args.startswith("ref-"):
        try:
            referrer_id = int(args.replace("ref-", ""))
        except ValueError:
            referrer_id = None

    _ensure_user(user_id, username)

    if referrer_id and referrer_id != user_id:
        referrer_exists = supabase.table("users").select("telegram_user_id").eq("telegram_user_id", referrer_id).execute()
        if referrer_exists.data:
            existing_ref = supabase.table("referrals").select("id").eq("referred_user_id", user_id).execute()
            if not existing_ref.data:
                supabase.table("referrals").insert({
                    "referrer_id":      referrer_id,
                    "referred_user_id": user_id,
                    "status":           "joined"
                }).execute()
                try:
                    await bot.send_message(
                        referrer_id,
                        "🎉 <b>Someone just joined using your referral link!</b>\n\n"
                        f"You'll earn <b>{REFERRAL_PERCENT}%</b> wallet credit the moment they make a purchase. 💸",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    regular_courses = [c for c in all_items if not c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    for c in regular_courses:
        display_name = c.get("button_text") or c["title"]
        builder.row(InlineKeyboardButton(
            text=f"{display_name}",
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
        ))

    builder.row(InlineKeyboardButton(
        text="🏷 Buy All [₹1,499 / 22$]", 
        url=f"https://t.me/{BOT2_USERNAME}?start=bundle_all"
    ))

    wallet      = _get_wallet(user_id)
    wallet_note = f"\n\n💰 <b>Wallet Balance:</b> ₹{wallet:.2f}" if wallet > 0 else ""

    sent_msg = await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=(
            "🛒 <b>Telegram's Best Collection!</b>\n\n"
            "🔥 Today's “Bundle” Offer : \nC||P + R||P :- 699₹ / 10$ \n\n✨ <b>Buy All Collection</b> :\n<del>Regular Price : 3,599₹ / 60$</del> ❌\n\nBundle Offer = 1,499₹ / 22$ ✅" + wallet_note
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    
    asyncio.create_task(_auto_delete(message.chat.id, sent_msg.message_id))

@dp.callback_query(F.data == "show_bundles_menu")
async def menu_show_bundles(callback: types.CallbackQuery):
    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    bundles = [c for c in all_items if c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    if not bundles:
        builder.row(InlineKeyboardButton(text="No bundles available right now", callback_data="ignore"))
    else:
        for b in bundles:
            display_name = b.get("button_text") or b["title"]
            builder.row(InlineKeyboardButton(
                text=f"📦 {display_name}", 
                url=f"https://t.me/{BOT2_USERNAME}?start={b['course_id']}"
            ))

    builder.row(InlineKeyboardButton(text="⬅️ Back to All Courses", callback_data="back_to_main_menu"))

    await callback.message.edit_caption(
        caption=(
            "🎁 <b>Exclusive Bundles</b>\n\n"
            "Select a bundle below to get multiple courses at a massively discounted price!\n\n"
            "⏳ <i>This message self-destructs in 15 minutes.</i>"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main_menu")
async def menu_back_to_main(callback: types.CallbackQuery):
    all_items = supabase.table("courses").select("course_id, title, button_text").order("created_at").execute().data
    regular_courses = [c for c in all_items if not c["course_id"].startswith("bundle_")]

    builder = InlineKeyboardBuilder()
    
    for c in regular_courses:
        display_name = c.get("button_text") or c["title"]
        builder.row(InlineKeyboardButton(
            text=f"{display_name}",
            url=f"https://t.me/{BOT2_USERNAME}?start={c['course_id']}"
        ))

    builder.row(InlineKeyboardButton(
        text="🏷 Buy All [₹1,499 / 22$]", 
        url=f"https://t.me/{BOT2_USERNAME}?start=bundle_all"
    ))

    wallet = _get_wallet(callback.from_user.id)
    wallet_note = f"\n\n💰 <b>Wallet Balance:</b> ₹{wallet:.2f}" if wallet > 0 else ""

    await callback.message.edit_caption(
        caption=(
            "🛒 <b>Telegram's Best Collection!</b>\n\n"
            "🔥 Today's “Bundle” Offer : \nC||P + R||P :- 699₹ / 10$ \n\n✨ <b>Buy All Collection</b> :\n<del>Regular Price : 3,599₹ / 60$</del> ❌\n\nBundle Offer = 1,499₹ / 22$ ✅" + wallet_note
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

# ── /wallet & Referrals ────────────────────────────────────────────────────────

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
        "💼 <b>Your Wallet</b>\n\n"
        f"┌ 💰 Balance:                <b>₹{balance:.2f}</b>\n"
        f"├ 👥 Total Referrals:        <b>{ref_count}</b>\n"
        f"└ 🛍 Referrals Purchased:    <b>{paid_refs}</b>\n\n"
        f"📌 <b>How it works:</b>\n"
        f"Share your referral link → a friend joins → they buy a course → you instantly earn <b>{REFERRAL_PERCENT}%</b> of their purchase as wallet credits!\n\n"
        "<i>Your wallet balance can be used as a discount on your next purchase.</i>\n\n"
        "<i>For the full referral program, use /refer</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(Command("refer"))
async def cmd_refer(message: types.Message):
    await _send_referral_info(message.from_user.id, message.from_user.username, message)

@dp.callback_query(F.data == "get_referral_link")
async def send_referral_link(callback: types.CallbackQuery):
    user_id  = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref-{user_id}"

    await callback.message.answer(
        "🔗 <b>Your Personal Referral Link</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        "📤 Share this with friends!\n"
        f"When they buy a course, you instantly earn <b>{REFERRAL_PERCENT}%</b> of their purchase straight into your wallet. 💸\n\n"
        "<i>Tap the link above to copy it.</i>",
        parse_mode="HTML"
    )
    await callback.answer()

# ── ADMIN: /addnew ──────────────────────────────────────────────────────────────

@dp.message(Command("addnew"))
async def cmd_addnew(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Add New Course — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this course.\n"
        "_(Use lowercase letters/numbers only, e.g. <code>python_basics</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_course_id)

@dp.message(AddCourseFSM.waiting_for_course_id)
async def process_course_id(message: types.Message, state: FSMContext):
    await state.update_data(course_id=message.text.strip().lower().replace(" ", "_"))
    await message.answer(
        "✅ ID saved!\n\n"
        "🛠 <b>Step 2 of 8 — Short Button Name</b>\n\n"
        "Enter the short text for the Inline Menu Button.\n_(e.g. <code>Python Basics</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_button_text)

@dp.message(AddCourseFSM.waiting_for_button_text)
async def process_button_text(message: types.Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await message.answer(
        "✅ Button name saved!\n\n"
        "🛠 <b>Step 3 of 8 — Full Display Title</b>\n\n"
        "Enter the long, detailed title shown inside the course page.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_title)

@dp.message(AddCourseFSM.waiting_for_title)
async def process_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "✅ Title saved!\n\n"
        "🛠 <b>Step 4 of 8 — Price (INR)</b>\n\n"
        "Enter the price in <b>₹</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_inr)

@dp.message(AddCourseFSM.waiting_for_price_inr)
async def process_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ That doesn't look like a number. Please enter something like <code>400</code>.", parse_mode="HTML")
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 <b>Step 5 of 8 — Price (USD)</b>\n\n"
        "Enter the price in <b>$</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_price_usd)

@dp.message(AddCourseFSM.waiting_for_price_usd)
async def process_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ That doesn't look like a number.", parse_mode="HTML")
    
    data = await state.get_data()
    numeric_inr = data.get("numeric_price", 0)
    
    display_price = f"₹{numeric_inr:g} / ${usd_val:g}"
    await state.update_data(price=display_price)
    
    await message.answer(
        f"✅ Display price saved as: <b>{display_price}</b>\n\n"
        "🛠 <b>Step 6 of 8 — Sales Description</b>\n\n"
        "Enter the sales text Bot 2 will show buyers:",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_text)

@dp.message(AddCourseFSM.waiting_for_bot2_text)
async def process_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer(
        "✅ Description saved!\n\n"
        "🛠 <b>Step 7 of 8 — Course Thumbnail URL</b>\n\n"
        "Paste a public image URL for the course thumbnail.",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_bot2_image)

@dp.message(AddCourseFSM.waiting_for_bot2_image)
async def process_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        "🛠 <b>Step 8 of 8 — Delivery Content (Message IDs)</b>\n\n"
        "Go to your private Storage Channel and find the message IDs for the files/links you want to send.\n"
        "Enter them separated by commas.\n\n"
        "<i>(Example: <code>104, 105, 106</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddCourseFSM.waiting_for_dump_ids)

@dp.message(AddCourseFSM.waiting_for_dump_ids)
async def process_dump_ids(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dump_ids = message.text.strip()

    try:
        supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "button_text":      data["button_text"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    "✅ Payment verified! Here is your access.",
            "dump_message_ids": dump_ids, 
        }).execute()
        
        await message.answer(
            "🎉 <b>Course Added Successfully!</b>\n\n"
            f"📘 <b>{data['title']}</b> is now live.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Database error:</b>\n\n<code>{e}</code>", parse_mode="HTML")

    await state.clear()

# ── ADMIN: /addbundle ───────────────────────────────────────────────────────────

@dp.message(Command("addbundle"))
async def cmd_addbundle(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 <b>Add New Bundle — Step 1 of 8</b>\n\n"
        "Enter a unique <b>internal ID</b> for this bundle.\n"
        "_(I will automatically add 'bundle_' to the front of whatever you type here.)_",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bundle_id)

@dp.message(AddBundleFSM.waiting_for_bundle_id)
async def process_bundle_id(message: types.Message, state: FSMContext):
    raw_id = message.text.strip().lower().replace(" ", "_")
    bundle_id = raw_id if raw_id.startswith("bundle_") else f"bundle_{raw_id}"
    
    await state.update_data(course_id=bundle_id)
    await message.answer(
        f"✅ ID saved as: <code>{bundle_id}</code>\n\n"
        "🛠 <b>Step 2 of 8 — Short Button Name</b>\n\n"
        "Enter the short text for the Inline Menu Button.\n_(e.g. <code>Mega Pack</code>)_",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_button_text)

@dp.message(AddBundleFSM.waiting_for_button_text)
async def process_bundle_button_text(message: types.Message, state: FSMContext):
    await state.update_data(button_text=message.text.strip())
    await message.answer(
        "✅ Button name saved!\n\n"
        "🛠 <b>Step 3 of 8 — Full Display Title</b>\n\n"
        "Enter the long, detailed title users will see on the sales page.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_title)

@dp.message(AddBundleFSM.waiting_for_title)
async def process_bundle_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await message.answer(
        "✅ Title saved!\n\n"
        "🛠 <b>Step 4 of 8 — Price (INR)</b>\n\n"
        "Enter the bundle price in <b>₹</b> as a plain number.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_price_inr)

@dp.message(AddBundleFSM.waiting_for_price_inr)
async def process_bundle_price_inr(message: types.Message, state: FSMContext):
    try:
        numeric = float(message.text.strip().replace("₹", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Please enter a valid number.", parse_mode="HTML")
        
    await state.update_data(numeric_price=numeric)
    await message.answer(
        "✅ INR Price saved!\n\n"
        "🛠 <b>Step 5 of 8 — Price (USD)</b>\n"
        "Enter the price in <b>$</b>.", 
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_price_usd)

@dp.message(AddBundleFSM.waiting_for_price_usd)
async def process_bundle_price_usd(message: types.Message, state: FSMContext):
    try:
        usd_val = float(message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        return await message.answer("❌ Please enter a valid number.", parse_mode="HTML")
    
    data = await state.get_data()
    display_price = f"₹{data['numeric_price']:g} / ${usd_val:g}"
    await state.update_data(price=display_price)
    
    await message.answer(
        f"✅ Price saved as: <b>{display_price}</b>\n\n"
        "🛠 <b>Step 6 of 8 — Sales Description</b>\n\n"
        "Enter the bundle text description for the sales bot:",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bot2_text)

@dp.message(AddBundleFSM.waiting_for_bot2_text)
async def process_bundle_bot2_text(message: types.Message, state: FSMContext):
    await state.update_data(bot2_text=message.text.strip())
    await message.answer(
        "✅ Description saved!\n\n"
        "🛠 <b>Step 7 of 8 — Bundle Thumbnail URL</b>\n\n"
        "Paste a public image URL for the bundle thumbnail.",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_bot2_image)

@dp.message(AddBundleFSM.waiting_for_bot2_image)
async def process_bundle_bot2_image(message: types.Message, state: FSMContext):
    await state.update_data(bot2_image_id=message.text.strip())
    await message.answer(
        "✅ Thumbnail saved!\n\n"
        "🛠 <b>Step 8 of 8 — Delivery Content (Message IDs)</b>\n\n"
        "Go to your private Storage Channel and find the message IDs for the files/links you want to include in this bundle.\n"
        "Enter them separated by commas.\n\n"
        "<i>(Example: <code>104, 105, 106</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(AddBundleFSM.waiting_for_dump_ids)

@dp.message(AddBundleFSM.waiting_for_dump_ids)
async def process_bundle_dump_ids(message: types.Message, state: FSMContext):
    data = await state.get_data()
    dump_ids = message.text.strip()

    try:
        supabase.table("courses").insert({
            "course_id":        data["course_id"],
            "button_text":      data["button_text"],
            "title":            data["title"],
            "price":            data["price"],
            "numeric_price":    data["numeric_price"],
            "bot2_text":        data["bot2_text"],
            "bot2_image_id":    data["bot2_image_id"],
            "delivery_text":    "✅ Payment verified! Here is your bundle access.",
            "dump_message_ids": dump_ids, 
        }).execute()
        
        await message.answer(
            "🎉 <b>Bundle Added Successfully!</b>\n\n"
            f"📦 <b>{data['title']}</b> is now live.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ <b>Database error:</b>\n\n<code>{e}</code>", parse_mode="HTML")

    await state.clear()

# ── ADMIN: /broadcast ──────────────────────────────────────────────────────────

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📢 <b>Broadcast Mode</b>\n\n"
        "Send the message you want delivered to all users.\n"
        "Supports text, photos, and videos.\n\n"
        "⚠️ <i>Inactive (blocked) accounts are automatically removed from the database.</i>",
        parse_mode="HTML"
    )
    await state.set_state(BroadcastFSM.waiting_for_message)

@dp.message(BroadcastFSM.waiting_for_message)
async def broadcast_preview(message: types.Message, state: FSMContext):
    await state.update_data(
        preview_chat_id=message.chat.id,
        preview_message_id=message.message_id
    )
    await state.set_state(BroadcastFSM.waiting_for_confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm — Send to all", callback_data="broadcast_confirm"),
        InlineKeyboardButton(text="❌ Cancel",                callback_data="broadcast_cancel"),
    ]])
    await message.answer(
        "👆 <b>Preview above.</b>\n\n"
        "This will be sent to <b>all users</b>. Confirm?",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.in_({"broadcast_confirm", "broadcast_cancel"}))
async def broadcast_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback.data == "broadcast_cancel":
        await state.clear()
        return await callback.message.edit_text("❌ <b>Broadcast cancelled.</b>", parse_mode="HTML")

    data = await state.get_data()
    await state.clear()
    status_msg = await callback.message.edit_text("⏳ Collecting user list…")

    rows         = supabase.table("users").select("telegram_user_id").execute().data
    unique_users = {r["telegram_user_id"] for r in rows}

    success = fail = 0
    for uid in unique_users:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=data["preview_chat_id"],
                message_id=data["preview_message_id"]
            )
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            supabase.table("transactions").delete().eq("telegram_user_id", uid).execute()
            supabase.table("referrals").delete().eq("referred_user_id", uid).execute()
            supabase.table("users").delete().eq("telegram_user_id", uid).execute()
        except Exception:
            fail += 1

    await status_msg.edit_text(
        "✅ <b>Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:          <b>{success}</b> users\n"
        f"🗑 Dead accounts removed: <b>{fail}</b>",
        parse_mode="HTML"
    )

# ── ADMIN: /dbroadcast ─────────────────────────────────────────────────────────

@dp.message(Command("dbroadcast"))
async def cmd_dbroadcast(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📹 <b>Delete-Broadcast Mode</b>\n\n"
        "Send the <b>video</b> you want to broadcast to all users.\n\n"
        "⚠️ <i>Broadcast messages will auto-delete from every user's chat after 15 minutes.</i>",
        parse_mode="HTML"
    )
    await state.set_state(DBroadcastFSM.waiting_for_video)

@dp.message(DBroadcastFSM.waiting_for_video)
async def dbroadcast_got_video(message: types.Message, state: FSMContext):
    # Accept both compressed video and video sent as a file/document
    if message.video:
        file_id = message.video.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        file_id = message.document.file_id
    else:
        return await message.answer(
            "❌ <b>That's not a video.</b>\n\n"
            "Please send a <b>video file</b>.\n"
            "<i>Tip: send it without compression if possible.</i>",
            parse_mode="HTML"
        )

    await state.update_data(video_file_id=file_id, caption=message.caption or "")
    await message.answer(
        "✅ <b>Video received!</b>\n\n"
        "Now enter the <b>inline button label</b>.\n"
        "<i>(e.g. <code>🛒 View All Courses</code>)</i>",
        parse_mode="HTML"
    )
    await state.set_state(DBroadcastFSM.waiting_for_button_text)

@dp.message(DBroadcastFSM.waiting_for_button_text)
async def dbroadcast_execute(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    button_text   = message.text.strip()
    video_file_id = data["video_file_id"]
    caption       = data["caption"]

    bot_info  = await bot.get_me()
    start_url = f"https://t.me/{bot_info.username}?start="

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=button_text, url=start_url)
    ]])

    status_msg = await message.answer("⏳ Broadcasting delete-broadcast to all users…")

    rows         = supabase.table("users").select("telegram_user_id").execute().data
    unique_users = {r["telegram_user_id"] for r in rows}

    success       = 0
    fail          = 0
    sent_messages = []   # (chat_id, message_id, sent_at)

    for uid in unique_users:
        try:
            sent = await bot.send_video(
                chat_id=uid,
                video=video_file_id,
                caption=caption or None,
                reply_markup=kb,
                parse_mode="HTML",
                protect_content=True
            )
            sent_messages.append((uid, sent.message_id, time.time()))
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            fail += 1
            supabase.table("transactions").delete().eq("telegram_user_id", uid).execute()
            supabase.table("referrals").delete().eq("referred_user_id", uid).execute()
            supabase.table("users").delete().eq("telegram_user_id", uid).execute()
        except Exception:
            fail += 1

    await status_msg.edit_text(
        "✅ <b>Delete-Broadcast Complete!</b>\n\n"
        f"📬 Delivered to:          <b>{success}</b> users\n"
        f"🗑 Dead accounts removed: <b>{fail}</b>\n\n"
        f"⏳ Each message will auto-delete <b>15 min after it was sent</b>.",
        parse_mode="HTML"
    )

    # Delete each message exactly AUTO_DELETE_SECS after IT was sent.
    # Since sent_messages is in send order, remaining wait naturally shrinks
    # as we iterate — no 10k concurrent tasks needed.
    async def _delete_all_broadcast():
        for chat_id, msg_id, sent_at in sent_messages:
            remaining = AUTO_DELETE_SECS - (time.time() - sent_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass

    asyncio.create_task(_delete_all_broadcast())

# ── ADMIN: /stats ──────────────────────────────────────────────────────────────

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Today",     callback_data="stats_today"),
        InlineKeyboardButton(text="📆 This Week", callback_data="stats_week"),
        InlineKeyboardButton(text="📊 All Time",  callback_data="stats_all"),
    ]])
    await message.answer("📊 <b>Stats — choose a time range:</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("stats_"))
async def stats_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("⛔ Unauthorized.", show_alert=True)

    period = callback.data.split("_", 1)[1]  # today / week / all
    await callback.message.edit_reply_markup(reply_markup=None)
    thinking = await callback.message.edit_text("⏳ Crunching the numbers…")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        label = "Today"
    elif period == "week":
        since = (now - timedelta(days=7)).isoformat()
        label = "Last 7 Days"
    else:
        since = None
        label = "All Time"

    # ── Users ──────────────────────────────────────────────────────────────────
    user_q = supabase.table("users").select("telegram_user_id")
    if since:
        user_q = user_q.gte("created_at", since)
    total_users = len(user_q.execute().data)

    # ── Transactions ───────────────────────────────────────────────────────────
    tx_q = supabase.table("transactions") \
        .select("amount_paid, course_id, wallet_used, payment_type") \
        .eq("status", "approved")
    if since:
        tx_q = tx_q.gte("created_at", since)
    approved_txs = tx_q.execute().data

    awaiting_txs = supabase.table("transactions") \
        .select("id").eq("status", "awaiting_approval").execute().data

    total_approved  = len(approved_txs)
    pending_count   = len(awaiting_txs)
    total_revenue   = sum(float(tx.get("amount_paid") or 0) for tx in approved_txs)

    wallet_paid_count = len([tx for tx in approved_txs if tx.get("payment_type") == "wallet"])
    screenshot_count  = total_approved - wallet_paid_count
    total_wallet_used_in_sales = sum(float(tx.get("wallet_used") or 0) for tx in approved_txs)

    # ── Top 3 selling courses ──────────────────────────────────────────────────
    course_sales: dict = {}
    for tx in approved_txs:
        cid = tx.get("course_id", "unknown")
        course_sales[cid] = course_sales.get(cid, 0) + 1

    top3_courses = sorted(course_sales.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_course_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (cid, count) in enumerate(top3_courses):
        tc_row = supabase.table("courses").select("title").eq("course_id", cid).execute()
        name   = tc_row.data[0]["title"] if tc_row.data else cid
        top3_course_lines.append(f"   {medals[i]} {name}  —  <b>{count} sale(s)</b>")

    # ── Referrals ──────────────────────────────────────────────────────────────
    ref_q = supabase.table("referrals").select("referrer_id, status")
    if since:
        ref_q = ref_q.gte("created_at", since)
    all_refs = ref_q.execute().data

    total_refs       = len(all_refs)
    converted_refs   = len([r for r in all_refs if r["status"] == "purchased"])
    unique_referrers = len(set(r["referrer_id"] for r in all_refs))

    referrer_counts: dict = {}
    for r in all_refs:
        rid = r["referrer_id"]
        referrer_counts[rid] = referrer_counts.get(rid, 0) + 1
    top3_referrers = sorted(referrer_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # ── Wallet Economy ─────────────────────────────────────────────────────────
    comm_q = supabase.table("referral_commissions").select("commission_amt")
    if since:
        comm_q = comm_q.gte("created_at", since)
    commissions = comm_q.execute().data
    total_commission_paid = sum(float(c.get("commission_amt") or 0) for c in commissions)

    wallet_rows       = supabase.table("users").select("wallet_balance").execute().data
    total_wallet_held = sum(float(w.get("wallet_balance") or 0) for w in wallet_rows)

    # ── Courses (always all-time) ───────────────────────────────────────────────
    all_courses  = supabase.table("courses").select("course_id").execute().data
    total_items  = len(all_courses)
    bundle_count = len([c for c in all_courses if c["course_id"].startswith("bundle_")])
    course_count = total_items - bundle_count

    # ── Compose message ────────────────────────────────────────────────────────
    lines = [
        f"📊 <b>Admin Statistics — {label}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "👥 <b>USERS</b>",
        f"   Total registered users:    <b>{total_users}</b>",
        "",
        "💰 <b>SALES &amp; REVENUE</b>",
        f"   ✅ Approved sales:          <b>{total_approved}</b>",
        f"   💵 Total revenue (INR):     <b>₹{total_revenue:,.2f}</b>",
        f"   ⏳ Awaiting approval:       <b>{pending_count}</b>",
        f"   📸 Screenshot-paid sales:   <b>{screenshot_count}</b>",
        f"   💳 Wallet-paid sales:       <b>{wallet_paid_count}</b>",
        f"   🏦 Wallet used in sales:    <b>₹{total_wallet_used_in_sales:,.2f}</b>",
        "",
        "🏆 <b>TOP SELLING ITEMS</b>",
    ]
    if top3_course_lines:
        lines += top3_course_lines
    else:
        lines.append("   No sales yet.")

    lines += [
        "",
        "📚 <b>CATALOGUE</b>",
        f"   Individual courses:         <b>{course_count}</b>",
        f"   Bundles:                    <b>{bundle_count}</b>",
        f"   Total items:                <b>{total_items}</b>",
        "",
        "🔗 <b>REFERRAL PROGRAM</b>",
        f"   Total referral links used:  <b>{total_refs}</b>",
        f"   Converted to purchase:      <b>{converted_refs}</b>",
        f"   Active referrers:           <b>{unique_referrers}</b>",
        "",
        "🏅 <b>TOP REFERRERS</b>",
    ]
    if top3_referrers:
        for i, (rid, count) in enumerate(top3_referrers):
            lines.append(f"   {medals[i]} <code>{rid}</code>  —  <b>{count} referral(s)</b>")
    else:
        lines.append("   No referrals yet.")

    lines += [
        "",
        "💸 <b>WALLET ECONOMY</b>",
        f"   Total commissions paid:     <b>₹{total_commission_paid:,.2f}</b>",
        f"   Wallet balance (all users): <b>₹{total_wallet_held:,.2f}</b>",
        f"   Wallet-paid sales:          <b>{wallet_paid_count}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💡 <b>Net cash collected:</b>  "
        f"<b>₹{(total_revenue - total_wallet_used_in_sales):,.2f}</b>  "
        f"<i>(revenue minus wallet discounts)</i>",
    ]

    await thinking.edit_text("\n".join(lines), parse_mode="HTML")

# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

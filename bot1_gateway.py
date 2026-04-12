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
ADMIN_ID         = int(os.getenv("ADMIN_ID"))
BOT2_USERNAME    = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")
REFERRAL_PERCENT = 25
WELCOME_PHOTO    = "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg"
AUTO_DELETE_SECS = 900

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
        text="Buy All <del>3,𝟗𝟗𝟗₹ / 60$</del> ₹1,499 / 22$", 
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
        text="Buy All <del>3,𝟗𝟗𝟗₹ / 60$</del> ₹1,499 / 22$", 
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
async def execute_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    status_msg = await message.answer("⏳ Collecting user list…")

    rows         = supabase.table("users").select("telegram_user_id").execute().data
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
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users:          <b>{total_users}</b>\n"
        f"✅ Approved Sales:       <b>{total_sales}</b>\n"
        f"📚 Courses/Bundles:      <b>{total_courses}</b>\n"
        f"🔗 Total Referrals:      <b>{total_refs}</b>\n"
        f"💰 Referrals → Purchase: <b>{paid_refs}</b>",
        parse_mode="HTML"
    )

# ── Entry ──────────────────────────────────────────────────────────────────────

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

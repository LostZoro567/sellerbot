"""
Central config. Everything you may want to change lives here or in .env.
Secrets / payment details come from .env (see .env.example).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(f"{name} must be set in .env")
    return value


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def fmt_duration(secs: int) -> str:
    """3600 -> '1 hour', 900 -> '15 minutes'."""
    if secs >= 3600 and secs % 3600 == 0:
        h = secs // 3600
        return f"{h} hour" + ("s" if h != 1 else "")
    if secs >= 60 and secs % 60 == 0:
        m = secs // 60
        return f"{m} minute" + ("s" if m != 1 else "")
    return f"{secs} seconds"


# ── Identity / contacts ────────────────────────────────────────────────────────
ADMIN_ID          = int(_req("ADMIN_ID"))
BOT2_USERNAME     = os.getenv("BOT2_USERNAME", "ExclusiveCollectionVIP_bot")
ADMIN_CONTACT_URL = os.getenv("ADMIN_CONTACT_URL", "https://t.me/ProSeller_69")
DUMP_CHAT_ID      = int(os.getenv("DUMP_CHAT_ID", "-1003913013704"))

# ── Timers (seconds) ───────────────────────────────────────────────────────────
MENU_DELETE_SECS          = _int("MENU_DELETE_SECS", 1800)        # bot1 menu self-destruct
BROADCAST_DELETE_SECS     = _int("BROADCAST_DELETE_SECS", 1800)   # /dbroadcast self-destruct
PAYMENT_WINDOW_SECS       = _int("PAYMENT_WINDOW_SECS", 900)      # bot2 payment windows
DELIVERY_DELETE_SECS      = _int("DELIVERY_DELETE_SECS", 3600)    # delivered files lifetime
DELIVERY_WARN_BEFORE_SECS = _int("DELIVERY_WARN_BEFORE_SECS", 600)  # warn this long before deletion
ADMIN_REMINDER_SECS       = _int("ADMIN_REMINDER_HOURS", 12) * 3600
PENDING_WINDOW_SECS       = _int("PENDING_WINDOW_HOURS", 24) * 3600  # how long an opened item stays "payable"
RECOVERY_FIRST_SECS       = _int("RECOVERY_FIRST_SECS", 960)
RECOVERY_SECOND_SECS      = _int("RECOVERY_SECOND_SECS", 86400)

# ── Stats ──────────────────────────────────────────────────────────────────────
STATS_UTC_OFFSET_MINUTES = _int("STATS_UTC_OFFSET_MINUTES", 330)  # 330 = IST (UTC+5:30)
STATS_TZ_LABEL           = os.getenv("STATS_TZ_LABEL", "IST")

# ── Catalogue / pricing text ───────────────────────────────────────────────────
BUNDLE_PREFIX          = "bundle_"
BUNDLE_ALL_ID          = "bundle_all"
COMBO_OFFER_LINE       = "C||P + R||P :- 699₹ / 10$"
BUNDLE_REGULAR_PRICE   = "3,599₹ / 60$"
BUNDLE_OFFER_PRICE     = "1,499₹ / 22$"
BUNDLE_ALL_BUTTON_TEXT = "🏷 Buy All [₹1,499 / 22$]"

WELCOME_PHOTO         = os.getenv("WELCOME_PHOTO", "https://i.ibb.co/B2bDwTpH/2e4c69f3d0d9.jpg")
PAYMENT_OPTIONS_IMAGE = os.getenv("PAYMENT_OPTIONS_IMAGE", "https://i.ibb.co/hRNCTGZc/x.jpg")

WELCOME_CAPTION = (
    "🛒 <b>Telegram's Best Collection!</b>\n\n"
    f"🔥 Today's “Bundle” Offer : \n{COMBO_OFFER_LINE} \n\n"
    "✨ <b>Buy All Collection</b> :\n"
    f"<del>Regular Price : {BUNDLE_REGULAR_PRICE}</del> ❌\n\n"
    f"Bundle Offer = {BUNDLE_OFFER_PRICE} ✅"
)

# ── Payment methods ────────────────────────────────────────────────────────────
PAYMENT_BUTTONS = [
    ("qr",     "🧾 QR Code"),
    ("paytm",  "💸 Paytm / UPI"),
    ("paypal", "🌐 PayPal"),
    ("crypto", "🪙 Crypto (USDT)"),
    ("others", "💳 Other Methods"),
]
METHOD_LABELS = {
    "qr":     "QR Code",
    "paytm":  "Paytm / UPI",
    "paypal": "PayPal",
    "crypto": "Crypto (USDT)",
    "others": "Other",
}
USD_METHODS = {"paypal", "crypto"}   # these are counted in $, everything else in ₹


def build_payment_methods() -> dict:
    """Payment details are read from .env so they are not stored in the code."""
    upi    = _req("UPI_ID")
    paypal = _req("PAYPAL_EMAIL")
    usdt   = _req("USDT_ADDRESS")

    window = f"⏳ <i>Window closes in {fmt_duration(PAYMENT_WINDOW_SECS)}.</i>"
    send_ss = "📸 <b>Once paid:</b> send your payment screenshot right here."

    return {
        "qr": {
            "text": (
                "🧾 <b>QR Code Payment</b>\n\n"
                "Scan the QR code above to complete your payment.\n\n"
                f"{send_ss}\n\n{window}"
            ),
            "image": os.getenv("PAY_IMAGE_QR", "https://i.ibb.co/bMP4nQ7S/ee15c8361b23.jpg"),
        },
        "paytm": {
            "text": (
                "💸 <b>Paytm / UPI Payment</b>\n\n"
                "Send payment to the UPI ID below:\n\n"
                f"🔑 UPI ID: <code>{upi}</code>\n\n"
                f"{send_ss}\n\n{window}"
            ),
            "image": os.getenv("PAY_IMAGE_PAYTM", "https://i.ibb.co/Gf4dxt28/bdb68f4ab32e.jpg"),
        },
        "paypal": {
            "text": (
                "🌐 <b>PayPal Payment</b>\n\n"
                "Send payment to:\n\n"
                f"📧 <code>{paypal}</code>\n\n"
                f"{send_ss}\n\n{window}"
            ),
            "image": os.getenv("PAY_IMAGE_PAYPAL", "https://i.ibb.co/gLPBppVv/1d77334f059d.jpg"),
        },
        "crypto": {
            "text": (
                "🪙 <b>Crypto Payment — USDT (BEP20)</b>\n\n"
                "Send USDT to:\n\n"
                f"👛 <code>{usdt}</code>\n\n"
                "⚠️ <i>Send on BEP20 network only!</i>\n\n"
                f"{send_ss}\n\n{window}"
            ),
            "image": os.getenv("PAY_IMAGE_CRYPTO", "https://graph.org/file/60cf45bb50cf108f47196-28db3241840c7bc2db.jpg"),
        },
        "others": {
            "text": (
                "💳 <b>Other Payment Methods</b>\n\n"
                "Message the admin directly for other payment methods."
            ),
            "image": os.getenv("PAY_IMAGE_OTHERS", "https://i.ibb.co/Sw8CMtvz/b856f157559b.jpg"),
            "admin_button": True,
        },
    }

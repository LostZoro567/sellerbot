"""
Auto broadcast: every AUTOCAST_INTERVAL_SECS, send one random item from config.AUTO_BROADCASTS
to all active users. State (last cast time, shuffle bag, previous messages) lives in a small
JSON file, so a restart never causes an early or double cast.
"""
import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from config import (
    AUTO_BROADCASTS, AUTOCAST_DELETE_PREVIOUS, AUTOCAST_ENABLED, AUTOCAST_INTERVAL_SECS,
    AUTOCAST_PROTECT_CONTENT, AUTOCAST_STATE_FILE, BOT2_USERNAME, STATS_TZ_LABEL,
    STATS_UTC_OFFSET_MINUTES, fmt_duration,
)

LOCAL = timezone(timedelta(minutes=STATS_UTC_OFFSET_MINUTES))
_TYPES = {"video", "photo", "text"}
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ── State file ─────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(AUTOCAST_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _update(**kw) -> dict:
    state = _load()
    state.update(kw)
    tmp = AUTOCAST_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, AUTOCAST_STATE_FILE)
    return state


# ── Config validation ──────────────────────────────────────────────────────────

def valid_items() -> list[tuple[int, dict]]:
    out = []
    for i, it in enumerate(AUTO_BROADCASTS):
        kind = (it.get("type") or "").lower()
        text = it.get("text") or ""
        media = it.get("media") or ""
        if kind not in _TYPES:
            print(f"[autocast] item #{i}: type must be video/photo/text — skipped")
        elif kind in ("video", "photo") and (not media or media.startswith("PASTE")):
            print(f"[autocast] item #{i}: missing media — skipped")
        elif kind == "text" and not text:
            print(f"[autocast] item #{i}: empty text — skipped")
        elif len(text) > (4096 if kind == "text" else 1024):
            print(f"[autocast] item #{i}: text too long for Telegram — skipped")
        else:
            out.append((i, it))
    return out


def build_markup(item: dict):
    rows = []
    for row in item.get("buttons") or []:
        btns = []
        for b in row:
            if b.get("course"):
                url = f"https://t.me/{BOT2_USERNAME}?start={b['course']}"
            elif b.get("url"):
                url = b["url"]
            else:
                continue
            btns.append(InlineKeyboardButton(text=b.get("text", "Open"), url=url))
        if btns:
            rows.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_item(bot, chat_id: int, item: dict, protect: bool = AUTOCAST_PROTECT_CONTENT) -> int:
    kind = item["type"].lower()
    text = item.get("text") or None
    kb = build_markup(item)
    if kind == "video":
        m = await bot.send_video(chat_id, item["media"], caption=text, reply_markup=kb,
                                 parse_mode="HTML", protect_content=protect)
    elif kind == "photo":
        m = await bot.send_photo(chat_id, item["media"], caption=text, reply_markup=kb,
                                 parse_mode="HTML", protect_content=protect)
    else:
        m = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML",
                                   protect_content=protect, link_preview_options=_NO_PREVIEW)
    return m.message_id


# ── Picking & casting ──────────────────────────────────────────────────────────

def _pick(valid_idx: list[int]) -> int:
    """Shuffle-bag: every item once (random order) before any repeats; never the same twice in a row."""
    state = _load()
    bag = [i for i in state.get("bag", []) if i in valid_idx]
    if not bag:
        bag = valid_idx[:]
        random.shuffle(bag)
        if len(bag) > 1 and bag[0] == state.get("last_index"):
            bag.append(bag.pop(0))
    idx = bag.pop(0)
    _update(bag=bag, last_index=idx)
    return idx


async def _delete_previous(bot):
    prev = _load().get("last_messages") or {}
    for uid, mid in prev.items():
        try:
            await bot.delete_message(int(uid), int(mid))
        except Exception:
            pass
        await asyncio.sleep(0.03)
    _update(last_messages={})


async def cast_once(bot, broadcast_fn):
    items = dict(valid_items())
    if not items:
        return
    idx = _pick(list(items))
    item = items[idx]
    _update(last_cast=time.time())          # stamp first → a crash mid-cast can't trigger an instant re-cast

    if AUTOCAST_DELETE_PREVIOUS:
        await _delete_previous(bot)

    async def send(uid):
        return await send_item(bot, uid, item)

    results, blocked, failed = await broadcast_fn(send)
    _update(
        last_cast=time.time(),
        last_messages={str(uid): mid for uid, mid in results} if AUTOCAST_DELETE_PREVIOUS else {},
        last_result={"sent": len(results), "blocked": blocked, "failed": failed, "item": idx},
    )
    print(f"[autocast] item #{idx}: sent={len(results)} blocked={blocked} failed={failed}")


async def autocast_loop(bot, broadcast_fn):
    if not AUTOCAST_ENABLED:
        return print("[autocast] disabled (AUTOCAST_ENABLED=0)")
    if not valid_items():
        return print("[autocast] no valid items in AUTO_BROADCASTS — auto broadcast is off")
    if "last_cast" not in _load():
        _update(last_cast=time.time())      # first run: wait one full interval before the first cast
    print(f"[autocast] running — one cast every {fmt_duration(AUTOCAST_INTERVAL_SECS)}")
    while True:
        try:
            state = _load()
            if not state.get("paused") and time.time() - state.get("last_cast", 0) >= AUTOCAST_INTERVAL_SECS:
                await cast_once(bot, broadcast_fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[autocast] error: {e}")
        await asyncio.sleep(30)


# ── Admin controls ─────────────────────────────────────────────────────────────

def set_paused(paused: bool):
    _update(paused=paused)


def request_now():
    _update(last_cast=0, paused=False)      # loop picks it up within 30 seconds


def status_text() -> str:
    state = _load()
    n_valid, n_total = len(valid_items()), len(AUTO_BROADCASTS)
    if not AUTOCAST_ENABLED:
        status = "🔴 disabled in config"
    elif not n_valid:
        status = "🔴 no valid items in config"
    elif state.get("paused"):
        status = "⏸ paused"
    else:
        status = "🟢 running"

    lines = [
        "📡 <b>Auto Broadcast</b>",
        f"Status:   <b>{status}</b>",
        f"Items:    <b>{n_valid}</b> valid of {n_total}",
        f"Interval: <b>{fmt_duration(AUTOCAST_INTERVAL_SECS)}</b>",
    ]
    last = state.get("last_cast")
    if last:
        lines.append(f"Last cast: {datetime.fromtimestamp(last, LOCAL):%d %b %H:%M} {STATS_TZ_LABEL}")
        if n_valid and not state.get("paused"):
            nxt = datetime.fromtimestamp(last + AUTOCAST_INTERVAL_SECS, LOCAL)
            lines.append(f"Next cast: {nxt:%d %b %H:%M} {STATS_TZ_LABEL}")
    res = state.get("last_result")
    if res:
        lines.append(f"Last result: item #{res['item']} → {res['sent']} sent, {res['blocked']} blocked, {res['failed']} failed")
    lines += ["", "<code>/autocast pause</code> · <code>/autocast resume</code> · <code>/autocast now</code>",
              "<code>/autotest</code> — preview every item here (admin only)"]
    return "\n".join(lines)

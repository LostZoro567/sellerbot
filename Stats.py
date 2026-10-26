"""Admin statistics (used by bot1's /stats). All money is split: ₹ INR and $ USD are never mixed."""
import asyncio
import html
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BUNDLE_PREFIX, METHOD_LABELS, STATS_TZ_LABEL, STATS_UTC_OFFSET_MINUTES
from db import count_rows, fetch_all, parse_ts, supabase

LOCAL = timezone(timedelta(minutes=STATS_UTC_OFFSET_MINUTES))
METHOD_ORDER = ["qr", "paytm", "others", "paypal", "crypto", None]
PERIODS = [
    ("today", "Today"), ("yesterday", "Yesterday"), ("week", "7 Days"),
    ("month", "This Month"), ("lastmonth", "Last Month"), ("all", "All Time"),
]


def stats_keyboard(current: str) -> InlineKeyboardMarkup:
    def btn(key, label):
        return InlineKeyboardButton(
            text=f"• {label}" if key == current else label, callback_data=f"stats:{key}"
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(k, l) for k, l in PERIODS[:3]],
        [btn(k, l) for k, l in PERIODS[3:]],
        [btn("months", "📅 Monthly Breakdown")],
    ])


# ── Time helpers ───────────────────────────────────────────────────────────────

def _month_start(y: int, m: int) -> datetime:
    return datetime(y, m, 1, tzinfo=LOCAL)


def _next_month(y: int, m: int):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _bounds(period: str):
    now = datetime.now(LOCAL)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today, today + timedelta(days=1), f"Today · {today:%d %b %Y}"
    if period == "yesterday":
        s = today - timedelta(days=1)
        return s, today, f"Yesterday · {s:%d %b %Y}"
    if period == "week":
        s = today - timedelta(days=6)
        return s, today + timedelta(days=1), f"Last 7 Days · {s:%d %b} – {today:%d %b %Y}"
    if period == "month":
        s = _month_start(now.year, now.month)
        return s, _month_start(*_next_month(now.year, now.month)), f"This Month · {s:%B %Y}"
    if period == "lastmonth":
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        s = _month_start(y, m)
        return s, _month_start(*_next_month(y, m)), f"Last Month · {s:%B %Y}"
    return None, None, "All Time"


def _in_period(ts: datetime, start, end) -> bool:
    return (start is None or ts >= start) and (end is None or ts < end)


def _in_range(q, col: str, start, end):
    if start is not None:
        q = q.gte(col, start.astimezone(timezone.utc).isoformat())
    if end is not None:
        q = q.lt(col, end.astimezone(timezone.utc).isoformat())
    return q


def _ago(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# ── Aggregation helpers ────────────────────────────────────────────────────────

def _new_agg() -> dict:
    return {"n": 0, "inr_n": 0, "inr": 0.0, "usd_n": 0, "usd": 0.0}


def _is_usd(r: dict) -> bool:
    return (r.get("currency") or "INR").upper() == "USD"


def _add(a: dict, r: dict):
    amt = float(r.get("amount_paid") or 0)
    a["n"] += 1
    if _is_usd(r):
        a["usd_n"] += 1
        a["usd"] += amt
    else:
        a["inr_n"] += 1
        a["inr"] += amt


def _money(a: dict) -> str:
    parts = []
    if a["inr_n"]:
        parts.append(f"₹{a['inr']:,.2f}")
    if a["usd_n"]:
        parts.append(f"${a['usd']:,.2f}")
    return " + ".join(parts) if parts else "—"


async def _load_approved() -> list:
    rows = await fetch_all(
        lambda: supabase.table("transactions")
        .select("id, telegram_user_id, course_id, amount_paid, currency, payment_method, created_at, approved_at")
        .eq("status", "approved")
        .order("id")
    )
    for r in rows:
        r["ts"] = parse_ts(r.get("approved_at") or r["created_at"])
    return rows


# ── Period report ──────────────────────────────────────────────────────────────

async def build_period_text(period: str) -> str:
    start, end, label = _bounds(period)
    now = datetime.now(LOCAL)

    def tx_count(status=None):
        q = supabase.table("transactions").select("id", count="exact")
        if status:
            q = q.eq("status", status)
        return count_rows(_in_range(q, "created_at", start, end))

    def user_count(active=None, ranged=False):
        q = supabase.table("users").select("telegram_user_id", count="exact")
        if active is not None:
            q = q.eq("is_active", active)
        if ranged:
            q = _in_range(q, "created_at", start, end)
        return count_rows(q)

    (approved_all, courses, awaiting, new_users, total_users, active_users,
     n_rejected, n_redelivered, n_opened) = await asyncio.gather(
        _load_approved(),
        fetch_all(lambda: supabase.table("courses").select("course_id, title").order("course_id")),
        fetch_all(lambda: supabase.table("transactions").select("id, submitted_at, created_at")
                  .eq("status", "awaiting_approval").order("id")),
        user_count(ranged=True),
        user_count(),
        user_count(active=True),
        tx_count("rejected"),
        tx_count("redelivered"),
        tx_count(),
    )

    titles = {c["course_id"]: c["title"] for c in courses}
    rows = [r for r in approved_all if _in_period(r["ts"], start, end)]

    first_ts = {}
    for r in approved_all:
        u = r["telegram_user_id"]
        if u not in first_ts or r["ts"] < first_ts[u]:
            first_ts[u] = r["ts"]

    total = _new_agg()
    by_method, by_course, by_kind, by_day = (defaultdict(_new_agg) for _ in range(4))
    buyers = defaultdict(int)
    for r in rows:
        _add(total, r)
        _add(by_method[r.get("payment_method")], r)
        _add(by_course[r["course_id"]], r)
        _add(by_kind["bundle" if r["course_id"].startswith(BUNDLE_PREFIX) else "course"], r)
        _add(by_day[r["ts"].astimezone(LOCAL).date()], r)
        buyers[r["telegram_user_id"]] += 1

    L = [
        f"📊 <b>Stats — {label}</b>",
        f"<i>{STATS_TZ_LABEL} · updated {now:%H:%M:%S}</i>",
        "",
        "💰 <b>SALES &amp; REVENUE</b>",
        f"   Approved sales:   <b>{total['n']}</b>",
        f"   ₹ INR revenue:    <b>₹{total['inr']:,.2f}</b>  ({total['inr_n']} sales)",
        f"   $ USD revenue:    <b>${total['usd']:,.2f}</b>  ({total['usd_n']} sales)",
    ]
    if total["inr_n"]:
        L.append(f"   Avg order (₹):    ₹{total['inr'] / total['inr_n']:,.2f}")
    if total["usd_n"]:
        L.append(f"   Avg order ($):    ${total['usd'] / total['usd_n']:,.2f}")

    L += ["", "💳 <b>BY PAYMENT METHOD</b>"]
    any_method = False
    for key in METHOD_ORDER:
        a = by_method.get(key)
        if a and a["n"]:
            any_method = True
            name = METHOD_LABELS.get(key) or "Not recorded (older sales)"
            L.append(f"   {name}: <b>{a['n']}</b> — {_money(a)}")
    if not any_method:
        L.append("   No sales in this period.")

    L += ["", "🏆 <b>TOP ITEMS</b>"]
    top = sorted(by_course.items(), key=lambda kv: (kv[1]["n"], kv[1]["inr"], kv[1]["usd"]), reverse=True)[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    if top:
        for i, (cid, a) in enumerate(top):
            name = html.escape(titles.get(cid, cid))[:40]
            L.append(f"   {medals[i]} {name} — <b>{a['n']}</b> — {_money(a)}")
    else:
        L.append("   No sales in this period.")

    c, b = by_kind.get("course", _new_agg()), by_kind.get("bundle", _new_agg())
    L += [
        "",
        "📦 <b>COURSES vs BUNDLES</b>",
        f"   Single courses: <b>{c['n']}</b> — {_money(c)}",
        f"   Bundles:        <b>{b['n']}</b> — {_money(b)}",
    ]

    unique = len(buyers)
    repeat = sum(1 for n in buyers.values() if n > 1)
    L += ["", "👥 <b>CUSTOMERS</b>", f"   Unique buyers:   <b>{unique}</b>"]
    if period != "all":
        new_buyers = sum(1 for u in buyers if _in_period(first_ts[u], start, end))
        L.append(f"   First-time:      <b>{new_buyers}</b>")
        L.append(f"   Returning:       <b>{unique - new_buyers}</b>")
    L.append(f"   Bought 2+ times: <b>{repeat}</b>")
    if period != "all":
        L.append(f"   New users joined: <b>{new_users}</b>")
    L.append(f"   Total users:     <b>{total_users}</b>  (active {active_users}, blocked {total_users - active_users})")

    L += ["", "🔄 <b>PIPELINE</b>", f"   Checkouts opened: <b>{n_opened}</b>"]
    if n_opened:
        L.append(f"   Conversion:       <b>{total['n'] / n_opened * 100:.1f}%</b>")
    L += [f"   Rejected:         <b>{n_rejected}</b>", f"   Re-delivered:     <b>{n_redelivered}</b>"]
    if awaiting:
        oldest = min(parse_ts(a.get("submitted_at") or a["created_at"]) for a in awaiting)
        L.append(f"   ⏳ Awaiting approval now: <b>{len(awaiting)}</b> (oldest {_ago(datetime.now(timezone.utc) - oldest)})")
    else:
        L.append("   ⏳ Awaiting approval now: <b>0</b>")

    if by_day and period not in ("today", "yesterday"):
        day, a = max(by_day.items(), key=lambda kv: (kv[1]["n"], kv[1]["inr"] + kv[1]["usd"]))
        L += ["", f"📈 <b>Best day:</b> {day:%d %b %Y} — <b>{a['n']}</b> sales — {_money(a)}"]

    n_bundles = sum(1 for cid in titles if cid.startswith(BUNDLE_PREFIX))
    L += ["", f"📚 <b>Catalogue:</b> {len(titles) - n_bundles} courses · {n_bundles} bundles"]
    L += ["", "<i>Re-delivered / rejected / cancelled payments are never counted as sales.</i>"]
    return "\n".join(L)


# ── Monthly breakdown ──────────────────────────────────────────────────────────

async def build_monthly_text() -> str:
    approved_all = await _load_approved()
    now = datetime.now(LOCAL)

    months, y, m = [], now.year, now.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)

    agg = defaultdict(_new_agg)
    for r in approved_all:
        loc = r["ts"].astimezone(LOCAL)
        _add(agg[(loc.year, loc.month)], r)

    new_users = await asyncio.gather(*[
        count_rows(_in_range(
            supabase.table("users").select("telegram_user_id", count="exact"),
            "created_at", _month_start(y, m), _month_start(*_next_month(y, m)),
        )) for y, m in months
    ])

    head = f"{'Month':<9}{'Sales':>6}{'INR (₹)':>13}{'USD ($)':>10}{'NewUsr':>8}"
    lines = [head, "-" * len(head)]
    t = _new_agg()
    t_users = 0
    for (y, m), nu in zip(months, new_users):
        a = agg.get((y, m), _new_agg())
        lines.append(f"{y}-{m:02d}  {a['n']:>6}{a['inr']:>13,.2f}{a['usd']:>10,.2f}{nu:>8}")
        for k in t:
            t[k] += a[k]
        t_users += nu
    lines.append("-" * len(head))
    lines.append(f"{'12 mo':<9}{t['n']:>6}{t['inr']:>13,.2f}{t['usd']:>10,.2f}{t_users:>8}")

    all_t = _new_agg()
    for r in approved_all:
        _add(all_t, r)

    return (
        "📅 <b>Monthly Breakdown — last 12 months</b>\n"
        f"<i>{STATS_TZ_LABEL} · updated {now:%H:%M:%S}</i>\n\n"
        f"<pre>{html.escape(chr(10).join(lines))}</pre>\n"
        f"🧮 <b>All-time:</b> {all_t['n']} sales · ₹{all_t['inr']:,.2f} · ${all_t['usd']:,.2f}\n\n"
        "<i>PayPal / USDT sales are counted in $, everything else in ₹. "
        "Sales are dated by approval time.</i>"
    )

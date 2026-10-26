import os
import re
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

if not url or not key:
    raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(url, key)


# ── Async helpers ──────────────────────────────────────────────────────────────
# supabase-py is synchronous. Running .execute() in a worker thread keeps the
# bot's event loop free while the database call is in flight.

async def aexec(query):
    """await aexec(supabase.table("x").select("*").eq("a", 1))"""
    return await asyncio.to_thread(query.execute)


async def fetch_all(make_query, page_size: int = 1000) -> list:
    """
    Fetch EVERY row of a query (Supabase caps a single response at 1000 rows).
    `make_query` must be a function returning a fresh select query each call:
        await fetch_all(lambda: supabase.table("users").select("*").order("id"))
    """
    rows, start = [], 0
    while True:
        res = await aexec(make_query().range(start, start + page_size - 1))
        data = res.data or []
        rows.extend(data)
        if len(data) < page_size:
            break
        start += page_size
    return rows


async def count_rows(query) -> int:
    """Exact row count. Query must be built with select(col, count="exact")."""
    res = await aexec(query.limit(1))
    return res.count or 0


_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)?$"
)


def parse_ts(value) -> datetime:
    """Parse a Postgres/Supabase timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        m = _TS_RE.match(s)
        if m:
            date_, time_, frac, tz = m.groups()
            frac = (frac or ".0")[1:7].ljust(6, "0")
            if not tz or tz == "Z":
                tz = "+00:00"
            elif len(tz) == 3:
                tz += ":00"
            elif len(tz) == 5:
                tz = tz[:3] + ":" + tz[3:]
            dt = datetime.fromisoformat(f"{date_}T{time_}.{frac}{tz}")
        else:
            dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

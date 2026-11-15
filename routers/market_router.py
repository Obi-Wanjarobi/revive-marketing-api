"""
Live market data for the Pulse Marketing dashboard (2026-11-14).

GET /market/rates  -> published benchmark rates from FRED (St. Louis Fed), no API key needed:
    MORTGAGE30US  Freddie Mac PMMS 30-yr fixed (weekly, Thursdays)
    MORTGAGE15US  Freddie Mac PMMS 15-yr fixed (weekly)
    OBMMIFHA30YF  Optimal Blue 30-yr FHA rate-lock index (daily, business days)
    DGS10         10-yr Treasury yield (daily)
    SOFR          Secured Overnight Financing Rate (daily)
  These are published averages / indexes, NOT quotes. Each value carries its own date.
GET /market/news   -> latest mortgage/housing headlines (title + link + source) from public RSS.

Both are cached server-side so the dashboard never hammers the sources. If a source
fails, the last good value is served with stale=True; nothing is ever made up.
"""
import csv
import io
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from fastapi import APIRouter, Depends

from auth import get_current_agent

router = APIRouter(prefix="/market", tags=["market"])

_UA = {"User-Agent": "RevivePulse/1.0 (+https://mypulsemarketing.com)"}
_RATES_TTL = 30 * 60
_NEWS_TTL = 15 * 60
_cache = {"rates": {"at": 0.0, "body": None}, "news": {"at": 0.0, "body": None}}

SERIES = [
    ("MORTGAGE30US", "30-Yr Fixed", "Freddie Mac PMMS"),
    ("MORTGAGE15US", "15-Yr Fixed", "Freddie Mac PMMS"),
    ("OBMMIFHA30YF", "FHA 30-Yr", "Optimal Blue index"),
    ("DGS10", "10-Yr Treasury", "U.S. Treasury"),
    ("SOFR", "SOFR", "NY Fed"),
]

FEEDS = [
    ("Mortgage News Daily", "https://www.mortgagenewsdaily.com/rss/news"),
    ("HousingWire", "https://www.housingwire.com/feed/"),
]


def _get(url: str, timeout: int = 10) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def _series(sid: str):
    """Return [(date, value), ...] for the last ~60 days, skipping blank/holiday rows."""
    start = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
    raw = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}", timeout=25).decode("utf-8", "replace")
    out = []
    for row in csv.reader(io.StringIO(raw)):
        if len(row) < 2:
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue  # header row or "." / blank for holidays
    return out


@router.get("/rates")
def market_rates(agent=Depends(get_current_agent)):
    c = _cache["rates"]
    if c["body"] and time.time() - c["at"] < _RATES_TTL:
        return c["body"]
    prev = {r["id"]: r for r in (c["body"] or {}).get("rates", [])}
    rates, failed = [], 0

    def safe(sid):
        try:
            return _series(sid)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=len(SERIES)) as ex:
        fetched = dict(zip([s[0] for s in SERIES], ex.map(safe, [s[0] for s in SERIES])))
    for sid, label, src in SERIES:
        try:
            pts = fetched.get(sid)
            if not pts:
                raise ValueError("empty")
            d, v = pts[-1]
            chg = round(v - pts[-2][1], 3) if len(pts) > 1 else None
            rates.append({"id": sid, "label": label, "value": v, "change": chg, "as_of": d, "source": src, "stale": False})
        except Exception:
            failed += 1
            if sid in prev:
                rates.append({**prev[sid], "stale": True})
    body = {"rates": rates, "fetched_at": datetime.utcnow().isoformat() + "Z",
            "note": "Published averages and indexes, not a quote.", "credit": "Data: FRED, Federal Reserve Bank of St. Louis"}
    if rates and failed < len(SERIES):
        _cache["rates"] = {"at": time.time(), "body": body}
    return body


def _feed(name: str, url: str):
    root = ET.fromstring(_get(url))
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        ts = None
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except Exception:
            pass
        items.append({"title": title[:200], "link": link, "source": name, "ts": ts})
        if len(items) >= 10:
            break
    return items


@router.get("/news")
def market_news(agent=Depends(get_current_agent)):
    c = _cache["news"]
    if c["body"] and time.time() - c["at"] < _NEWS_TTL:
        return c["body"]
    per_feed = []
    for name, url in FEEDS:
        try:
            per_feed.append(_feed(name, url))
        except Exception:
            continue
    items = [it for group in zip(*per_feed) for it in group] if len(per_feed) > 1 else (per_feed[0] if per_feed else [])
    if not items and c["body"]:
        return {**c["body"], "stale": True}
    body = {"items": items[:12], "fetched_at": datetime.utcnow().isoformat() + "Z", "stale": False}
    if items:
        _cache["news"] = {"at": time.time(), "body": body}
    return body

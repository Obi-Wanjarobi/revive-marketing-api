"""
Pipeline summary for the Pulse Marketing dashboard widget (2026-11-10).

Reads the Pulse CRM full-pipeline backup (the same snapshot the CRM's
"Back up now" / auto-backup writes) SERVER-SIDE and returns COUNTS ONLY.
No borrower names, numbers, emails or addresses ever leave this endpoint.

Stage rules mirror the CRM's own pipeline tiles (mypulsecrm.com, tiles()):
  active        = not Funded, not On hold/withdrawn, not Prospect
  underwriting  = Underwriting + Conditional
  closing month = active files whose close date is in the current month
  funded month  = Funded files whose close date is in the current month

Env vars (Railway -> Marketing Engine -> revive-marketing-api -> Variables):
  PULSE_API_BASE      = https://revive-loan-portal-api-production.up.railway.app
  PULSE_INTERNAL_KEY  = that service's INTERNAL_API_KEY (Boss pastes it)
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

import pytz
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_agent
from core.config import get_settings
from models import Agent

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

_CACHE = {"at": 0.0, "body": None}
_TTL_SECONDS = 60
_TZ = pytz.timezone("America/Los_Angeles")
_PROSPECT = {"Lead", "Prospect", "Pitched", "Pre-Qual", "Pre-Approved"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _group(stage) -> str:
    st = str(stage or "")
    if st in _PROSPECT or re.match(r"^(prospect|lead)", st, re.I):
        return "prospect"
    if re.match(r"^cond", st, re.I):
        return "cond"
    if re.search(r"clear to close|^ctc|^docs", st, re.I):
        return "ctc"
    if re.search(r"funded|closed loan|^closed$", st, re.I):
        return "funded"
    if re.search(r"hold|withdrawn|denied|cancel", st, re.I):
        return "closed"
    if re.search(r"^uw$|underwrit", st, re.I):
        return "underwriting"
    if re.search(r"process", st, re.I):
        return "processing"
    return "application"


def _num(v) -> float:
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v if v is not None else "")) or 0)
    except ValueError:
        return 0.0


def _close_month(v, now):
    """Return (year, month) of a close date in any of the formats the CRM stores, or None."""
    s = str(v or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)                     # 2026-11-20
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", s)      # 11/20/2026, 11/20/26, 11/20
    if m:
        y = m.group(3)
        y = now.year if not y else (2000 + int(y) if len(y) == 2 else int(y))
        return y, int(m.group(1))
    m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+\d{1,2}(?:,?\s+(\d{4}))?", s)  # Nov 20, 2026 / Nov 20
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2) or now.year), _MONTHS[m.group(1).lower()]
    return None


def _fetch_snapshot(base: str, key: str) -> dict:
    url = base.rstrip("/") + "/internal/pulse/snapshot?" + urllib.parse.urlencode({"key": "pipeline"})
    req = urllib.request.Request(url, headers={"x-internal-key": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        body = json.loads(r.read().decode("utf-8") or "{}")
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, str):
        data = json.loads(data or "{}")
    return data or {}


@router.get("/summary")
def pipeline_summary(agent: Agent = Depends(get_current_agent)):
    s = get_settings()
    if not s.pulse_api_base or not s.pulse_internal_key:
        raise HTTPException(status_code=503, detail="pipeline_not_configured")

    if _CACHE["body"] and time.time() - _CACHE["at"] < _TTL_SECONDS:
        return _CACHE["body"]

    try:
        data = _fetch_snapshot(s.pulse_api_base, s.pulse_internal_key)
    except Exception:
        raise HTTPException(status_code=502, detail="pipeline_source_unreachable")

    loans = data.get("loans") or {}
    if isinstance(loans, dict):
        loans = list(loans.values())
    loans = [L for L in loans if isinstance(L, dict)]

    now = datetime.now(_TZ)
    this_month = (now.year, now.month)
    out = {"active": 0, "underwriting": 0, "closing_this_month": 0, "closing_volume": 0.0,
           "funded_this_month": 0, "funded_volume": 0.0, "funded_total": 0}
    for L in loans:
        g = _group(L.get("stage"))
        in_month = _close_month(L.get("close"), now) == this_month
        if g == "funded":
            out["funded_total"] += 1
            if in_month:
                out["funded_this_month"] += 1
                out["funded_volume"] += _num(L.get("amt"))
            continue
        if g in ("closed", "prospect"):
            continue
        out["active"] += 1
        if g in ("underwriting", "cond"):
            out["underwriting"] += 1
        if in_month:
            out["closing_this_month"] += 1
            out["closing_volume"] += _num(L.get("amt"))

    out["as_of"] = data.get("saved")
    out["month"] = now.strftime("%B %Y")
    out["source"] = "Pulse CRM pipeline backup"
    _CACHE.update(at=time.time(), body=out)
    return out

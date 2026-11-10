"""
Pipeline + Pulse Intelligence for the Pulse Marketing dashboard (2026-11-10).

Reads the Pulse CRM full-pipeline backup (the same snapshot the CRM's
"Back up now" / auto-backup writes) SERVER-SIDE.

  GET /pipeline/summary       counts only (any logged-in agent)
  GET /pipeline/intelligence  PCRM "Pulse Intelligence" aggregates (any agent;
                              employee names only, no borrower data) PLUS the
                              "Pulse Review — Top Priorities" loan list, which
                              names borrowers and is returned to ADMINS ONLY.

Stage rules mirror mypulsecrm.com (tiles()/group() and the dashboard compute()).
Lock / close dates use the loan record's own fields (lockExp|lock, close|
estClosing|closeDate); PCRM can additionally read LOS-mapped dates in-browser.

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

_SNAP = {"at": 0.0, "data": None}
_TTL_SECONDS = 60
_TZ = pytz.timezone("America/Los_Angeles")
_PROSPECT = {"Lead", "Prospect", "Pitched", "Pre-Qual", "Pre-Approved"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_STAGES = ["Application", "Processing", "Underwriting", "Conditions", "Clear to Close", "Funded"]


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


def _parse_date(v, now):
    """Parse the date formats the CRM stores. Returns a tz-aware datetime (Pacific) or None."""
    s = str(v or "").strip()
    if not s or s.upper() == "TBD":
        return None
    y = mo = d = None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)                         # 2026-11-20
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?", s)     # 11/20/2026, 11/20/26, 11/20
        if m:
            yy = m.group(3)
            y = now.year if not yy else (2000 + int(yy) if len(yy) == 2 else int(yy))
            mo, d = int(m.group(1)), int(m.group(2))
        else:
            m = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?", s)  # Nov 20, 2026
            if m and m.group(1).lower() in _MONTHS:
                y, mo, d = int(m.group(3) or now.year), _MONTHS[m.group(1).lower()], int(m.group(2))
    if y is None:
        return None
    try:
        return _TZ.localize(datetime(y, mo, d, 12, 0))
    except ValueError:
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


def _load():
    """Return (loans:list[dict], saved) from the cached CRM snapshot."""
    s = get_settings()
    if not s.pulse_api_base or not s.pulse_internal_key:
        raise HTTPException(status_code=503, detail="pipeline_not_configured")
    if _SNAP["data"] is None or time.time() - _SNAP["at"] >= _TTL_SECONDS:
        try:
            _SNAP["data"] = _fetch_snapshot(s.pulse_api_base, s.pulse_internal_key)
            _SNAP["at"] = time.time()
        except Exception:
            raise HTTPException(status_code=502, detail="pipeline_source_unreachable")
    data = _SNAP["data"] or {}
    loans = data.get("loans") or {}
    if isinstance(loans, dict):
        loans = list(loans.values())
    return [L for L in loans if isinstance(L, dict)], data.get("saved")


@router.get("/summary")
def pipeline_summary(agent: Agent = Depends(get_current_agent)):
    loans, saved = _load()
    now = datetime.now(_TZ)
    out = {"active": 0, "underwriting": 0, "closing_this_month": 0, "closing_volume": 0.0,
           "funded_this_month": 0, "funded_volume": 0.0, "funded_total": 0}
    for L in loans:
        g = _group(L.get("stage"))
        cd = _parse_date(L.get("close"), now)
        in_month = bool(cd and (cd.year, cd.month) == (now.year, now.month))
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
    out["as_of"] = saved
    out["month"] = now.strftime("%B %Y")
    out["source"] = "Pulse CRM pipeline backup"
    return out


def _merge_names(mp: dict, blanks: int) -> dict:
    """PCRM mergeNames(): fold a one-word name into the full name with that last name."""
    names = list(mp.keys())
    canon = {}
    for n in names:
        p = n.split()
        if len(p) == 1:
            full = next((o for o in names if len(o.split()) > 1 and o.split()[-1].lower() == p[0].lower()), None)
            canon[n] = full or n
        else:
            canon[n] = n
    merged = {}
    for n in names:
        t = merged.setdefault(canon[n], {"n": 0, "vol": 0.0, "active": 0, "funded": 0})
        for k in t:
            t[k] += mp[n][k]
    if blanks:
        merged["Unassigned"] = {"n": blanks, "vol": 0.0, "active": 0, "funded": 0}
    return merged


def _table(mp: dict):
    rows = sorted(mp.items(), key=lambda kv: -kv[1]["n"])[:10]
    return [{"name": k, "loans": v["n"], "active": v["active"], "funded": v["funded"],
             "volume": round(v["vol"], 2), "avg": round(v["vol"] / v["n"], 2) if v["n"] else 0} for k, v in rows]


@router.get("/intelligence")
def pipeline_intelligence(agent: Agent = Depends(get_current_agent)):
    loans, saved = _load()
    now = datetime.now(_TZ)
    now_ts = now.timestamp()
    m = {"total": 0, "active": 0, "volume": 0.0, "uw": 0, "ctc": 0, "funded": 0, "withdrawn": 0,
         "lock_soon": 0, "no_lo": 0, "no_proc": 0}
    by_stage, by_type, by_lo, by_proc, needs = {}, {}, {}, {}, []

    for o in loans:
        m["total"] += 1
        st = o.get("stage") or "?"
        by_stage[st] = by_stage.get(st, 0) + 1
        ty = o.get("type") or "Other"
        by_type[ty] = by_type.get(ty, 0) + 1
        live = st not in ("Funded", "Withdrawn", "Application")
        for field, bucket, blank in (("off", by_lo, "no_lo"), ("proc", by_proc, "no_proc")):
            nm = str(o.get(field) or "").strip()
            if not nm:
                m[blank] += 1
                continue
            e = bucket.setdefault(nm, {"n": 0, "vol": 0.0, "active": 0, "funded": 0})
            e["n"] += 1
            e["vol"] += _num(o.get("amt"))
            e["active"] += 1 if live else 0
            e["funded"] += 1 if st == "Funded" else 0
        lock = _parse_date(o.get("lockExp") or o.get("lock"), now)
        close = _parse_date(o.get("close") or o.get("estClosing") or o.get("closeDate"), now)
        if live:
            m["active"] += 1
            m["volume"] += _num(o.get("amt"))
            if lock and (lock.timestamp() - now_ts) / 86400 <= 7:
                m["lock_soon"] += 1
        m["uw"] += st == "Underwriting"
        m["ctc"] += st == "Clear to Close"
        m["funded"] += st == "Funded"
        m["withdrawn"] += st == "Withdrawn"

        if st not in ("Funded", "Withdrawn"):
            c = o.get("cond") if isinstance(o.get("cond"), dict) else {}
            outc = (c.get("t") - (c.get("c") or 0)) if isinstance(c.get("t"), (int, float)) else 0
            if st == "Clear to Close":
                act, w = "schedule closing", 6
            elif st == "On Hold":
                act, w = "on hold — action needed to resume", 6
            elif st == "Conditions":
                act, w = (f"{outc} condition{'s' if outc > 1 else ''} to clear" if outc > 0 else "final condition review"), 5
            elif st == "Underwriting":
                act, w = "in underwriting — awaiting decision", 4
            elif st == "Processing":
                act, w = "ready to submit to underwriting", 3
            elif st == "Application":
                act, w = "collect docs & start processing", 2
            else:
                act, w = "needs review", 1
            ts, kind = None, ""
            if lock and (close is None or lock <= close):
                ts, kind = lock.timestamp(), "lock"
            elif close:
                ts, kind = close.timestamp(), "close"
            if ts is not None and ts - now_ts < -45 * 86400:
                ts = None
            if ts is not None:
                dd = round((ts - now_ts) / 86400)
                noun = "rate lock" if kind == "lock" else "closing"
                verb = "locks" if kind == "lock" else "closes"
                if dd < 0:
                    dot, sub = "red", f"{noun} {-dd}d overdue"
                elif dd <= 3:
                    dot, sub = "red", f"{verb} in {dd}d"
                elif dd <= 14:
                    dot, sub = "amber", f"{verb} in {dd}d"
                else:
                    dot, sub = ("green" if st == "Clear to Close" else "blue"), f"{verb} in {dd}d"
            else:
                ts = float("inf")
                dot = "green" if st == "Clear to Close" else "blue"
                sub = "awaiting " + ("lock date" if kind == "lock" else "closing date")
            needs.append({"dot": dot, "name": o.get("name") or o.get("num") or "Loan",
                          "stage": st, "action": act, "due": sub, "_ts": ts, "_w": w})

    needs.sort(key=lambda x: (x["_ts"], -x["_w"]))
    seen, pri = set(), []
    for x in needs:
        k = str(x["name"]).strip().lower()
        if k and k in seen:
            continue
        seen.add(k)
        pri.append({k2: v for k2, v in x.items() if not k2.startswith("_")})

    lo = _merge_names(by_lo, m["no_lo"])
    proc = _merge_names(by_proc, m["no_proc"])
    assigned = m["total"] - m["no_lo"]
    top = max(((k, v["n"]) for k, v in lo.items() if k != "Unassigned"), key=lambda kv: kv[1], default=("—", 0))
    is_admin = str(getattr(agent, "role", "")).split(".")[-1] == "admin"

    return {
        "kpis": {"active": m["active"], "volume": round(m["volume"], 2), "underwriting": m["uw"],
                 "clear_to_close": m["ctc"], "funded": m["funded"]},
        "health": {
            "stalled_intake": by_stage.get("Application", 0),
            "no_lo": m["no_lo"], "total": m["total"],
            "lo_concentration": {"pct": round(top[1] / assigned * 100) if assigned else 0, "name": top[0], "n": top[1]},
            "locks_expiring_7d": m["lock_soon"],
            "bottleneck": max(by_stage.items(), key=lambda kv: kv[1])[0] if by_stage else "—",
        },
        "by_stage": [{"stage": s, "n": by_stage.get(s, 0)} for s in _STAGES],
        "by_type": [{"type": k, "n": v} for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:5]],
        "completed": {"withdrawn": m["withdrawn"], "funded": m["funded"]},
        "lo_production": _table(lo),
        "processor_production": _table(proc),
        "priorities": pri[:15] if is_admin else None,
        "priorities_count": len(pri),
        "priorities_restricted": not is_admin,
        "as_of": saved,
        "source": "Pulse CRM pipeline backup",
    }

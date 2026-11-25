"""
Lead flow / floor manager (2026-11-25).

Boss's rules:
- Every lead on the master list has exactly ONE owner (auto-assigned on intake,
  least-loaded round-robin; admins can still re-assign by hand).
- A lead the owner has not called within the stale window goes back to the pool
  and is re-assigned to someone else, so nothing goes cold.
- Q (and the dashboard) push each person to work their own uncalled leads.

Compliance (EPSILON):
- Only DIALABLE leads ever enter a call queue or the stale rotation: documented
  call consent, not DNC, not on the suppressed list, real phone, not archived.
  Consent is never inferred here.
- The stale clock only counts the lead's CALLABLE hours (quiet-hours window in
  the LEAD's time zone), so nobody loses a lead overnight.
- Nothing here dials, texts or emails anyone.

Everything is reversible and audited: each owner change is appended to
contacts.assignment_log {at, from, to, by, reason}.
"""
import json
import logging
import threading
from datetime import datetime, timedelta, time as dtime

import pytz
from sqlalchemy.orm import Session

from core.compliance import is_suppressed
from core.config import get_settings
from models import Agent, AppSetting, Contact

log = logging.getLogger("lead_flow")
settings = get_settings()

DEFAULTS = {
    "stale_enabled": "true",   # sweep on/off
    "stale_hours": "4",        # callable hours an owner gets before the lead rotates
    "max_rotations": "3",      # after this many stale hand-offs the lead is flagged for a manager instead
}
SWEEP_EVERY_SECONDS = 600


# ---------------------------------------------------------------- settings
def get_setting(db: Session, key: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value is not None else DEFAULTS.get(key, "")


def all_settings(db: Session) -> dict:
    return {
        "stale_enabled": get_setting(db, "stale_enabled").lower() == "true",
        "stale_hours": float(get_setting(db, "stale_hours") or 4),
        "max_rotations": int(float(get_setting(db, "max_rotations") or 3)),
    }


def set_setting(db: Session, key: str, value: str, by: str | None):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is None:
        row = AppSetting(key=key)
        db.add(row)
    row.value = value
    row.updated_by = by
    row.updated_at = datetime.utcnow()


# ---------------------------------------------------------------- owner changes
def _log(contact: Contact) -> list:
    try:
        v = json.loads(contact.assignment_log) if contact.assignment_log else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def set_owner(contact: Contact, agent_id: str | None, by: str | None, reason: str, now: datetime | None = None):
    """The ONE way to change a lead's owner. Restarts the stale clock and appends to the audit log."""
    now = now or datetime.utcnow()
    prev = contact.assigned_agent_id
    contact.assigned_agent_id = agent_id
    contact.assigned_at = now if agent_id else None
    entries = _log(contact)
    entries.append({"at": now.isoformat(), "from": prev, "to": agent_id, "by": by, "reason": reason})
    contact.assignment_log = json.dumps(entries[-50:])


def stale_rotations(contact: Contact) -> int:
    return sum(1 for e in _log(contact) if e.get("reason") == "stale")


# ---------------------------------------------------------------- dialable / callable time
def dialable(db: Session, c: Contact) -> bool:
    """Same rules as the dial gate (minus the clock): consent, not DNC, not suppressed, real phone, active."""
    if not c.phone or c.archived or c.dnc_flag or not c.consent_call:
        return False
    return not is_suppressed(db, c.phone, None)


def _tz(c: Contact):
    try:
        return pytz.timezone(c.timezone or settings.default_timezone)
    except Exception:
        return pytz.timezone(settings.default_timezone)


def _window():
    sh, sm = (int(x) for x in settings.quiet_hours_start.split(":"))
    eh, em = (int(x) for x in settings.quiet_hours_end.split(":"))
    return dtime(sh, sm), dtime(eh, em)


def callable_hours_between(c: Contact, start_utc: datetime, end_utc: datetime) -> float:
    """Hours between two naive-UTC instants that fall inside the lead's allowed calling window."""
    if not start_utc or end_utc <= start_utc:
        return 0.0
    tz = _tz(c)
    ws, we = _window()
    s = pytz.utc.localize(start_utc).astimezone(tz)
    e = pytz.utc.localize(end_utc).astimezone(tz)
    total = 0.0
    day = s.date()
    for _ in range(62):  # cap: two months is plenty
        if day > e.date():
            break
        lo = tz.localize(datetime.combine(day, ws))
        hi = tz.localize(datetime.combine(day, we))
        a, b = max(lo, s), min(hi, e)
        if b > a:
            total += (b - a).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def is_uncalled(c: Contact) -> bool:
    """Owner hasn't dialed it since they got it."""
    if c.last_called_at is None:
        return True
    return bool(c.assigned_at and c.last_called_at < c.assigned_at)


# ---------------------------------------------------------------- picking an owner
def active_agents(db: Session):
    return (db.query(Agent).filter(Agent.active == True)  # noqa: E712
            .order_by(Agent.created_at.asc(), Agent.id.asc()).all())


def least_loaded(db: Session, exclude: set | None = None, counts: dict | None = None):
    from routers.contacts_router import assigned_counts  # local import: avoid a cycle
    pool = [a for a in active_agents(db) if not exclude or a.id not in exclude]
    if not pool:
        return None
    counts = counts if counts is not None else assigned_counts(db)
    return min(pool, key=lambda a: counts.get(a.id, 0)).id


# ---------------------------------------------------------------- the sweep
def run_sweep(db: Session, now: datetime | None = None, dry_run: bool = False, by: str = "system") -> dict:
    """Re-home leads that are unowned, owned by a removed user, or sat uncalled past the window."""
    from routers.contacts_router import assigned_counts
    now = now or datetime.utcnow()
    cfg = all_settings(db)
    out = {"ran_at": now.isoformat(), "dry_run": dry_run, "enabled": cfg["stale_enabled"],
           "assigned_unowned": 0, "moved_from_inactive": 0, "moved_stale": 0,
           "flagged_for_manager": 0, "changes": []}
    if not cfg["stale_enabled"] and not dry_run:
        return out

    active_ids = {a.id for a in active_agents(db)}
    if not active_ids:
        return out
    counts = assigned_counts(db)

    q = (db.query(Contact)
         .filter((Contact.archived == False) | (Contact.archived.is_(None)))  # noqa: E712
         .filter(Contact.consent_call == True)  # noqa: E712
         .filter((Contact.dnc_flag == False) | (Contact.dnc_flag.is_(None))))  # noqa: E712
    for c in q.all():
        if not dialable(db, c):
            continue
        owner = c.assigned_agent_id
        reason, exclude = None, set()
        if owner is None:
            reason = "unowned"
        elif owner not in active_ids:
            reason, exclude = "owner_inactive", {owner}
        elif cfg["stale_enabled"] and is_uncalled(c) and c.assigned_at:
            if c.next_follow_up and c.next_follow_up > now:
                continue  # owner scheduled it; they're working it
            if callable_hours_between(c, c.assigned_at, now) >= cfg["stale_hours"]:
                if stale_rotations(c) >= cfg["max_rotations"]:
                    out["flagged_for_manager"] += 1
                    continue
                reason, exclude = "stale", {owner}
        if not reason:
            continue
        to = least_loaded(db, exclude=exclude, counts=counts)
        if not to or to == owner:
            continue
        out["changes"].append({"contact_id": c.id, "from": owner, "to": to, "reason": reason})
        key = {"unowned": "assigned_unowned", "owner_inactive": "moved_from_inactive", "stale": "moved_stale"}[reason]
        out[key] += 1
        if not dry_run:
            set_owner(c, to, by, reason, now)
            if owner:
                counts[owner] = max(0, counts.get(owner, 0) - 1)
            counts[to] = counts.get(to, 0) + 1
    if not dry_run:
        db.commit()
    out["changes"] = out["changes"][:200]
    return out


def needs_manager(db: Session, now: datetime | None = None) -> list:
    """Dialable, still uncalled, and already rotated max_rotations times."""
    now = now or datetime.utcnow()
    cfg = all_settings(db)
    rows = []
    for c in db.query(Contact).filter(Contact.assignment_log.isnot(None)).all():
        if dialable(db, c) and is_uncalled(c) and stale_rotations(c) >= cfg["max_rotations"]:
            rows.append(c)
    return rows


# ---------------------------------------------------------------- background loop
_started = False


def start_background_sweeper(session_factory):
    """Single replica: one daemon thread re-checks every 10 minutes."""
    global _started
    if _started:
        return
    _started = True
    stop = threading.Event()

    def loop():
        while not stop.wait(SWEEP_EVERY_SECONDS):
            db = session_factory()
            try:
                r = run_sweep(db)
                n = r["assigned_unowned"] + r["moved_from_inactive"] + r["moved_stale"]
                if n:
                    print("[lead-sweep]", {k: r[k] for k in r if k != "changes"}, flush=True)
            except Exception as exc:  # never take the API down over a sweep
                print("[lead-sweep] failed:", repr(exc), flush=True)
                db.rollback()
            finally:
                db.close()

    threading.Thread(target=loop, name="lead-sweep", daemon=True).start()

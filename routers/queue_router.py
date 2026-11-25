"""
Call queue + floor manager endpoints (2026-11-25).

  GET  /queue/me             my call queue: follow-ups due, then uncalled leads (oldest first)
  GET  /queue/summary        counts for me (admin: ?agent_id=) -- what Q nudges on
  GET  /queue/team           admin: every caller's numbers + leads that need a manager
  GET  /queue/settings       stale window etc.
  PUT  /queue/settings       admin: change them
  POST /queue/sweep          admin: run the re-home pass now (?dry_run=true to preview)
  GET  /queue/history/{id}   who owned this lead and why

No phone numbers or emails are returned by the summary/team endpoints (names + counts only).
"""
import hmac
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth import get_current_agent, require_admin
from core import lead_flow as lf
from core.compliance import is_within_quiet_hours
from core.config import get_settings
from sqlalchemy import func
from database import get_db
from models import Agent, AgentRole, CallSession, Contact

router = APIRouter(prefix="/queue", tags=["queue"])


def _name(c: Contact) -> str:
    return " ".join(x for x in (c.first_name, c.last_name) if x) or "Unnamed lead"


def _is_admin(a: Agent) -> bool:
    return a.role == AgentRole.admin or a.role == "admin"


def _my_leads(db: Session, agent_id: str):
    return (db.query(Contact)
            .filter(Contact.assigned_agent_id == agent_id)
            .filter((Contact.archived == False) | (Contact.archived.is_(None)))  # noqa: E712
            .all())


def _build(db: Session, agent_id: str, now: datetime) -> dict:
    cfg = lf.all_settings(db)
    due, new, blocked = [], [], {"no_consent": 0, "dnc_or_suppressed": 0, "no_phone": 0}
    for c in _my_leads(db, agent_id):
        if not c.phone:
            blocked["no_phone"] += 1
            continue
        if not c.consent_call:
            blocked["no_consent"] += 1
            continue
        if not lf.dialable(db, c):
            blocked["dnc_or_suppressed"] += 1
            continue
        item = {
            "contact_id": c.id,
            "name": _name(c),
            "source": c.source,
            "received_at": c.created_at.isoformat() if c.created_at else None,
            "assigned_at": c.assigned_at.isoformat() if c.assigned_at else None,
            "callable_now": is_within_quiet_hours(c),
            "timezone": c.timezone,
        }
        if c.next_follow_up and c.next_follow_up <= now:
            item["kind"] = "follow_up_due"
            item["follow_up_at"] = c.next_follow_up.isoformat()
            item["follow_up_note"] = c.follow_up_note
            due.append(item)
        elif lf.is_uncalled(c) and not (c.next_follow_up and c.next_follow_up > now):
            used = lf.callable_hours_between(c, c.assigned_at, now) if c.assigned_at else 0.0
            item["kind"] = "uncalled"
            item["hours_left_before_reassign"] = round(max(0.0, cfg["stale_hours"] - used), 2) if cfg["stale_enabled"] else None
            item["rotations"] = lf.stale_rotations(c)
            new.append(item)
    due.sort(key=lambda x: x["follow_up_at"])
    # Most at-risk first (about to rotate), then oldest assignment.
    new.sort(key=lambda x: (x["hours_left_before_reassign"] if x["hours_left_before_reassign"] is not None else 9e9,
                            x["assigned_at"] or ""))
    return {"follow_ups_due": due, "uncalled": new, "blocked": blocked, "settings": cfg}


def _calls_today(db: Session, agent_id: str, now: datetime) -> int:
    return (db.query(CallSession).filter(CallSession.agent_id == agent_id,
                                         CallSession.started_at >= now - timedelta(hours=24)).count())


def _summary(db: Session, a: Agent, now: datetime) -> dict:
    q = _build(db, a.id, now)
    risk = [x for x in q["uncalled"] if x["hours_left_before_reassign"] is not None and x["hours_left_before_reassign"] <= 1]
    callable_now = [x for x in q["uncalled"] + q["follow_ups_due"] if x["callable_now"]]
    nxt = (q["follow_ups_due"] + q["uncalled"])
    nxt = [x for x in nxt if x["callable_now"]][:1] or nxt[:1]
    return {
        "agent_id": a.id,
        "name": a.name or a.username,
        "uncalled": len(q["uncalled"]),
        "follow_ups_due": len(q["follow_ups_due"]),
        "about_to_reassign": len(risk),
        "callable_now": len(callable_now),
        "calls_last_24h": _calls_today(db, a.id, now),
        "next_up": ({"contact_id": nxt[0]["contact_id"], "name": nxt[0]["name"], "kind": nxt[0]["kind"]} if nxt else None),
        "blocked": q["blocked"],
        "stale_hours": q["settings"]["stale_hours"],
    }


@router.get("/me")
def my_queue(db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    now = datetime.utcnow()
    q = _build(db, agent.id, now)
    q["summary"] = _summary(db, agent, now)
    return q


@router.get("/summary")
def summary(agent_id: str | None = None, db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    target = agent
    if agent_id and agent_id != agent.id:
        if not _is_admin(agent):
            raise HTTPException(status_code=403, detail="Admin only")
        target = db.query(Agent).filter(Agent.id == agent_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="Agent not found")
    return _summary(db, target, datetime.utcnow())


@router.get("/team")
def team(db: Session = Depends(get_db), admin: Agent = Depends(require_admin)):
    now = datetime.utcnow()
    people = [_summary(db, a, now) for a in lf.active_agents(db)]
    unowned = sum(1 for c in db.query(Contact).filter(Contact.assigned_agent_id.is_(None)).all() if lf.dialable(db, c))
    mgr = lf.needs_manager(db, now)
    return {
        "agents": people,
        "totals": {k: sum(p[k] for p in people) for k in ("uncalled", "follow_ups_due", "about_to_reassign", "calls_last_24h")},
        "unowned_dialable": unowned,
        "needs_manager": [{"contact_id": c.id, "name": _name(c), "owner_id": c.assigned_agent_id,
                           "rotations": lf.stale_rotations(c)} for c in mgr[:100]],
        "settings": lf.all_settings(db),
    }


class SettingsIn(BaseModel):
    stale_enabled: bool | None = None
    stale_hours: float | None = Field(default=None, ge=0.5, le=72)
    max_rotations: int | None = Field(default=None, ge=1, le=20)


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    return lf.all_settings(db)


@router.put("/settings")
def put_settings(payload: SettingsIn, db: Session = Depends(get_db), admin: Agent = Depends(require_admin)):
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        lf.set_setting(db, k, ("true" if v else "false") if isinstance(v, bool) else str(v), admin.id)
    db.commit()
    return lf.all_settings(db)


@router.post("/sweep")
def sweep(dry_run: bool = False, db: Session = Depends(get_db), admin: Agent = Depends(require_admin)):
    return lf.run_sweep(db, dry_run=dry_run, by=f"admin:{admin.id}")


@router.get("/history/{contact_id}")
def history(contact_id: str, db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Contact not found")
    if c.assigned_agent_id != agent.id and not _is_admin(agent):
        raise HTTPException(status_code=403, detail="Not your lead")
    return {"contact_id": c.id, "owner_id": c.assigned_agent_id,
            "assigned_at": c.assigned_at.isoformat() if c.assigned_at else None,
            "last_called_at": c.last_called_at.isoformat() if c.last_called_at else None,
            "log": lf._log(c)}


# ---------------------------------------------------------------- Q central (server-to-server)
def _require_q_key(x_q_key: str | None = Header(default=None)):
    expected = get_settings().q_floor_key
    if not expected or len(expected) < 24 or expected.startswith("change-me"):
        raise HTTPException(status_code=503, detail="Q link is not configured")
    if not x_q_key or not hmac.compare_digest(x_q_key, expected):
        raise HTTPException(status_code=401, detail="Invalid Q key")


@router.get("/q/summary", dependencies=[Depends(_require_q_key)])
def q_summary(email: str, db: Session = Depends(get_db)):
    """What Q needs to act as floor manager for ONE person, looked up by their email.
    Names + counts only (no phone numbers or emails of leads). Admins also get the team view."""
    em = (email or "").strip().lower()
    a = (db.query(Agent).filter(func.lower(Agent.email) == em, Agent.active == True)  # noqa: E712
         .order_by(Agent.created_at.asc()).first()) if em else None
    if not a:
        raise HTTPException(status_code=404, detail="No active Pulse Marketing user with that email")
    now = datetime.utcnow()
    q = _build(db, a.id, now)
    out = {"me": _summary(db, a, now),
           "next": [{"name": x["name"], "kind": x["kind"], "callable_now": x["callable_now"],
                     "hours_left_before_reassign": x.get("hours_left_before_reassign")}
                    for x in (q["follow_ups_due"] + q["uncalled"])[:5]],
           "is_admin": _is_admin(a)}
    if out["is_admin"]:
        people = [_summary(db, p, now) for p in lf.active_agents(db)]
        out["team"] = [{k: p[k] for k in ("name", "uncalled", "follow_ups_due", "about_to_reassign", "calls_last_24h")}
                       for p in people]
        out["needs_manager"] = len(lf.needs_manager(db, now))
    return out

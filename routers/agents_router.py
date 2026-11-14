import html
import secrets
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_agent, require_admin, hash_password
from database import get_db
from models import Agent, AgentRole, CallSession

try:
    from core.sendgrid_client import send_email
except Exception:  # pragma: no cover
    send_email = None

LOGIN_URL = "https://mypulsemarketing.com"

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    username: str
    email: str | None = None
    password: str | None = None          # optional: server generates one when blank
    role: AgentRole = AgentRole.agent
    email_login_info: bool = False


class AgentUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    password: str | None = None
    role: AgentRole | None = None
    active: bool | None = None
    email_login_info: bool = False


class AgentOut(BaseModel):
    id: str
    name: str
    username: str
    email: str | None
    role: AgentRole
    active: bool
    is_owner: bool = False

    class Config:
        from_attributes = True


def _owner_id(db: Session):
    """Owner = earliest-created ACTIVE admin that has an email on file (falls back
    to the earliest active admin). Protected from deactivation/demotion."""
    q = (db.query(Agent).filter(Agent.role == AgentRole.admin, Agent.active == True)  # noqa: E712
         .order_by(Agent.created_at.asc().nullslast(), Agent.id.asc()))
    o = q.filter(Agent.email.isnot(None), Agent.email != "").first() or q.first()
    return o.id if o else None


def _out(a: Agent, owner_id, **extra) -> dict:
    d = {"id": a.id, "name": a.name, "username": a.username, "email": a.email,
         "role": a.role, "active": bool(a.active), "is_owner": a.id == owner_id}
    d.update(extra)
    return d


def _temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Pulse-" + "".join(secrets.choice(alphabet) for _ in range(10))


def _send_login_email(a: Agent, password: str):
    """Returns (sent: bool, error: str|None)."""
    if not a.email:
        return False, "no_email_on_file"
    if send_email is None:
        return False, "email_not_configured"
    body = (
        f"<p>Hi {html.escape(a.name or a.username)},</p>"
        f"<p>Your Pulse Marketing sign-in is ready.</p>"
        f"<p><b>Sign in:</b> <a href=\"{LOGIN_URL}\">{LOGIN_URL}</a><br>"
        f"<b>Username:</b> {html.escape(a.username)}<br>"
        f"<b>Temporary password:</b> {html.escape(password)}</p>"
        f"<p>Please keep this private. Ask your admin to reset it any time.</p>"
        f"<p>&mdash; Revive Capital</p>"
    )
    status, err = send_email(a.email, "Your Pulse Marketing sign-in", body)
    return (err is None), err


@router.get("")
def list_agents(db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    owner = _owner_id(db)
    return [_out(a, owner) for a in db.query(Agent).order_by(Agent.name.asc()).all()]


@router.get("/me")
def me(db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    return _out(agent, _owner_id(db))


@router.post("")
def create_agent(payload: AgentCreate, db: Session = Depends(get_db), admin=Depends(require_admin)):
    username = (payload.username or "").strip()
    if not username or " " in username:
        raise HTTPException(status_code=400, detail="Username is required and cannot contain spaces")
    if db.query(Agent).filter(func.lower(Agent.username) == username.lower()).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    if payload.password and len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not payload.password and not payload.email_login_info:
        raise HTTPException(status_code=400, detail="Enter a password, or choose to email login info")
    generated = not payload.password
    password = payload.password or _temp_password()
    new_agent = Agent(
        name=payload.name.strip(),
        username=username,
        email=(payload.email or "").strip() or None,
        password_hash=hash_password(password),
        role=payload.role,
    )
    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)
    sent, err = (False, None)
    if payload.email_login_info:
        sent, err = _send_login_email(new_agent, password)
    extra = {"email_sent": sent, "email_error": err}
    if generated and not sent:
        extra["temp_password"] = password  # admin relays it; only when it could not be emailed
    return _out(new_agent, _owner_id(db), **extra)


@router.patch("/{agent_id}")
def update_agent(agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner = _owner_id(db)
    if payload.name is not None and payload.name.strip():
        target.name = payload.name.strip()
    if payload.email is not None:
        target.email = payload.email.strip() or None
    if payload.role is not None:
        if target.id == owner and payload.role != AgentRole.admin:
            raise HTTPException(status_code=400, detail="The owner must stay an admin")
        target.role = payload.role
    if payload.active is not None:
        if target.id == owner and not payload.active:
            raise HTTPException(status_code=400, detail="The owner cannot be deactivated")
        target.active = payload.active
    new_pw = None
    if payload.password:
        if len(payload.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        new_pw = payload.password
        target.password_hash = hash_password(new_pw)
    db.commit()
    db.refresh(target)
    sent, err = (False, None)
    if payload.email_login_info:
        if new_pw is None:
            new_pw = _temp_password()
            sent, err = _send_login_email(target, new_pw)
            if sent:  # only change the password once the email is accepted
                target.password_hash = hash_password(new_pw)
                db.commit()
        else:
            sent, err = _send_login_email(target, new_pw)
    return _out(target, owner, email_sent=sent, email_error=err)


@router.post("/{agent_id}/send-login")
def send_login(agent_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not target.email:
        raise HTTPException(status_code=400, detail="This user has no email on file")
    pw = _temp_password()
    sent, err = _send_login_email(target, pw)
    if sent:  # email first; never lock someone out on a failed send
        target.password_hash = hash_password(pw)
        db.commit()
    return {"email_sent": sent, "email_error": err}


@router.post("/{agent_id}/reactivate")
def reactivate_agent(agent_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    target.active = True
    db.commit()
    db.refresh(target)
    return _out(target, _owner_id(db))


@router.get("/scoreboard")
def scoreboard(days: int = 1, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Per-agent activity over a rolling window: dials + outcome breakdown.
    Reads call_sessions (one row per dial, stamped with agent_id/disposition/started_at).
    days is clamped to 1..90; the window is rolling from now (not calendar day) to
    stay timezone-safe."""
    days = max(1, min(int(days or 1), 90))
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.query(CallSession.agent_id, CallSession.disposition, func.count(CallSession.id))
        .filter(CallSession.started_at >= since)
        .group_by(CallSession.agent_id, CallSession.disposition)
        .all()
    )
    agg: dict = {}
    for aid, disp, n in rows:
        d = agg.setdefault(aid, {})
        d["calls"] = d.get("calls", 0) + n
        d[disp or "none"] = d.get(disp or "none", 0) + n

    result = []
    for a in db.query(Agent).order_by(Agent.name.asc()).all():
        d = agg.get(a.id, {})
        result.append({
            "agent_id": a.id,
            "name": a.name or a.username,
            "active": bool(a.active),
            "calls": d.get("calls", 0),
            "contacted": d.get("contacted", 0),
            "follow_up": d.get("follow_up", 0),
            "new_appointment": d.get("new_appointment", 0),
            "missed_appointment": d.get("missed_appointment", 0),
            "bad_lead": d.get("bad_lead", 0),
            "dnc": d.get("dnc_requested", 0),
        })
    return {"days": days, "since": since.isoformat(), "agents": result}


@router.post("/{agent_id}/deactivate")
def deactivate_agent(agent_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    owner = _owner_id(db)
    if target.id == owner:
        raise HTTPException(status_code=400, detail="The owner cannot be deactivated")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")
    target.active = False
    db.commit()
    db.refresh(target)
    return _out(target, owner)

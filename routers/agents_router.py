from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_agent, require_admin, hash_password
from database import get_db
from models import Agent, AgentRole, CallSession

router = APIRouter(prefix="/agents", tags=["agents"])


class AgentCreate(BaseModel):
    name: str
    username: str
    email: str | None = None
    password: str
    role: AgentRole = AgentRole.agent


class AgentOut(BaseModel):
    id: str
    name: str
    username: str
    email: str | None
    role: AgentRole
    active: bool

    class Config:
        from_attributes = True


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    return db.query(Agent).all()


@router.post("", response_model=AgentOut)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db), admin=Depends(require_admin)):
    if db.query(Agent).filter(Agent.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    new_agent = Agent(
        name=payload.name,
        username=payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(new_agent)
    db.commit()
    db.refresh(new_agent)
    return new_agent


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


@router.post("/{agent_id}/deactivate", response_model=AgentOut)
def deactivate_agent(agent_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    target.active = False
    db.commit()
    db.refresh(target)
    return target

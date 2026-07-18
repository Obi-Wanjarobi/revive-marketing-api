from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_agent, require_admin, hash_password
from database import get_db
from models import Agent, AgentRole

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


@router.post("/{agent_id}/deactivate", response_model=AgentOut)
def deactivate_agent(agent_id: str, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.query(Agent).filter(Agent.id == agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found")
    target.active = False
    db.commit()
    db.refresh(target)
    return target

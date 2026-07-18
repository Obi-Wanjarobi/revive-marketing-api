from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from auth import verify_password, create_access_token
from database import get_db
from models import Agent

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.username == form_data.username).first()
    if not agent or not verify_password(form_data.password, agent.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not agent.active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(agent.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "agent": {"id": agent.id, "name": agent.name, "role": agent.role, "username": agent.username},
    }

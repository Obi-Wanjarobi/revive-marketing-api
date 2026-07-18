from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import get_current_agent, require_admin
from database import get_db
from models import SuppressedNumber

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("/suppressed")
def list_suppressed(db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    return db.query(SuppressedNumber).all()


@router.post("/suppressed")
def add_suppressed(phone: str | None = None, email: str | None = None, reason: str = "manual",
                    db: Session = Depends(get_db), admin=Depends(require_admin)):
    entry = SuppressedNumber(phone=phone, email=email, reason=reason)
    db.add(entry)
    db.commit()
    return {"status": "added", "id": entry.id}

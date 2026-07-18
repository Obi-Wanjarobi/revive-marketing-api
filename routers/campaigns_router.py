from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_agent
from database import get_db
from models import Campaign, CampaignContact, Contact, CampaignType, CampaignStatus

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class CampaignCreate(BaseModel):
    name: str
    type: CampaignType = CampaignType.multi
    email_subject: str | None = None
    email_body_html: str | None = None
    sms_body: str | None = None


class CampaignOut(BaseModel):
    id: str
    name: str
    type: CampaignType
    status: CampaignStatus
    email_subject: str | None
    sms_body: str | None

    class Config:
        from_attributes = True


@router.get("", response_model=list[CampaignOut])
def list_campaigns(db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    return db.query(Campaign).all()


@router.post("", response_model=CampaignOut)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    campaign = Campaign(created_by=agent.id, **payload.model_dump())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/add-contacts")
def add_contacts(campaign_id: str, contact_ids: list[str], db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    existing = {
        cc.contact_id
        for cc in db.query(CampaignContact).filter(CampaignContact.campaign_id == campaign_id).all()
    }
    added = 0
    for cid in contact_ids:
        if cid in existing:
            continue
        if not db.query(Contact).filter(Contact.id == cid).first():
            continue
        db.add(CampaignContact(campaign_id=campaign_id, contact_id=cid))
        added += 1
    db.commit()
    return {"added": added}


@router.post("/{campaign_id}/status")
def set_status(campaign_id: str, status: CampaignStatus, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.status = status
    db.commit()
    return {"id": campaign.id, "status": campaign.status}


@router.get("/{campaign_id}/stats")
def campaign_stats(campaign_id: str, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    rows = db.query(CampaignContact).filter(CampaignContact.campaign_id == campaign_id).all()

    def tally(field):
        counts = {}
        for r in rows:
            v = getattr(r, field)
            counts[v] = counts.get(v, 0) + 1
        return counts

    return {
        "total_contacts": len(rows),
        "call": tally("call_status"),
        "email": tally("email_status"),
        "sms": tally("sms_status"),
    }

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_agent
from core.compliance import can_contact
from core.sendgrid_client import send_email
from core.twilio_client import send_sms
from database import get_db
from models import Campaign, CampaignContact, Contact, BlastMessage

router = APIRouter(prefix="/blast", tags=["blast"])


@router.post("/{campaign_id}/send")
def send_blast(
    campaign_id: str,
    channels: list[str],  # subset of ["email", "sms"]
    db: Session = Depends(get_db),
    agent=Depends(get_current_agent),
):
    """
    Sends the campaign's email and/or SMS to every attached contact who
    passes the compliance gate. Every single recipient is checked
    individually and independently - a passing check for one contact never
    implies anything about another.
    """
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    invalid_channels = set(channels) - {"email", "sms"}
    if invalid_channels:
        raise HTTPException(status_code=400, detail=f"Unknown channel(s): {invalid_channels}")

    links = db.query(CampaignContact).filter(CampaignContact.campaign_id == campaign_id).all()

    summary = {"email": {"sent": 0, "skipped": 0, "failed": 0}, "sms": {"sent": 0, "skipped": 0, "failed": 0}}

    for link in links:
        contact = db.query(Contact).filter(Contact.id == link.contact_id).first()
        if not contact:
            continue

        if "email" in channels and campaign.email_subject:
            summary["email"][_send_one(db, campaign, link, contact, "email")] += 1

        if "sms" in channels and campaign.sms_body:
            summary["sms"][_send_one(db, campaign, link, contact, "sms")] += 1

    db.commit()
    return {"campaign_id": campaign_id, "results": summary}


def _send_one(db: Session, campaign: Campaign, link: CampaignContact, contact: Contact, channel: str) -> str:
    """Returns 'sent', 'skipped', or 'failed' — also writes a BlastMessage row for the audit trail."""
    result = can_contact(db, contact, channel)  # type: ignore[arg-type]
    if not result.allowed:
        status_field = "email_status" if channel == "email" else "sms_status"
        setattr(link, status_field, f"skipped_{result.reason}")
        db.add(BlastMessage(
            campaign_id=campaign.id, contact_id=contact.id, channel=channel,
            status=f"skipped_{result.reason}",
        ))
        return "skipped"

    if channel == "email":
        provider_id, error = send_email(contact.email, campaign.email_subject, campaign.email_body_html or "")
    else:
        provider_id, error = send_sms(contact.phone, campaign.sms_body)

    status_field = "email_status" if channel == "email" else "sms_status"
    if error:
        setattr(link, status_field, "failed")
        db.add(BlastMessage(campaign_id=campaign.id, contact_id=contact.id, channel=channel, status="failed", error=error))
        return "failed"
    else:
        setattr(link, status_field, "sent")
        db.add(BlastMessage(
            campaign_id=campaign.id, contact_id=contact.id, channel=channel, status="sent",
            provider_sid=provider_id, sent_at=datetime.utcnow(),
        ))
        return "sent"

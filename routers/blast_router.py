import hashlib
import hmac
import html as _html
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from auth import get_current_agent
from core.compliance import can_contact
from core.config import get_settings
from core.sendgrid_client import send_email
from core.twilio_client import send_sms
from database import get_db
from models import Campaign, CampaignContact, Contact, BlastMessage, SuppressedNumber

router = APIRouter(prefix="/blast", tags=["blast"])

# ---- CAN-SPAM / TCPA footers (added 2026-11-14) ----------------------------
# Every campaign email gets the sender's postal address + a working one-click
# unsubscribe link; every campaign text gets "Reply STOP to opt out".
API_PUBLIC_URL = os.getenv("API_PUBLIC_URL", "https://revive-marketing-api-production.up.railway.app")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Revive Capital, LLC")
COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "400 Spectrum Center Drive, Ste 1900, Irvine, CA 92618")
COMPANY_NMLS = os.getenv("COMPANY_NMLS", "").strip()  # shown only when set — never invented


def _unsub_token(contact_id: str) -> str:
    key = (get_settings().jwt_secret or "").encode()
    return hmac.new(key, f"unsub:{contact_id}".encode(), hashlib.sha256).hexdigest()[:32]


def _unsub_url(contact_id: str) -> str:
    return f"{API_PUBLIC_URL}/blast/unsubscribe?c={contact_id}&t={_unsub_token(contact_id)}"


def _email_footer(contact_id: str) -> str:
    nmls = f" &middot; NMLS #{_html.escape(COMPANY_NMLS)}" if COMPANY_NMLS else ""
    return (
        '<hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px">'
        '<p style="font-size:12px;color:#666;line-height:1.5">'
        f"{_html.escape(COMPANY_NAME)}{nmls}<br>{_html.escape(COMPANY_ADDRESS)}<br>"
        "You are receiving this because you contacted us or agreed to hear from us. "
        f'<a href="{_unsub_url(contact_id)}">Unsubscribe</a> from these emails.</p>'
    )


def _sms_with_stop(body: str) -> str:
    b = (body or "").strip()
    return b if "stop" in b.lower() else f"{b}\nReply STOP to opt out."


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
        provider_id, error = send_email(contact.email, campaign.email_subject,
                                        (campaign.email_body_html or "") + _email_footer(contact.id))
    else:
        provider_id, error = send_sms(contact.phone, _sms_with_stop(campaign.sms_body))

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


_UNSUB_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>{title}</title><body style='font-family:system-ui,sans-serif;max-width:520px;margin:60px auto;padding:0 20px;color:#1B2A4A'>"
    "<h2>{title}</h2><p>{msg}</p><p style='color:#888;font-size:13px'>Revive Capital</p></body>"
)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(c: str = "", t: str = "", db: Session = Depends(get_db)):
    """Public one-click email unsubscribe (CAN-SPAM). Signed link — no login needed,
    and nobody can unsubscribe a different contact by guessing ids."""
    if not c or not t or not hmac.compare_digest(t, _unsub_token(c)):
        return HTMLResponse(_UNSUB_PAGE.format(title="Link not valid",
                            msg="This unsubscribe link is incomplete. Reply to any of our emails with UNSUBSCRIBE and we will remove you."), status_code=400)
    contact = db.query(Contact).filter(Contact.id == c).first()
    if contact:
        contact.unsubscribed_email = True
        contact.consent_email = False
        if contact.email and not db.query(SuppressedNumber).filter(SuppressedNumber.email == contact.email).first():
            db.add(SuppressedNumber(email=contact.email, reason="email_unsubscribe"))
        db.commit()
    return HTMLResponse(_UNSUB_PAGE.format(title="You're unsubscribed",
                        msg="You won't receive marketing emails from Revive Capital anymore. It can take up to 24 hours to stop completely."))

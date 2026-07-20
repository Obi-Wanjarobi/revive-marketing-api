"""
Public website lead-intake endpoint.

The Revive Capital marketing site (Get Pre-Approved / contact form) posts leads
here THROUGH a server-side Netlify function that holds the ingest key -- the key
is never exposed in the browser.

Design notes:
- Auth: a shared ingest key in the `X-Ingest-Key` header (settings.leads_ingest_key,
  falling back to settings.internal_api_key). This is the same service-to-service
  key pattern already used elsewhere; a public form endpoint should use a scoped
  key, NOT an agent login.
- Consent: TCPA-first. Consent flags are only honored when the person is NOT
  already suppressed / opted out. A web form submission is treated as documented
  opt-in, recorded with a consent_source + timestamp -- never assumed.
- Dedupe: matches an existing contact by normalized phone or email and UPDATES it
  (upsert), so repeat submissions don't create duplicate records.
- Additive only: reuses the existing Contact model (no new columns, no migration).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import get_settings
from core.compliance import is_suppressed
from database import get_db
from models import Contact
from routers.contacts_router import normalize_phone, ContactOut

settings = get_settings()
router = APIRouter(prefix="/leads", tags=["leads"])


class LeadIn(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: str | None = None
    timezone: str | None = None
    source: str | None = None            # e.g. "website:get-pre-approved"
    tags: str | None = None
    # Consent as captured on the web form (checkbox + disclosure):
    consent_call: bool = False
    consent_sms: bool = False
    consent_email: bool = False
    consent_source: str | None = None    # e.g. "website form: Get Pre-Approved checkbox"


class LeadResult(BaseModel):
    id: str
    created: bool        # True = new contact created; False = existing contact updated
    suppressed: bool     # True = phone/email already on the DNC/suppression list (consent NOT auto-applied)
    contact: ContactOut


def require_ingest_key(x_ingest_key: str | None = Header(default=None)):
    expected = settings.leads_ingest_key or settings.internal_api_key
    if not expected:
        # Fail closed: never accept unauthenticated writes if no key is configured.
        raise HTTPException(status_code=503, detail="Lead intake is not configured")
    if not x_ingest_key or x_ingest_key != expected:
        raise HTTPException(status_code=401, detail="Invalid ingest key")


@router.post("", response_model=LeadResult, dependencies=[Depends(require_ingest_key)])
def create_lead(payload: LeadIn, db: Session = Depends(get_db)):
    phone = normalize_phone(payload.phone)
    email = (payload.email or "").strip().lower() or None
    if not phone and not email:
        raise HTTPException(status_code=422, detail="A valid phone number or email is required")

    # Dedupe: match an existing contact by phone first, then email.
    existing = None
    if phone:
        existing = db.query(Contact).filter(Contact.phone == phone).first()
    if existing is None and email:
        existing = db.query(Contact).filter(Contact.email == email).first()

    suppressed = is_suppressed(db, phone, email)

    contact = existing if existing is not None else Contact()

    # Update identity fields without wiping existing good data with blanks.
    if payload.first_name and payload.first_name.strip():
        contact.first_name = payload.first_name.strip()
    if payload.last_name and payload.last_name.strip():
        contact.last_name = payload.last_name.strip()
    if phone:
        contact.phone = phone
    if email:
        contact.email = email
    if payload.timezone and payload.timezone.strip():
        contact.timezone = payload.timezone.strip()
    if not contact.source:
        contact.source = payload.source or "website"
    if payload.tags:
        contact.tags = payload.tags

    # Consent: honor the form's opt-in ONLY if this person is not already
    # suppressed or flagged do-not-contact. Otherwise keep them off outreach.
    applied_consent = False
    if not suppressed and not contact.dnc_flag:
        if payload.consent_call:
            contact.consent_call = True
            applied_consent = True
        if payload.consent_sms and not contact.unsubscribed_sms:
            contact.consent_sms = True
            applied_consent = True
        if payload.consent_email and not contact.unsubscribed_email:
            contact.consent_email = True
            applied_consent = True
        if applied_consent:
            contact.consent_source = payload.consent_source or payload.source or "website form"
            contact.consent_timestamp = datetime.utcnow()

    if existing is None:
        db.add(contact)
    db.commit()
    db.refresh(contact)

    return LeadResult(
        id=contact.id,
        created=(existing is None),
        suppressed=suppressed,
        contact=contact,
    )

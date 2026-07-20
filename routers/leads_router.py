"""
Public website lead-intake endpoint.

Security model (hardened after the System Integrity review, 2026-07-20):
- AUTH: dedicated LEADS_INGEST_KEY via the X-Ingest-Key header, compared in
  constant time. No fallback to the internal service key; blank / placeholder /
  short keys are refused (fail closed).
- CONSENT (TCPA): a public form submission is a CLAIM, not proof of consent.
  This endpoint NEVER sets consent_call/sms/email to True -- that prevents a
  third party from fabricating consent for a stranger's phone/email. The claim
  is recorded in consent_source as "pending verification"; consent is only
  turned on later through a verified path (double opt-in, or an authenticated
  agent after human verification).
- PRIVACY: the response returns only {id, created}. It never echoes stored PII
  or suppression/DNC status, so it can't be used as a lookup / enumeration
  oracle.
- ANTI-HIJACK: on a dedupe match, only MISSING fields are filled in. An existing
  contact's phone / email / name is never overwritten from this unauthenticated
  public caller.
- ABUSE: simple per-IP rate limiting and bounded input lengths.
Additive only: reuses the existing Contact model (no schema migration).
"""
import hmac
import time
from collections import defaultdict, deque
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.config import get_settings
from core.compliance import is_suppressed
from database import get_db
from models import Contact
from routers.contacts_router import normalize_phone

settings = get_settings()
router = APIRouter(prefix="/leads", tags=["leads"])

# Keys that must never be accepted as a valid ingest key.
_PLACEHOLDER_KEYS = {
    "",
    "change-me",
    "change-me-to-a-long-random-string",
}
_MIN_KEY_LEN = 16

# Simple in-memory per-IP rate limiter (single replica): max N requests / window.
_RATE_MAX = 10
_RATE_WINDOW_SECONDS = 60
_recent_hits: "defaultdict[str, deque]" = defaultdict(deque)


def rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = _recent_hits[ip]
    while hits and now - hits[0] > _RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _RATE_MAX:
        raise HTTPException(status_code=429, detail="Too many requests, please try again shortly")
    hits.append(now)


def require_ingest_key(x_ingest_key: str | None = Header(default=None)):
    expected = settings.leads_ingest_key
    # Fail closed: refuse if no real dedicated key is configured.
    if not expected or expected in _PLACEHOLDER_KEYS or len(expected) < _MIN_KEY_LEN:
        raise HTTPException(status_code=503, detail="Lead intake is not configured")
    if not x_ingest_key or not hmac.compare_digest(x_ingest_key, expected):
        raise HTTPException(status_code=401, detail="Invalid ingest key")


class LeadIn(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    timezone: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=120)
    tags: str | None = Field(default=None, max_length=500)
    # Consent as CLAIMED on the web form checkbox. Recorded, NOT trusted /
    # NOT auto-applied (see module docstring).
    consent_call: bool = False
    consent_sms: bool = False
    consent_email: bool = False
    consent_source: str | None = Field(default=None, max_length=200)


class LeadResult(BaseModel):
    id: str
    created: bool  # True = new contact created; False = matched an existing contact


@router.post(
    "",
    response_model=LeadResult,
    dependencies=[Depends(rate_limit), Depends(require_ingest_key)],
)
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

    claimed_channels = [
        name for name, on in (
            ("call", payload.consent_call),
            ("sms", payload.consent_sms),
            ("email", payload.consent_email),
        ) if on
    ]

    if existing is not None:
        contact = existing
        # Anti-hijack: fill only MISSING fields; never overwrite an existing
        # identity/identifier from this unauthenticated public caller.
        if not contact.first_name and payload.first_name and payload.first_name.strip():
            contact.first_name = payload.first_name.strip()
        if not contact.last_name and payload.last_name and payload.last_name.strip():
            contact.last_name = payload.last_name.strip()
        if not contact.phone and phone:
            contact.phone = phone
        if not contact.email and email:
            contact.email = email
        if not contact.timezone and payload.timezone and payload.timezone.strip():
            contact.timezone = payload.timezone.strip()
        if not contact.tags and payload.tags:
            contact.tags = payload.tags
    else:
        contact = Contact(
            first_name=(payload.first_name or "").strip() or None,
            last_name=(payload.last_name or "").strip() or None,
            phone=phone,
            email=email,
            timezone=(payload.timezone or "").strip() or None,
            source=payload.source or "website",
            tags=payload.tags or None,
        )
        db.add(contact)

    # CONSENT: never auto-enabled here. Record the (unverified) claim so a later
    # verification step can act on it. consent_* flags are left untouched -- new
    # contacts stay at their model default (False); an already-verified existing
    # contact's consent is not disturbed.
    if claimed_channels and (not contact.consent_source or contact.consent_source.startswith("WEB FORM CLAIM")):
        stamp = datetime.utcnow().strftime("%Y-%m-%d")
        src = (payload.consent_source or payload.source or "Get Pre-Approved form")
        contact.consent_source = (
            "WEB FORM CLAIM (unverified, pending double opt-in) "
            f"{stamp}: {src} [{','.join(claimed_channels)}]"
        )[:250]

    # Defense-in-depth: reflect do-not-contact on the record if suppressed.
    if suppressed:
        contact.dnc_flag = True

    db.commit()
    db.refresh(contact)
    return LeadResult(id=contact.id, created=(existing is None))

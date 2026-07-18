import csv
import io
from datetime import datetime

import phonenumbers
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_agent
from database import get_db
from models import Contact, SuppressedNumber

router = APIRouter(prefix="/contacts", tags=["contacts"])


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, "US")
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        return None


class ContactOut(BaseModel):
    id: str
    first_name: str | None
    last_name: str | None
    phone: str | None
    email: str | None
    timezone: str | None
    consent_call: bool
    consent_sms: bool
    consent_email: bool
    dnc_flag: bool

    class Config:
        from_attributes = True


class ConsentUpdate(BaseModel):
    consent_call: bool | None = None
    consent_sms: bool | None = None
    consent_email: bool | None = None
    consent_source: str | None = None


@router.get("", response_model=list[ContactOut])
def list_contacts(
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    agent=Depends(get_current_agent),
):
    return db.query(Contact).offset(offset).limit(limit).all()


@router.post("/import-csv")
def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """
    Expects columns: first_name,last_name,phone,email,timezone (timezone optional).
    Imported contacts start with ALL consent flags False - consent must be
    set explicitly (e.g. via a prior-relationship flag or documented opt-in),
    never assumed from the fact that a number showed up in a spreadsheet.
    """
    content = file.file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    created, skipped_invalid_phone = 0, 0
    for row in reader:
        phone = normalize_phone(row.get("phone"))
        email = (row.get("email") or "").strip() or None
        if not phone and not email:
            skipped_invalid_phone += 1
            continue
        contact = Contact(
            first_name=(row.get("first_name") or "").strip() or None,
            last_name=(row.get("last_name") or "").strip() or None,
            phone=phone,
            email=email,
            timezone=(row.get("timezone") or "").strip() or None,
            source="csv_upload",
        )
        db.add(contact)
        created += 1
    db.commit()
    return {"created": created, "skipped_invalid": skipped_invalid_phone}


@router.patch("/{contact_id}/consent", response_model=ContactOut)
def update_consent(contact_id: str, payload: ConsentUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    data = payload.model_dump(exclude_unset=True)
    for field in ("consent_call", "consent_sms", "consent_email"):
        if field in data:
            setattr(contact, field, data[field])
    if "consent_source" in data:
        contact.consent_source = data["consent_source"]
    if any(k in data for k in ("consent_call", "consent_sms", "consent_email")):
        contact.consent_timestamp = datetime.utcnow()

    db.commit()
    db.refresh(contact)
    return contact


@router.post("/{contact_id}/suppress")
def suppress_contact(contact_id: str, reason: str = "manual", db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Manual opt-out / DNC add. Also adds to the suppressed_numbers table so
    it's honored even if this contact record is later deleted or re-imported."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    contact.dnc_flag = True
    if contact.phone:
        db.add(SuppressedNumber(phone=contact.phone, reason=reason))
    if contact.email:
        db.add(SuppressedNumber(email=contact.email, reason=reason))
    db.commit()
    return {"status": "suppressed", "contact_id": contact_id}

import csv
import io
import json
from datetime import datetime

import phonenumbers
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_agent
from database import get_db
from models import (
    Contact,
    Agent,
    SuppressedNumber,
    CampaignContact,
    CallSession,
    BlastMessage,
)

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


# ---------------------------------------------------------------------------
# Auto-assignment (round-robin / least-loaded).
# New leads should never sit unowned. We assign each new lead to the ACTIVE
# agent currently holding the fewest active (non-archived) leads, breaking ties
# by agent creation order. In steady state this rotates evenly (round-robin),
# and it self-heals if agents are added, deactivated, or leads are re-assigned.
# ---------------------------------------------------------------------------
def active_agents_ordered(db: Session):
    return (
        db.query(Agent)
        .filter(Agent.active == True)  # noqa: E712
        .order_by(Agent.created_at.asc(), Agent.id.asc())
        .all()
    )


def assigned_counts(db: Session) -> dict:
    rows = (
        db.query(Contact.assigned_agent_id, func.count(Contact.id))
        .filter(Contact.assigned_agent_id.isnot(None))
        .filter((Contact.archived == False) | (Contact.archived.is_(None)))  # noqa: E712
        .group_by(Contact.assigned_agent_id)
        .all()
    )
    return {aid: n for aid, n in rows}


def pick_rr_agent(db: Session):
    """Return the agent_id of the least-loaded active agent (round-robin), or None."""
    agents = active_agents_ordered(db)
    if not agents:
        return None
    counts = assigned_counts(db)
    return min(agents, key=lambda a: counts.get(a.id, 0)).id


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
    created_at: datetime | None = None  # when the lead/contact was received
    assigned_agent_id: str | None = None  # lead owner (re-assignment)
    notes: str | None = None  # shared lead notes
    app_data: str | None = None  # focused 1003 intake (JSON string)
    archived: bool | None = False  # cleaned out of the active list (reversible)
    next_follow_up: datetime | None = None  # scheduled callback time
    follow_up_note: str | None = None  # short reason/label for the callback
    notes_log: str | None = None  # JSON array of per-agent notes (who/what/when)

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
    # Round-robin across active agents for the whole batch, keeping a running
    # tally so the import is distributed evenly (not all onto one agent).
    rr_agents = active_agents_ordered(db)
    rr_counts = assigned_counts(db) if rr_agents else {}
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
        if rr_agents:
            aid = min(rr_agents, key=lambda a: rr_counts.get(a.id, 0)).id
            contact.assigned_agent_id = aid
            rr_counts[aid] = rr_counts.get(aid, 0) + 1
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


@router.delete("/{contact_id}")
def delete_contact(contact_id: str, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """
    Permanently delete a contact record.

    Compliance / safety:
    - If the contact was DNC or opted out, the number/email is first written to
      the suppressed_numbers list so a delete can NEVER resurrect a do-not-contact
      number via a later import.
    - Call history (call_sessions) is kept for the record and just detached
      (contact_id set NULL), so dialing/compliance history is not lost.
    - Rows that require this contact (campaign_contacts, blast_messages) are
      removed first to satisfy foreign keys, then the contact is deleted.
    """
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Never let a delete un-suppress a do-not-contact number.
    if contact.dnc_flag or contact.unsubscribed_sms or contact.unsubscribed_email:
        if contact.phone:
            db.add(SuppressedNumber(phone=contact.phone, reason="deleted_contact"))
        if contact.email:
            db.add(SuppressedNumber(email=contact.email, reason="deleted_contact"))

    # Keep call history, just detach it from the deleted contact.
    for cs in db.query(CallSession).filter(CallSession.contact_id == contact_id).all():
        cs.contact_id = None

    # Remove rows with a NOT NULL foreign key to this contact.
    db.query(CampaignContact).filter(CampaignContact.contact_id == contact_id).delete(synchronize_session=False)
    db.query(BlastMessage).filter(BlastMessage.contact_id == contact_id).delete(synchronize_session=False)

    db.delete(contact)
    db.commit()
    return {"status": "deleted", "contact_id": contact_id}


class AssignUpdate(BaseModel):
    agent_id: str | None = None  # None = unassign


@router.patch("/{contact_id}/assign", response_model=ContactOut)
def assign_contact(contact_id: str, payload: AssignUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Assign (or unassign, with agent_id=null) a lead to an agent for ownership."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    aid = payload.agent_id or None
    if aid is not None:
        owner = db.query(Agent).filter(Agent.id == aid).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Agent not found")
    contact.assigned_agent_id = aid
    db.commit()
    db.refresh(contact)
    return contact


class NoteUpdate(BaseModel):
    notes: str | None = None


@router.patch("/{contact_id}/note", response_model=ContactOut)
def update_note(contact_id: str, payload: NoteUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Save the shared note on a lead (relevant info / 1003 context)."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.notes = (payload.notes or None)
    db.commit()
    db.refresh(contact)
    return contact


class ApplicationUpdate(BaseModel):
    app_data: str | None = None  # focused 1003 intake, JSON string
    notes: str | None = None


@router.patch("/{contact_id}/application", response_model=ContactOut)
def update_application(contact_id: str, payload: ApplicationUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Save the focused 1003 intake (and optionally notes) on a lead. No SSN/income is expected here."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    data = payload.model_dump(exclude_unset=True)
    if "app_data" in data:
        contact.app_data = data["app_data"] or None
    if "notes" in data:
        contact.notes = data["notes"] or None
    db.commit()
    db.refresh(contact)
    return contact


class ArchiveUpdate(BaseModel):
    archived: bool = True


@router.patch("/{contact_id}/archive", response_model=ContactOut)
def archive_contact(contact_id: str, payload: ArchiveUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Archive (clean out) or restore a lead. Reversible — nothing is deleted."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.archived = bool(payload.archived)
    db.commit()
    db.refresh(contact)
    return contact


class FollowUpUpdate(BaseModel):
    next_follow_up: datetime | None = None  # callback datetime; null clears it
    follow_up_note: str | None = None       # short reason/label for the callback


@router.patch("/{contact_id}/followup", response_model=ContactOut)
def update_followup(contact_id: str, payload: FollowUpUpdate, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Schedule (or clear, with next_follow_up=null) a callback/follow-up on a lead.
    Powers the dashboard 'Due today' view. Reversible — just a timestamp on the record."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    data = payload.model_dump(exclude_unset=True)
    if "next_follow_up" in data:
        contact.next_follow_up = data["next_follow_up"]
    if "follow_up_note" in data:
        contact.follow_up_note = (data["follow_up_note"] or None)
    db.commit()
    db.refresh(contact)
    return contact


class NoteLogAdd(BaseModel):
    text: str


@router.post("/{contact_id}/notes-log", response_model=ContactOut)
def add_note_log(contact_id: str, payload: NoteLogAdd, db: Session = Depends(get_db), agent=Depends(get_current_agent)):
    """Append an agent-attributed note to the lead's notes thread (who/what/when).
    Append-only = an audit trail (IOTA). The shared 'notes' field is separate and
    stays as the pinned summary everyone edits."""
    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    text_val = (payload.text or "").strip()
    if not text_val:
        raise HTTPException(status_code=422, detail="Note text is required")
    try:
        log = json.loads(contact.notes_log) if contact.notes_log else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []
    log.append({
        "agent_id": getattr(agent, "id", None),
        "agent": (getattr(agent, "name", None) or getattr(agent, "username", None) or "Agent"),
        "at": datetime.utcnow().isoformat(),
        "text": text_val[:4000],
    })
    contact.notes_log = json.dumps(log)
    db.commit()
    db.refresh(contact)
    return contact

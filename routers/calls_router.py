from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from twilio.twiml.voice_response import VoiceResponse, Dial

from auth import get_current_agent
from core.compliance import can_contact
from core.twilio_client import generate_voice_access_token, get_twilio_client
from core.config import get_settings
from database import get_db
from models import Contact, CallSession, Agent

router = APIRouter(prefix="/calls", tags=["calls"])
settings = get_settings()


@router.get("/voice-token")
def voice_token(agent: Agent = Depends(get_current_agent)):
    """Frontend calls this to get a token for the Twilio Voice JS SDK softphone."""
    identity = agent.twilio_client_identity or f"agent_{agent.id}"
    token = generate_voice_access_token(identity)
    if token is None:
        raise HTTPException(
            status_code=503,
            detail="Twilio Voice not configured yet - set TWILIO_API_KEY_SID / SECRET / TWIML_APP_SID",
        )
    return {"token": token, "identity": identity}


class DialRequest(BaseModel):
    contact_id: str
    campaign_id: str | None = None
    lines: int = 1  # 1-3, how many of this contact's numbers to try (v1: primary number only)


@router.post("/dial")
def initiate_dial(payload: DialRequest, db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    """
    Agent clicks 'Dial' in the UI -> this endpoint runs the compliance check,
    then hands back what the browser softphone needs to place the call via
    the Voice SDK. The actual ringing happens client-side against Twilio;
    this endpoint is the compliance gate + logging, not the dial itself.
    """
    if payload.lines not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="lines must be 1, 2, or 3")

    contact = db.query(Contact).filter(Contact.id == payload.contact_id).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.phone:
        raise HTTPException(status_code=400, detail="Contact has no phone number on file")

    result = can_contact(db, contact, "call")
    if not result.allowed:
        raise HTTPException(status_code=403, detail=f"Blocked by compliance check: {result.reason}")

    session = CallSession(
        agent_id=agent.id,
        contact_id=contact.id,
        campaign_id=payload.campaign_id,
        lines_dialed=payload.lines,
        started_at=datetime.utcnow(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return {
        "call_session_id": session.id,
        "dial_to": contact.phone,
        "from_number": settings.twilio_from_number,
        "lines_dialed": payload.lines,
    }


class DispositionUpdate(BaseModel):
    disposition: str  # connected/voicemail/no_answer/busy/wrong_number/dnc_requested
    notes: str | None = None
    duration_seconds: int | None = None


@router.post("/{session_id}/disposition")
def log_disposition(session_id: str, payload: DispositionUpdate, db: Session = Depends(get_db), agent: Agent = Depends(get_current_agent)):
    session = db.query(CallSession).filter(CallSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Call session not found")

    session.disposition = payload.disposition
    session.notes = payload.notes
    session.duration_seconds = payload.duration_seconds
    session.ended_at = datetime.utcnow()
    db.commit()

    # A "dnc_requested" disposition immediately suppresses the contact -
    # this is the agent saying "they asked to be removed," treat it as final.
    if payload.disposition == "dnc_requested" and session.contact_id:
        contact = db.query(Contact).filter(Contact.id == session.contact_id).first()
        if contact:
            contact.dnc_flag = True
            db.commit()

    return {"status": "logged", "session_id": session_id}


@router.post("/twiml/outbound")
async def twiml_outbound(To: str = "", From: str = ""):
    """
    TwiML endpoint the Voice SDK call connects to. Twilio posts here when the
    browser softphone places a call; this tells Twilio what number to actually
    ring. Configure this URL as the TwiML App's Voice Request URL in Twilio console.
    """
    response = VoiceResponse()
    dial = Dial(caller_id=settings.twilio_from_number)
    dial.number(To or settings.twilio_from_number)
    response.append(dial)
    return Response(content=str(response), media_type="application/xml")

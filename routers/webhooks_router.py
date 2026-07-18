from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse

from database import get_db
from models import Contact, SuppressedNumber, CallSession

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "yes"}


@router.post("/twilio/sms-inbound")
def sms_inbound(From: str = Form(...), Body: str = Form(""), db: Session = Depends(get_db)):
    """
    Twilio posts here for every inbound SMS. Any STOP-family keyword must
    suppress the sender immediately and permanently, no exceptions -
    this is a carrier/TCPA requirement, not a nice-to-have.
    """
    body_normalized = Body.strip().lower()

    contact = db.query(Contact).filter(Contact.phone == From).first()

    if body_normalized in STOP_WORDS:
        if contact:
            contact.unsubscribed_sms = True
            contact.dnc_flag = True
        if not db.query(SuppressedNumber).filter(SuppressedNumber.phone == From).first():
            db.add(SuppressedNumber(phone=From, reason="stop_keyword"))
        db.commit()
        reply = MessagingResponse()
        reply.message("You've been unsubscribed and won't receive further texts from us.")
        return Response(content=str(reply), media_type="application/xml")

    if body_normalized in START_WORDS and contact:
        contact.unsubscribed_sms = False
        db.commit()
        reply = MessagingResponse()
        reply.message("You're re-subscribed. Reply STOP anytime to opt out again.")
        return Response(content=str(reply), media_type="application/xml")

    # Any other inbound text: no auto-reply, just logged for an agent to see.
    return Response(content=str(MessagingResponse()), media_type="application/xml")


@router.post("/twilio/call-status")
def call_status(CallSid: str = Form(...), CallStatus: str = Form(""), CallDuration: str = Form("0"),
                 db: Session = Depends(get_db)):
    """Twilio posts final call status here. Used to backfill duration if the
    agent didn't manually log it, and to catch machine/no-answer outcomes."""
    session = db.query(CallSession).filter(CallSession.twilio_call_sid == CallSid).first()
    if session and session.disposition is None:
        status_map = {
            "no-answer": "no_answer",
            "busy": "busy",
            "failed": "failed",
            "completed": "connected",
        }
        session.disposition = status_map.get(CallStatus, CallStatus)
        if CallDuration.isdigit():
            session.duration_seconds = int(CallDuration)
        db.commit()
    return {"status": "received"}

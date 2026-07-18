"""
Compliance gate. Every outbound call, SMS, and email must pass through
here before it goes out. Nothing in dialer.py or blast.py should talk to
Twilio/SendGrid directly without calling can_contact() first.
"""
from datetime import datetime, time as dtime
from typing import Literal

import pytz
from sqlalchemy.orm import Session

from core.config import get_settings
from models import Contact, SuppressedNumber

settings = get_settings()

Channel = Literal["call", "sms", "email"]


class ComplianceResult:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason  # empty if allowed=True

    def __bool__(self):
        return self.allowed


def is_suppressed(db: Session, phone: str | None, email: str | None) -> bool:
    q = db.query(SuppressedNumber)
    if phone:
        if q.filter(SuppressedNumber.phone == phone).first():
            return True
    if email:
        if q.filter(SuppressedNumber.email == email).first():
            return True
    return False


def has_consent(contact: Contact, channel: Channel) -> bool:
    if channel == "call":
        return bool(contact.consent_call)
    if channel == "sms":
        return bool(contact.consent_sms) and not contact.unsubscribed_sms
    if channel == "email":
        return bool(contact.consent_email) and not contact.unsubscribed_email
    return False


def is_within_quiet_hours(contact: Contact) -> bool:
    """
    Returns True if it's currently an ALLOWED time to contact this person
    (i.e. NOT inside the restricted quiet-hours window), based on the
    CONTACT's local timezone - not the agent's, not the server's.
    TCPA guidance is 8am-9pm in the recipient's local time by default.
    """
    tz_name = contact.timezone or settings.default_timezone
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone(settings.default_timezone)

    local_now = datetime.now(tz).time()

    start_h, start_m = (int(x) for x in settings.quiet_hours_start.split(":"))
    end_h, end_m = (int(x) for x in settings.quiet_hours_end.split(":"))
    window_start = dtime(start_h, start_m)
    window_end = dtime(end_h, end_m)

    return window_start <= local_now <= window_end


def can_contact(db: Session, contact: Contact, channel: Channel) -> ComplianceResult:
    """Single entry point. Call this before every dial, text, or email send."""
    if channel in ("call", "sms") and is_suppressed(db, contact.phone, None):
        return ComplianceResult(False, "phone_suppressed")
    if channel == "email" and is_suppressed(db, None, contact.email):
        return ComplianceResult(False, "email_suppressed")

    if contact.dnc_flag and channel in ("call", "sms"):
        return ComplianceResult(False, "dnc_flag")

    if not has_consent(contact, channel):
        return ComplianceResult(False, "no_consent")

    if channel in ("call", "sms") and not is_within_quiet_hours(contact):
        return ComplianceResult(False, "outside_quiet_hours")

    return ComplianceResult(True)

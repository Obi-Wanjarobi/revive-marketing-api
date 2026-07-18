import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Boolean, DateTime, Integer, ForeignKey, Text, Enum, Float
)
from sqlalchemy.orm import relationship

from database import Base


def gen_id():
    return str(uuid.uuid4())


class AgentRole(str, enum.Enum):
    admin = "admin"
    agent = "agent"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, nullable=True)
    password_hash = Column(String, nullable=False)
    role = Column(Enum(AgentRole), default=AgentRole.agent)
    twilio_client_identity = Column(String, nullable=True)  # for Voice SDK token identity
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Contact(Base):
    __tablename__ = "contacts"

    id = Column(String, primary_key=True, default=gen_id)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    phone = Column(String, index=True, nullable=True)  # E.164
    email = Column(String, index=True, nullable=True)
    timezone = Column(String, nullable=True)  # IANA tz name, e.g. America/Denver
    source = Column(String, nullable=True)  # e.g. "pulse_import", "csv_upload"
    pulse_contact_id = Column(String, nullable=True, index=True)  # link back to Pulse CRM record
    tags = Column(String, nullable=True)  # comma-separated for v1

    consent_call = Column(Boolean, default=False)
    consent_sms = Column(Boolean, default=False)
    consent_email = Column(Boolean, default=False)
    consent_source = Column(String, nullable=True)  # how/when consent was captured
    consent_timestamp = Column(DateTime, nullable=True)

    dnc_flag = Column(Boolean, default=False)  # internal or scrubbed-list suppression
    unsubscribed_email = Column(Boolean, default=False)
    unsubscribed_sms = Column(Boolean, default=False)  # STOP keyword received

    created_at = Column(DateTime, default=datetime.utcnow)


class SuppressedNumber(Base):
    """Internal do-not-contact list: STOP replies, manual opt-outs, bounced/invalid numbers."""
    __tablename__ = "suppressed_numbers"

    id = Column(String, primary_key=True, default=gen_id)
    phone = Column(String, index=True, nullable=True)
    email = Column(String, index=True, nullable=True)
    reason = Column(String, nullable=True)  # "stop_keyword", "manual", "bounced", "national_dnc"
    added_at = Column(DateTime, default=datetime.utcnow)


class CampaignType(str, enum.Enum):
    call = "call"
    sms = "sms"
    email = "email"
    multi = "multi"  # email + sms together


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    completed = "completed"


class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    type = Column(Enum(CampaignType), default=CampaignType.multi)
    status = Column(Enum(CampaignStatus), default=CampaignStatus.draft)
    email_subject = Column(String, nullable=True)
    email_body_html = Column(Text, nullable=True)
    sms_body = Column(Text, nullable=True)
    created_by = Column(String, ForeignKey("agents.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CampaignContact(Base):
    __tablename__ = "campaign_contacts"

    id = Column(String, primary_key=True, default=gen_id)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False)
    contact_id = Column(String, ForeignKey("contacts.id"), nullable=False)
    call_status = Column(String, default="pending")  # pending/dialed/connected/voicemail/skipped_dnc
    email_status = Column(String, default="pending")  # pending/sent/failed/skipped_no_consent/skipped_dnc
    sms_status = Column(String, default="pending")


class CallSession(Base):
    __tablename__ = "call_sessions"

    id = Column(String, primary_key=True, default=gen_id)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    contact_id = Column(String, ForeignKey("contacts.id"), nullable=True)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=True)
    twilio_call_sid = Column(String, nullable=True)
    lines_dialed = Column(Integer, default=1)  # 1-3, how many numbers rang in parallel
    disposition = Column(String, nullable=True)  # connected/voicemail/no_answer/busy/wrong_number/dnc_requested
    notes = Column(Text, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)


class BlastMessage(Base):
    __tablename__ = "blast_messages"

    id = Column(String, primary_key=True, default=gen_id)
    campaign_id = Column(String, ForeignKey("campaigns.id"), nullable=False)
    contact_id = Column(String, ForeignKey("contacts.id"), nullable=False)
    channel = Column(String, nullable=False)  # "email" or "sms"
    status = Column(String, default="pending")  # pending/sent/failed/skipped_no_consent/skipped_dnc/skipped_quiet_hours
    provider_sid = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)

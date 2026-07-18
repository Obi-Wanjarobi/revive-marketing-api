from twilio.rest import Client
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

from core.config import get_settings

settings = get_settings()


def get_twilio_client() -> Client | None:
    if not settings.twilio_configured:
        return None
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def generate_voice_access_token(agent_identity: str) -> str | None:
    """Short-lived token the browser softphone uses to register with Twilio Voice."""
    if not (settings.twilio_account_sid and settings.twilio_api_key_sid
            and settings.twilio_api_key_secret and settings.twilio_twiml_app_sid):
        return None

    token = AccessToken(
        settings.twilio_account_sid,
        settings.twilio_api_key_sid,
        settings.twilio_api_key_secret,
        identity=agent_identity,
        ttl=3600,
    )
    voice_grant = VoiceGrant(
        outgoing_application_sid=settings.twilio_twiml_app_sid,
        incoming_allow=True,
    )
    token.add_grant(voice_grant)
    return token.to_jwt()


def send_sms(to_number: str, body: str) -> tuple[str | None, str | None]:
    """Returns (message_sid, error). One of the two will be None."""
    client = get_twilio_client()
    if client is None:
        return None, "twilio_not_configured"
    try:
        kwargs = {"to": to_number, "body": body}
        if settings.twilio_messaging_service_sid:
            kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
        else:
            kwargs["from_"] = settings.twilio_from_number
        msg = client.messages.create(**kwargs)
        return msg.sid, None
    except Exception as e:
        return None, str(e)

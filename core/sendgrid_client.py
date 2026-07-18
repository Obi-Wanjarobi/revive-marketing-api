from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from core.config import get_settings

settings = get_settings()


def send_email(to_email: str, subject: str, html_body: str) -> tuple[str | None, str | None]:
    """Returns (message_id_or_status, error). One of the two will be None."""
    if not settings.sendgrid_configured:
        return None, "sendgrid_not_configured"
    try:
        message = Mail(
            from_email=(settings.sendgrid_from_email, settings.sendgrid_from_name),
            to_emails=to_email,
            subject=subject,
            html_content=html_body,
        )
        sg = SendGridAPIClient(settings.sendgrid_api_key)
        response = sg.send(message)
        return f"status_{response.status_code}", None
    except Exception as e:
        return None, str(e)

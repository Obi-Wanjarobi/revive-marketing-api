from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "sqlite:///./dev.db"

    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720

    internal_api_key: str = ""

    # Dedicated key for the public website lead-intake endpoint (POST /leads).
    # Held server-side by the Netlify lead proxy; never exposed in the browser.
    # Falls back to internal_api_key if left blank.
    leads_ingest_key: str = ""

    pulse_api_base: str = ""
    pulse_internal_key: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_twiml_app_sid: str = ""
    twilio_from_number: str = ""
    twilio_messaging_service_sid: str = ""

    sendgrid_api_key: str = ""
    sendgrid_from_email: str = "marketing@myrevivecapital.com"
    sendgrid_from_name: str = "Revive Capital"

    quiet_hours_start: str = "08:00"
    quiet_hours_end: str = "21:00"
    default_timezone: str = "America/Los_Angeles"

    class Config:
        env_file = ".env"

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def sendgrid_configured(self) -> bool:
        return bool(self.sendgrid_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()

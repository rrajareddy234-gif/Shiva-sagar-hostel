"""Configuration. Secrets come from the environment only; nothing is hardcoded.

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    channel_provider: str
    owner_phone: str | None
    timezone: str

    # Meta Cloud API (only needed when channel_provider == "meta_cloud")
    meta_app_secret: str | None
    meta_access_token: str | None
    meta_phone_number_id: str | None
    meta_verify_token: str | None

    # AI (optional until the AI layer is wired)
    ai_provider: str
    ai_model: str
    ai_api_key: str | None
    ai_base_url: str | None

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database_url=_env("DATABASE_URL", required=True),
            channel_provider=_env("CHANNEL_PROVIDER", "meta_cloud"),
            owner_phone=_env("OWNER_PHONE"),
            timezone=_env("HOSTEL_TIMEZONE", "Asia/Kolkata"),
            meta_app_secret=_env("META_APP_SECRET"),
            meta_access_token=_env("META_ACCESS_TOKEN"),
            meta_phone_number_id=_env("META_PHONE_NUMBER_ID"),
            meta_verify_token=_env("META_VERIFY_TOKEN"),
            ai_provider=_env("AI_PROVIDER", "gemini"),
            ai_model=_env("AI_MODEL", "gemini-2.5-flash"),
            ai_api_key=_env("AI_API_KEY"),
            ai_base_url=_env("AI_BASE_URL"),
        )

    def channel_credentials(self) -> dict:
        """Credentials for the selected channel provider."""
        if self.channel_provider == "meta_cloud":
            return {
                "app_secret": self.meta_app_secret or "",
                "access_token": self.meta_access_token or "",
                "phone_number_id": self.meta_phone_number_id or "",
                "verify_token": self.meta_verify_token or "",
            }
        if self.channel_provider == "peach":
            return {
                "api_key": _env("PEACH_API_KEY", "") or "",
                "phone_number_id": _env("PEACH_PHONE_NUMBER_ID", "") or "",
            }
        raise ValueError(f"unknown CHANNEL_PROVIDER {self.channel_provider!r}")

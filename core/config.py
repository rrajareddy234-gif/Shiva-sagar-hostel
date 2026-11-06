"""Configuration. Secrets come from the environment only; nothing is hardcoded.

Copy `.env.example` to `.env` and fill it in. `.env` is git-ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into the environment, without overwriting what is set.

    Docker passes `.env` in through env_file, so this only matters when the app
    runs directly. Real environment variables always win, which keeps the
    container's behaviour unchanged and lets a test override a single value.
    """
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


# Words accepted for a boolean setting. A value that is not one of these is
# treated as unset, so a typo leaves the safer default in place rather than
# silently flipping it.
_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    return default


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
    ai_timeout: int = 90
    staff_phones: tuple[str, ...] = ()
    # Where a guest's payment screenshot is filed. The image is only ever moved
    # from the chat to this directory and then to the owner; the bot does not
    # read it. Kept out of the database so a large image cannot bloat a backup.
    payment_proof_dir: str = "payment_proofs"
    # A temporary pause on outbound WhatsApp replies. True by default: the
    # system behaves exactly as before unless the owner turns it off. While
    # false, incoming messages are still received, logged and handled, and
    # bookings, availability and payments are untouched -- only the automatic
    # reply to the guest is held back.
    auto_reply_enabled: bool = True

    @staticmethod
    def from_env() -> Settings:
        raw_staff = _env("STAFF_PHONES", "") or ""
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
            ai_model=_env("AI_MODEL", "gemini-flash-lite-latest"),
            ai_api_key=_env("AI_API_KEY"),
            ai_base_url=_env("AI_BASE_URL"),
            ai_timeout=int(_env("AI_TIMEOUT", "90") or 90),
            staff_phones=tuple(p.strip() for p in raw_staff.split(",") if p.strip()),
            payment_proof_dir=_env("PAYMENT_PROOF_DIR", "payment_proofs"),
            auto_reply_enabled=_env_bool("AUTO_REPLY_ENABLED", True),
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

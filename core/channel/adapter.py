"""Provider-independent WhatsApp channel interface.

Business logic talks to `ChannelAdapter` only, never to a concrete provider.
Switching between the official Meta Cloud API and a BSP such as Peach is a
configuration change, not a code change. See docs/ADR-001-whatsapp-channel.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    """One normalised inbound message, whatever provider delivered it."""

    provider_message_id: str          # provider's unique id; used for dedupe
    from_phone: str                   # E.164, e.g. +919876543210
    text: str | None
    message_type: str = "text"        # text | image | audio | document | interactive | ...
    timestamp: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


class ChannelAdapter(ABC):
    """A WhatsApp channel. Implementations must be side-effect free on verify/parse."""

    name: str = "unknown"

    @abstractmethod
    def verify_inbound(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Return True only if the request is authentic (e.g. valid HMAC signature).

        Must never raise on bad input; return False instead.
        """

    @abstractmethod
    def parse_inbound(self, raw_body: bytes) -> list[InboundMessage]:
        """Normalise a verified webhook payload into zero or more messages.

        Status callbacks and other non-message events yield an empty list.
        Must be pure: no database writes, no network calls.
        """

    @abstractmethod
    def send_text(self, to_phone: str, text: str) -> SendResult:
        """Send a free-form text reply. Only valid inside the 24-hour service window."""

    @abstractmethod
    def send_template(self, to_phone: str, template_name: str,
                      params: list[str] | None = None,
                      language: str = "en") -> SendResult:
        """Send an approved template. Required outside the 24-hour service window."""

    def verify_challenge(self, query: dict[str, str]) -> str | None:
        """Handle the GET webhook verification handshake.

        Return the challenge string to echo back, or None if the request is invalid.
        Only providers with a handshake override this.
        """
        return None

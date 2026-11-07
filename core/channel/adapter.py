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
    # Set when the guest tapped a button or a list row: the id we chose, never
    # the label. Routing on the id survives a relabelling or a translation.
    button_id: str | None = None
    # The name the guest uses on WhatsApp, when the provider supplies it. This
    # is a display name, not a verified identity: it may be a handle, a
    # nickname or a business name. It is stored as WHATSAPP_PROFILE so it can
    # never overwrite a name the guest typed or the owner entered.
    profile_name: str | None = None
    # The provider's id for an attached image or document, so the caller can
    # download it. Set for media messages; None for text.
    media_id: str | None = None
    media_type: str | None = None      # provider's mime type, e.g. image/jpeg


@dataclass(frozen=True)
class Button:
    """One tappable answer. `id` is ours; `title` is what the guest reads."""

    id: str
    title: str


@dataclass(frozen=True)
class Prompt:
    """A question with tappable answers, so the guest does not have to type.

    Up to three `buttons` are shown directly under the body. More than three
    options must go in `rows`, which WhatsApp reveals behind one `button_text`.
    """

    body: str
    buttons: tuple[Button, ...] = ()
    rows: tuple[Button, ...] = ()
    button_text: str = "Choose"

    def is_valid(self) -> bool:
        """Meta's limits. A prompt that breaks them is never sent as-is."""
        if self.rows:
            return 1 <= len(self.rows) <= 10 and len(self.button_text) <= 20
        if self.buttons:
            return 1 <= len(self.buttons) <= 3
        return False


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    # False when trying again would only make things worse. Meta's per-pair
    # rate limit is the case that matters: a second send to the same guest
    # while throttled is another message counted against the same limit, so
    # retrying deepens the block instead of working around it.
    retryable: bool = True


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

    @abstractmethod
    def send_interactive(self, to_phone: str, prompt: Prompt) -> SendResult:
        """Send a question with tappable answers.

        Only valid inside the 24-hour service window, like send_text. The caller
        is responsible for falling back to send_text when this fails.
        """

    def send_document(self, to_phone: str, filename: str, content: bytes,
                      media_type: str = "text/csv",
                      caption: str | None = None) -> SendResult:
        """Send a file. Providers that cannot do this keep the default.

        The default reports failure, so the caller falls back to text. This is
        the same contract as send_interactive: a nicety, never a requirement.
        """
        return SendResult(ok=False, error="this channel cannot send documents")

    def fetch_media(self, media_id: str) -> tuple[bytes, str] | None:
        """Download an inbound image or document. None when unsupported.

        The guest's payment screenshot is fetched through here so it can be
        filed and forwarded to the owner. The bot never reads the bytes -- it
        has no way to -- it only moves the image from the chat to the owner.
        """
        return None

    def verify_challenge(self, query: dict[str, str]) -> str | None:
        """Handle the GET webhook verification handshake.

        Return the challenge string to echo back, or None if the request is invalid.
        Only providers with a handshake override this.
        """
        return None

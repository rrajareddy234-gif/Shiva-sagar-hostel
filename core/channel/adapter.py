"""Provider-independent WhatsApp channel interface.

Business logic talks to `ChannelAdapter` only, never to a concrete provider.
Switching between the official Meta Cloud API and a BSP such as Peach is a
configuration change, not a code change. See docs/ADR-001-whatsapp-channel.md.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
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
class DeliveryStatus:
    """One provider delivery-status notification for an outgoing message."""

    provider_message_id: str
    status: str
    timestamp: int | None = None
    recipient_id: str | None = None
    errors: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Button:
    """One tappable answer. `id` is ours; `title` is what the guest reads."""

    id: str
    title: str


# Meta caps the body of any interactive message (buttons or list) at 1024
# characters. An answer longer than this cannot travel in the same message as
# its choices, so the dispatcher splits it deliberately rather than letting Meta
# reject the whole thing. See docs/WHATSAPP_INTERACTIVE_REMEDIATION.md.
MAX_INTERACTIVE_BODY = 1024


def compose_interactive_body(answer: str, question: str) -> str:
    """Compose the one interactive body, shortening the answer to fit Meta's cap.

    The answer and the question must travel together in one interactive message.
    When the pair is too long the answer is shortened at a line boundary --
    dropping the least essential trailing lines rather than the question, which
    tells the guest what the buttons are for. Nothing here sends a second
    message.
    """
    answer = (answer or "").strip()
    question = (question or "").strip()
    if not answer:
        return question[:MAX_INTERACTIVE_BODY]
    if not question or answer == question:
        return answer[:MAX_INTERACTIVE_BODY]
    separator = "\n\n" if answer[-1] in ".!?" else "\n"
    combined = f"{answer}{separator}{question}"
    if len(combined) <= MAX_INTERACTIVE_BODY:
        return combined
    budget = MAX_INTERACTIVE_BODY - len(separator) - len(question)
    if budget <= 0:
        return combined[:MAX_INTERACTIVE_BODY]
    kept: list[str] = []
    used = 0
    for line in answer.split("\n"):
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    trimmed = "\n".join(kept) if kept else answer[:budget]
    return f"{trimmed}{separator}{question}"


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
        """Meta's structural limits. A prompt that breaks them is never sent."""
        if self.rows:
            return 1 <= len(self.rows) <= 10 and len(self.button_text) <= 20
        if self.buttons:
            return 1 <= len(self.buttons) <= 3
        return False

    def body_fits(self) -> bool:
        """True when the body is short enough to travel inside one interactive
        message. A longer body must be split by the caller."""
        return len(self.body or "") <= MAX_INTERACTIVE_BODY


@dataclass(frozen=True)
class Flow:
    """A WhatsApp Flow message: a form the guest fills in natively.

    A Flow is a richer interactive than buttons: the guest sees real widgets
    (a calendar, a dropdown) rendered by WhatsApp itself. We name the published
    `flow_id` and the `screen` to open, and pass `data` the screen reads. The
    guest's answers come back on the webhook as a `FlowResponse`.

    `cta` is the button that opens the Flow, e.g. "Pick dates".
    """

    flow_id: str
    screen: str
    cta: str = "Open"
    body: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    flow_token: str = ""
    mode: str = "navigate"          # navigate | data_exchange
    header: str = ""
    footer: str = ""

    def is_valid(self) -> bool:
        """Meta's limits for a flow message."""
        return bool(self.flow_id and self.screen and self.cta) and len(self.cta) <= 20


@dataclass(frozen=True)
class FlowResponse:
    """The guest's answers from a submitted Flow.

    `data` is the Flow's response object, e.g. ``{"check_in": "2026-12-01",
    "check_out": "2026-12-03"}``. `flow_token` is the value we set when the Flow
    was sent, used to tie the answers back to the conversation that asked.
    """

    provider_message_id: str
    from_phone: str
    flow_token: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# A calendar Flow's screen returns the picked range as either a pair of ISO
# dates or a single "start/end" string, depending on the screen's naming. Every
# spelling is accepted so a published screen is not silently ignored.
FLOW_CHECK_IN_KEYS = ("check_in", "checkin", "check_in_date", "start_date",
                      "start", "from", "date_start")
FLOW_CHECK_OUT_KEYS = ("check_out", "checkout", "check_out_date", "end_date",
                       "end", "to", "date_end")


def _as_iso_date(value: Any) -> str | None:
    """Coerce a Flow date field to an ISO date string, or None if unusable."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept a full timestamp or a range string by taking the leading date.
        text = text.split("T", 1)[0].split(" ", 1)[0]
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None
    return None


def flow_date_range(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read a check-in / check-out range from a Flow's response object.

    Returns ``(None, None)`` when the Flow did not carry a usable range, so a
    malformed submission is refused rather than booked as a wrong date.
    """
    if not isinstance(data, dict):
        return None, None

    check_in = _first_date(data, FLOW_CHECK_IN_KEYS)
    check_out = _first_date(data, FLOW_CHECK_OUT_KEYS)

    if not (check_in and check_out):
        # A single "start/end" field is also accepted.
        for key in ("date_range", "range", "dates", "stay"):
            raw = data.get(key)
            if isinstance(raw, str) and "/" in raw:
                start, _, end = raw.partition("/")
                check_in = check_in or _as_iso_date(start)
                check_out = check_out or _as_iso_date(end)
                break

    return check_in, check_out


def _first_date(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in data:
            parsed = _as_iso_date(data.get(key))
            if parsed:
                return parsed
    return None


def flow_response_data(raw: dict[str, Any]) -> dict[str, Any]:
    """The answer object inside a Flow submission's raw message.

    Meta sends an ``nfm_reply`` whose ``response_json`` is a JSON *string*. A
    malformed or absent payload yields an empty dict, never an exception.
    """
    if not isinstance(raw, dict):
        return {}
    nfm = (raw.get("interactive") or {}).get("nfm_reply") or {}
    if not isinstance(nfm, dict):
        return {}
    raw_json = nfm.get("response_json")
    if isinstance(raw_json, str) and raw_json.strip():
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            return {}
    if isinstance(raw_json, dict):
        return raw_json
    return {}


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
    # Unknown means the provider may have accepted the request; never resend it.
    certainty: str = "rejected"  # accepted | rejected | unknown
    error_code: str | None = None
    http_status: int | None = None
    retry_after_seconds: int | None = None


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

    def parse_statuses(self, raw_body: bytes) -> list[DeliveryStatus]:
        """Parse provider delivery callbacks. Providers may leave this unsupported."""
        return []

    def parse_flow_responses(self, raw_body: bytes) -> list[FlowResponse]:
        """Parse Flow submissions. Providers without Flows return an empty list."""
        return []

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

    def send_flow(self, to_phone: str, flow: Flow) -> SendResult:
        """Send a WhatsApp Flow (a native form, e.g. a calendar picker).

        Providers that cannot send a Flow keep the default, which reports
        failure so the caller can fall back to buttons. This mirrors
        send_interactive: a richer widget is a nicety, never a requirement.
        """
        return SendResult(ok=False, error="this channel cannot send flows")

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

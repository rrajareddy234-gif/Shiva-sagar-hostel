"""Peach (trypeach.ai) channel adapter — documented stub, NOT enabled.

Peach is a Meta Technology Partner (BSP) built on the official WhatsApp Business
API. This file exists so the channel can be switched to Peach later without a
rebuild. It is deliberately inert: it is never selected unless
CHANNEL_PROVIDER=peach is set, and it refuses to operate until it is completed
and reviewed. See docs/ADR-001-whatsapp-channel.md for why Peach is not used.

To finish this adapter you would need, from Peach:
  1. A Peach API key and the Peach phone-number identifier.
  2. Peach's webhook signing scheme, so verify_inbound can be implemented safely.
  3. Peach's inbound webhook payload shape, to fill in parse_inbound.
  4. Peach's send-message and send-template endpoints.

Important: a WhatsApp number is fed to exactly one webhook through one Meta app.
Moving a number between Peach and a direct Cloud API connection is a migration,
not a toggle. This adapter is for running the *whole* system on Peach, never for
running Peach and the direct API side by side on the same number.
"""

from __future__ import annotations

import logging

from core.channel.adapter import ChannelAdapter, InboundMessage, SendResult

log = logging.getLogger(__name__)


class PeachAdapter(ChannelAdapter):
    name = "peach"

    def __init__(self, *, api_key: str, phone_number_id: str, webhook_secret: str | None = None):
        if not api_key or not phone_number_id:
            raise ValueError("Peach adapter requires api_key and phone_number_id")
        self._api_key = api_key
        self._phone_number_id = phone_number_id
        self._webhook_secret = webhook_secret

    def verify_inbound(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        # Fail closed: until Peach's signing scheme is implemented and reviewed,
        # this adapter must never accept a request.
        log.error("peach: verify_inbound is not implemented; rejecting request")
        return False

    def parse_inbound(self, raw_body: bytes) -> list[InboundMessage]:
        raise NotImplementedError("Peach inbound payload mapping is not implemented")

    def send_text(self, to_phone: str, text: str) -> SendResult:
        return SendResult(ok=False, error="Peach adapter is not implemented")

    def send_template(self, to_phone: str, template_name: str,
                      params: list[str] | None = None,
                      language: str = "en") -> SendResult:
        return SendResult(ok=False, error="Peach adapter is not implemented")

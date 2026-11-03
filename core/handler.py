"""Message handling: ties the router, the owner commands and the AI together.

Order of handling for an inbound message:
  1. Owner commands, if the message came from the owner's number.
  2. The deterministic router, which answers common questions from SQL.
  3. The AI layer, only for free text the router could not handle.

Steps 1 and 2 make no AI call, which is what keeps the running cost low.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai.adapter import AIClient, AIUnavailable
from ai.prompts import CUSTOMER_SYSTEM, OWNER_SYSTEM
from core import commands, router, security
from core.db import Database

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Outcome:
    reply: str
    handled_by: str            # owner_command | router | ai | fallback
    intent: str = "unknown"
    escalate: bool = False


def handle_message(db: Database, *, phone: str, text: str, owner_phone: str | None,
                   ai: AIClient | None = None, history: list[dict] | None = None) -> Outcome:
    """Produce the reply for one inbound message."""
    role = security.role_for(phone, owner_phone)

    if security.looks_like_injection(text):
        log.warning("possible prompt injection from %s", phone)

    if role == security.OWNER:
        command_result = commands.handle(db, phone=phone, text=text, owner_phone=owner_phone)
        if command_result is not None:
            return Outcome(reply=command_result.text, handled_by="owner_command",
                           intent=command_result.action or "owner_command")

        owner_reply = router.owner_answer(db, text)
        if owner_reply.handled:
            return Outcome(reply=owner_reply.text, handled_by="router",
                           intent=owner_reply.intent)

    else:
        customer_reply = router.route(db, text, role=role)
        if customer_reply.handled:
            return Outcome(reply=customer_reply.text, handled_by="router",
                           intent=customer_reply.intent)

    if ai is None:
        return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)

    system = OWNER_SYSTEM if role == security.OWNER else CUSTOMER_SYSTEM
    messages = list(history or []) + [{"role": "user", "content": text or ""}]
    try:
        response = ai.complete(system=system, messages=messages)
    except AIUnavailable as exc:
        log.error("AI unavailable: %s", exc)
        return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)

    if not response.text:
        return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)
    return Outcome(reply=response.text, handled_by="ai")


def _fallback(role: str) -> str:
    if role == security.OWNER:
        return ("I could not reach the assistant just now. Your data is safe. "
                "Please try again, or ask me a specific question such as "
                "'how many vacancies today'.")
    return ("Thanks for your message. I'll check with the owner and get back to you "
            "shortly. In the meantime you can ask me about room prices or "
            "availability.")

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
from core import commands, router, security, tools
from core.db import Database

log = logging.getLogger(__name__)

# The model may call tools, read the results and answer. Two rounds is enough
# for a booking conversation; a third would mean the model is looping.
MAX_TOOL_ROUNDS = 3


@dataclass(frozen=True)
class Outcome:
    reply: str
    handled_by: str            # owner_command | router | ai | fallback
    intent: str = "unknown"
    escalate: bool = False
    tool_calls: tuple[str, ...] = ()


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

    return _ask_ai(db, ai=ai, role=role, phone=phone, text=text, history=history)


def _ask_ai(db: Database, *, ai: AIClient, role: str, phone: str, text: str,
            history: list[dict] | None) -> Outcome:
    """Ask the model, letting it call tools, then return its answer."""
    system = OWNER_SYSTEM if role == security.OWNER else CUSTOMER_SYSTEM
    ctx = tools.ToolContext(db=db, phone=phone, role=role)
    specs = tools.specs_for(role)
    messages = list(history or []) + [{"role": "user", "content": text or ""}]
    used: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = ai.complete(system=system, messages=messages, tools=specs)
        except AIUnavailable as exc:
            log.error("AI unavailable: %s", exc)
            return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)
        except Exception as exc:
            log.exception("the AI call failed: %s", exc)
            return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)

        _record(db, response, role=role, phone=phone, purpose="reply")

        if not response.tool_calls:
            if not response.text:
                return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)
            return Outcome(reply=response.text, handled_by="ai", tool_calls=tuple(used))

        # Record the model's request, run each tool, and feed the results back.
        messages.append({"role": "assistant", "content": response.text or "",
                         "tool_calls": [{"id": f"call_{i}", "name": call.name,
                                         "arguments": call.arguments}
                                        for i, call in enumerate(response.tool_calls)]})
        for i, call in enumerate(response.tool_calls):
            used.append(call.name)
            result = tools.execute(ctx, call.name, call.arguments)
            messages.append({"role": "tool", "tool_call_id": f"call_{i}",
                             "name": call.name, "content": _json(result)})

    log.warning("the model kept calling tools without answering for %s", phone)
    return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True,
                   tool_calls=tuple(used))


def _json(value) -> str:
    import json
    return json.dumps(value, default=str)


def _record(db: Database, response, *, role: str, phone: str, purpose: str) -> None:
    """Store the token count and estimated cost, so the owner can see the spend."""
    if not (response.input_tokens or response.output_tokens):
        return
    try:
        from ai.usage import record_usage
        record_usage(db, response, purpose=f"{purpose}:{role}")
    except Exception as exc:  # never let bookkeeping break a reply
        log.warning("could not record AI usage: %s", exc)


def _fallback(role: str) -> str:
    if role == security.OWNER:
        return ("I could not reach the assistant just now. Your data is safe. "
                "Please try again, or ask me a specific question such as "
                "'how many vacancies today'.")
    return ("Thanks for your message. I'll check with the owner and get back to you "
            "shortly. In the meantime you can ask me about room prices or "
            "availability.")

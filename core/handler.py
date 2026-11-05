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
from ai.prompts import CUSTOMER_SYSTEM, OWNER_SYSTEM, STAFF_SYSTEM
from core import commands, hostel_facts, memory, router, security, tools
from core.channel.adapter import Prompt
from core.db import Database
from core.export import DataFile

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
    # Tappable answers to send alongside the reply, when the router offered them.
    prompt: Prompt | None = None
    # A data file to send alongside the reply, e.g. an owner export.
    file: DataFile | None = None
    # Side effects a tool performed that the caller should act on, such as a
    # booking request the owner must be asked to approve. The handler never
    # sends these itself; the dispatcher owns the channel.
    events: tuple[dict, ...] = ()


def handle_message(db: Database, *, phone: str, text: str, owner_phone: str | None,
                   ai: AIClient | None = None, history: list[dict] | None = None,
                   button_id: str | None = None,
                   staff_phones: tuple[str, ...] = ()) -> Outcome:
    """Produce the reply for one inbound message."""
    role = security.role_for(phone, owner_phone, set(staff_phones) or None)

    if security.looks_like_injection(text):
        log.warning("possible prompt injection from %s", phone)

    if role == security.OWNER:
        # A tap on an Approve/Reject button is handled before typed commands:
        # the id names the booking exactly, so there is nothing to resolve.
        tapped = commands.handle_button(db, phone=phone, button_id=button_id,
                                       owner_phone=owner_phone)
        if tapped is not None:
            return Outcome(reply=tapped.text, handled_by="owner_command",
                           intent=tapped.action or "owner_command",
                           prompt=tapped.prompt, file=tapped.file)

        command_result = commands.handle(db, phone=phone, text=text, owner_phone=owner_phone)
        if command_result is not None:
            return Outcome(reply=command_result.text, handled_by="owner_command",
                           intent=command_result.action or "owner_command",
                           prompt=command_result.prompt, file=command_result.file)

        owner_reply = router.owner_answer(db, text)
        if owner_reply.handled:
            return Outcome(reply=owner_reply.text, handled_by="router",
                           intent=owner_reply.intent)

    elif role == security.STAFF:
        staff_reply = router.staff_answer(db, text, button_id=button_id)
        if staff_reply.handled:
            return Outcome(reply=staff_reply.text, handled_by="router",
                           intent=staff_reply.intent, prompt=staff_reply.prompt)

    else:
        # A booking in progress owns the conversation: the guest is answering our
        # own questions, so the flow answers before the router or the AI. A
        # submit here creates the request in code, which is what makes the owner
        # notification reliable.
        from core import booking_flow
        flow = booking_flow.handle(db, phone=phone, text=text, button_id=button_id)
        if flow is not None:
            return Outcome(reply=flow.reply, handled_by="router", intent="book",
                           prompt=flow.prompt, events=flow.events)

        customer_reply = router.route(db, text, role=role, button_id=button_id,
                                      phone=phone)
        if customer_reply.handled:
            return Outcome(reply=customer_reply.text, handled_by="router",
                           intent=customer_reply.intent, prompt=customer_reply.prompt)

    if ai is None:
        return Outcome(reply=_fallback(role), handled_by="fallback", escalate=True)

    return _ask_ai(db, ai=ai, role=role, phone=phone, text=text, history=history)


def _system_for(db: Database, *, role: str, phone: str) -> str:
    """The rules, plus what the database knows about this caller.

    The memory block is facts read from the database, never a summary and never
    a guess. Money is left out on purpose; a tool supplies it live. The hostel
    facts are appended after the memory block, so the assistant answers "where
    are you?" and "is there wifi?" from the owner's own words.
    """
    if role == security.OWNER:
        base = OWNER_SYSTEM
    elif role == security.STAFF:
        base = STAFF_SYSTEM
    else:
        base = CUSTOMER_SYSTEM
    parts = [base, memory.memory_for(db, phone=phone, role=role)]
    facts = hostel_facts.render_for_prompt(db)
    if facts:
        parts.append(facts)
    return "\n\n".join(parts)


def _ask_ai(db: Database, *, ai: AIClient, role: str, phone: str, text: str,
            history: list[dict] | None) -> Outcome:
    """Ask the model, letting it call tools, then return its answer."""
    system = _system_for(db, role=role, phone=phone)
    ctx = tools.ToolContext(db=db, phone=phone, role=role)
    specs = tools.specs_for(role)
    # The caller's current message is already the last stored turn, so history
    # must not repeat it or the model sees the same sentence twice.
    history = list(history or [])
    if history and history[-1].get("role") == "user" \
            and (history[-1].get("content") or "").strip() == (text or "").strip():
        history = history[:-1]
    messages = history + [{"role": "user", "content": text or ""}]
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
            return Outcome(reply=response.text, handled_by="ai", tool_calls=tuple(used),
                           events=tuple(ctx.events))

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
                   tool_calls=tuple(used), events=tuple(ctx.events))


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

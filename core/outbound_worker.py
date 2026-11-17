"""Sends persisted outbox payloads without entering business logic."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core import outbox, send_policy
from core.channel.adapter import Button, Flow, Prompt, SendResult
from core.config import Settings
from core.db import Database

log = logging.getLogger(__name__)


def _owner_identities(settings: Settings) -> list[str]:
    """The owner and staff numbers as bare digit strings, for the claim gate."""
    from core.security import normalise_phone
    numbers = [getattr(settings, "owner_phone", None)]
    numbers += list(getattr(settings, "staff_phones", None) or ())
    return [d for d in (normalise_phone(n).lstrip("+") for n in numbers) if d]


def run_batch(db: Database, channel, settings: Settings, *, limit: int = 20) -> dict:
    phone_scope = settings.meta_phone_number_id or channel.name
    completed = failed = unknown = blocked = 0
    claimed_count = 0
    # The two-key send gate, evaluated once per batch and pushed into the claim
    # query. A blocked message is never selected, so it cannot be sent and
    # cannot be retried in a loop; it simply waits, pending, for the switches.
    # The master switch covers every message, so it gates the owner side too.
    guest_ok = send_policy.guest_replies_allowed(db, settings)
    owner_ok = (send_policy.env_allows(settings)
                and send_policy.owner_notify_allowed(settings))
    owner_ids = _owner_identities(settings)
    for _ in range(max(0, limit)):
        item = outbox.claim_next(db, phone_scope=phone_scope,
                                 owner_phone=settings.owner_phone,
                                 owner_identities=owner_ids,
                                 guest_ok=guest_ok, owner_ok=owner_ok)
        if item is None:
            break
        claimed_count += 1
        # Belt and braces: the claim already filtered by audience, but a message
        # that somehow reaches here while its gate is off is held, not sent.
        if not send_policy.may_send(db, settings, item["recipient"]):
            outbox.release_claim(db, item)
            blocked += 1
            continue
        if item["message_type"] == "booking_notice":
            try:
                _materialize_booking_notice(db, item)
            except Exception as exc:
                result = SendResult(ok=False, error=type(exc).__name__, certainty="rejected",
                                    error_code="booking_notice_render_failed", http_status=500)
                state = outbox.complete_attempt(db, item, result, phone_scope=phone_scope,
                                                owner_phone=settings.owner_phone)
                failed += state == "failed"
                continue
        if not outbox.mark_send_started(db, item):
            continue
        try:
            result = _send(channel, item, settings)
        except Exception as exc:
            # The provider may have received the request before the exception.
            # Unknown is deliberately terminal: no blind duplicate send.
            result = SendResult(ok=False, error=type(exc).__name__, certainty="unknown")
            log.exception("outbound worker send raised for outbox item %s",
                          item["outbound_id"])
        state = outbox.complete_attempt(db, item, result,
                                        phone_scope=phone_scope,
                                        owner_phone=settings.owner_phone)
        if state in ("accepted", "delivered", "read"):
            completed += 1
        elif state == "failed":
            failed += 1
        elif state == "unknown":
            unknown += 1
    return {"claimed": claimed_count, "accepted": completed,
            "failed": failed, "unknown": unknown, "blocked": blocked,
            "sending": send_policy.blocked_summary(db, settings)}


def _materialize_booking_notice(db: Database, item: dict) -> None:
    """Render a committed booking event; this performs no booking mutation.

    The guest conversation is interactive-only, so a notice is sent as one
    interactive message: the notice text, then the terminal actions under it.
    """
    from core import booking, guest_notices, outbox, router
    from core.channel.adapter import compose_interactive_body
    booking_row = booking.get_booking(db, item["payload"]["booking_id"])
    kind = item["payload"]["notice_kind"]
    renderers = {"accepted": guest_notices.acceptance_message,
                 "confirmed": guest_notices.final_confirmation_message,
                 "rejected": guest_notices.rejection_message,
                 "proof": guest_notices.proof_received_message}
    rendered = (renderers[kind](booking_row) if kind == "proof"
                else renderers[kind](db, booking_row))
    prompt = router.terminal_prompt()
    body = compose_interactive_body(rendered, prompt.body)
    combined = Prompt(body=body, rows=prompt.rows, button_text=prompt.button_text)
    if not combined.is_valid():
        raise ValueError("booking notice has no valid choices")
    with db.transaction() as cur:
        cur.execute("UPDATE outbound_messages SET message_type='interactive', "
                    "payload=%s::jsonb, updated_at=now() "
                    "WHERE outbound_id=%s AND state='sending' AND attempt_id=%s",
                    (json.dumps({"prompt": outbox.prompt_payload(combined)},
                                ensure_ascii=False),
                     item["outbound_id"], item["attempt_id"]))
        if cur.rowcount != 1:
            raise RuntimeError("outbound claim lost while rendering booking notice")
    item["message_type"] = "interactive"
    item["payload"] = {"prompt": outbox.prompt_payload(combined)}


def _send(channel, item: dict, settings: Settings):
    payload = item["payload"] or {}
    recipient = item["recipient"]
    kind = item["message_type"]
    if kind == "text":
        return channel.send_text(recipient, payload.get("text", ""))
    if kind == "interactive":
        data = payload.get("prompt") or {}
        prompt = Prompt(
            body=data.get("body", ""),
            buttons=tuple(Button(**button) for button in data.get("buttons", [])),
            rows=tuple(Button(**row) for row in data.get("rows", [])),
            button_text=data.get("button_text", "Choose"),
        )
        return channel.send_interactive(recipient, prompt)
    if kind == "flow":
        data = payload or {}
        flow = Flow(
            flow_id=data.get("flow_id", ""),
            screen=data.get("screen", ""),
            cta=data.get("cta", "Open"),
            body=data.get("body", ""),
            data=data.get("data") or {},
            flow_token=data.get("flow_token", ""),
            mode=data.get("mode", "navigate"),
            header=data.get("header", ""),
            footer=data.get("footer", ""),
        )
        return channel.send_flow(recipient, flow)
    if kind == "template":
        return channel.send_template(recipient, payload["template_name"],
                                     params=payload.get("params"),
                                     language=payload.get("language", "en"))
    if kind == "document":
        content = outbox.read_attachment(settings.payment_proof_dir,
                                         payload["file_ref"])
        result = channel.send_document(recipient, payload.get("filename", "document"),
                                       content,
                                       payload.get("media_type", "application/octet-stream"),
                                       caption=payload.get("caption"))
        if not result.ok:
            # The file carried the answer, so a silent failure leaves the guest
            # waiting for something that never arrives. Say so once; a separate
            # file retry would only delay the notice further. A guest is told
            # interactively, so the fallback does not become the one bare text
            # in an otherwise interactive conversation.
            _send_file_failure(channel, recipient, settings)
        return result
    raise ValueError(f"unsupported outbound message type: {kind}")


def _send_file_failure(channel, recipient: str, settings: Settings) -> None:
    message = ("I could not send the file just now. Please ask me again in a "
               "moment. Nothing has changed in your records.")
    from core import router, security
    owner_phone = getattr(settings, "owner_phone", None)
    staff = set(getattr(settings, "staff_phones", None) or ())
    is_guest = (not owner_phone
                or security.role_for(recipient, owner_phone, staff) == security.CUSTOMER)
    if is_guest:
        prompt = router.terminal_prompt()
        channel.send_interactive(
            recipient, Prompt(body=message, rows=prompt.rows,
                              button_text=prompt.button_text))
    else:
        channel.send_text(recipient, message)


def stage_attachment(root: str, *, content: bytes | None = None,
                     source_path: str | None = None, filename: str) -> str:
    """Copy/create a private spool object and return its basename only."""
    if (content is None) == (source_path is None):
        raise ValueError("provide exactly one of content or source_path")
    if content is None:
        content = Path(source_path).read_bytes()
    return outbox.attach_file(root, content, filename)


def discard_staged_attachment(root: str, reference: str | None) -> None:
    if not reference or Path(reference).name != reference:
        return
    path = Path(root) / "outbound_outbox" / reference
    try:
        if path.parent.resolve() == (Path(root) / "outbound_outbox").resolve():
            path.unlink(missing_ok=True)
    except OSError:
        log.exception("could not remove unattached outbound spool file")

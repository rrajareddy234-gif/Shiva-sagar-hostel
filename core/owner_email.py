"""Email the owner a copy of every booking, so the record outlives the server.

The owner's worry is concrete and has already come true twice: the server was
wiped and the database with it. A booking that exists only in PostgreSQL on one
machine is one disk away from being gone. An email is a copy in a different
place, on a service that is not ours to lose.

Two properties matter:

* Email is best effort. A booking must never fail, and a guest must never wait,
  because an SMTP server is unreachable. Every send here swallows its own error
  and logs it.
* The message is plain text built from the same wording the guest sees, so the
  owner's copy and the guest's copy cannot disagree.

Configuration is by environment variable, so no secret is ever committed:

    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       587 (STARTTLS) by default
    SMTP_USER       the sending Gmail address
    SMTP_PASSWORD   a Gmail *App Password*, not the account password
    OWNER_EMAIL     where the copies go; defaults to SMTP_USER

With no SMTP_HOST set, email is simply off and every call is a no-op. That is
deliberate: the system must run fully without it, and turning it on must be a
change to configuration only.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _config() -> dict | None:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return None
    user = (os.environ.get("SMTP_USER") or "").strip()
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD") or "",
        "sender": user or (os.environ.get("SMTP_FROM") or "").strip(),
        "recipient": (os.environ.get("OWNER_EMAIL") or user).strip(),
    }


def enabled() -> bool:
    """Whether email is configured. Read by the health check and the tests."""
    cfg = _config()
    return bool(cfg and cfg["sender"] and cfg["recipient"])


def send(subject: str, body: str,
         attachments: list[tuple[str, bytes, str]] | None = None) -> bool:
    """Send one plain-text email, with optional attachments. Never raises.

    `attachments` is a list of (filename, content, media_type). The payment
    screenshot goes out this way: WhatsApp media expires on Meta's side, and the
    local copy lives on the same disk as everything else, so the email is the
    only version of the proof that survives both.
    """
    cfg = _config()
    if not cfg or not cfg["sender"] or not cfg["recipient"]:
        return False

    message = EmailMessage()
    message["From"] = cfg["sender"]
    message["To"] = cfg["recipient"]
    message["Subject"] = subject
    message.set_content(body)

    for filename, content, media_type in (attachments or []):
        if not content:
            continue
        maintype, _, subtype = (media_type or "application/octet-stream").partition("/")
        message.add_attachment(content, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=filename)

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
            smtp.ehlo()
            # STARTTLS where offered. Gmail requires it; a plain relay may not
            # offer it, and failing on a missing STARTTLS would lose the copy.
            if smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(message)
        return True
    except Exception as exc:
        log.warning("could not email the owner: %s", exc)
        return False


def booking_email(kind: str, booking: dict, details: str, *,
                  guest: dict | None = None,
                  attachment: tuple[str, bytes, str] | None = None) -> bool:
    """Email one booking event. `kind` is accepted | confirmed | rejected | proof.

    The guest's name and phone are put in the body, not just the reference: the
    owner has to check the money against a person, and a reference alone does not
    say who paid. `attachment` is the payment screenshot, when there is one.
    """
    labels = {
        "accepted": "Booking accepted",
        "proof": "Payment screenshot received",
        "rejected": "Booking not taken",
        "confirmed": "Booking CONFIRMED",
    }
    label = labels.get(kind, kind)
    subject = f"[Sagar PG] {label} {booking.get('reference', '')}".strip()

    guest_block = ""
    if guest:
        guest_block = (f"Guest: {guest.get('name') or 'Unnamed'}\n"
                       f"Phone: {guest.get('phone') or ''}\n\n")

    body = (
        f"{label}\n"
        f"{'=' * len(label)}\n\n"
        f"{guest_block}"
        f"{details}\n\n"
        f"-- \n"
        f"An automatic copy from the Sagar PG booking system.\n"
        f"Keep this email: it is your own record if the server is ever lost.\n"
    )
    return send(subject, body, [attachment] if attachment else None)

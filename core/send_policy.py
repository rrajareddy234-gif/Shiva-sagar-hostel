"""The two-key gate that decides whether an outbound WhatsApp message may go.

Two independent switches must agree before anything leaves the building:

* ``SEND_ENABLED`` in the environment is the master switch. It defaults to
  **false**, so a fresh clone, a test run or a development box cannot message
  anyone by accident. Only an explicit ``SEND_ENABLED=true`` turns it on, and
  the value comes from the environment alone -- nothing in the app or the
  database can raise it.
* For a **guest**, the owner's live pause switch must also be resumed. That is
  the ``AUTO_REPLY_ENABLED`` default and the pause/resume row in the settings
  table, read through ``core.auto_reply``. Either one off means the guest is not
  messaged.

Owner-facing operational notifications (a booking request, an AI outage alert, a
scheduled report) are governed separately by ``OWNER_NOTIFY_ENABLED``, which
defaults to true: the owner must hear about their business even while guests are
paused. They still need ``SEND_ENABLED`` -- the master switch covers every
message.

This module is the single place that answers "may this go out". It is called
from the outbound delivery layer (``core.outbound_worker`` and
``core.outbox``), not only from the dispatcher, so the inline drain, the
scheduled worker, booking notices, payment acknowledgements and retry processing
all pass through the same check.

When a message is blocked it is **left pending**, never marked sent and never
counted as a delivery attempt: the outbox row stays in ``pending`` and the claim
is released, so the message goes out once the switches are turned on. Nothing
retries it in the meantime -- ``claim_next`` simply will not claim it -- so a
disabled gate cannot become a busy retry loop.
"""

from __future__ import annotations

import logging

from core import security

log = logging.getLogger(__name__)

# The audience a message is for. Guests are the sensitive case: a guest message
# must never go out unless both keys agree.
GUEST = "guest"
OWNER = "owner"


def is_guest(recipient: str, owner_phone: str | None,
             staff_phones: set[str] | tuple[str, ...] | None = None) -> bool:
    """True when ``recipient`` is a guest rather than the owner or staff.

    The owner and staff are answered in plain text and are not subject to the
    guest pause; the interactive-only rule and the guest half of the send gate
    are about guests. With no owner number configured every recipient is a guest
    -- the conservative reading.
    """
    if not owner_phone:
        return True
    role = security.role_for(recipient, owner_phone, set(staff_phones or ()))
    return role == security.CUSTOMER


def audience_for(recipient: str, settings) -> str:
    """GUEST or OWNER for one recipient, using the app's settings."""
    owner_phone = getattr(settings, "owner_phone", None)
    staff = getattr(settings, "staff_phones", None) or ()
    return GUEST if is_guest(recipient, owner_phone, staff) else OWNER


def env_allows(settings) -> bool:
    """The master switch. Missing or unreadable settings means: do not send."""
    return bool(getattr(settings, "send_enabled", False))


def owner_notify_allowed(settings) -> bool:
    """Whether owner-facing notifications are permitted by their own policy."""
    return bool(getattr(settings, "owner_notify_enabled", True))


def guest_replies_allowed(db, settings) -> bool:
    """Both keys for a guest: the master switch and the owner's pause.

    The pause is read from the database (via ``core.auto_reply``) so the
    dashboard button and the WhatsApp command take effect at once. A database
    that cannot be read falls back to the ``AUTO_REPLY_ENABLED`` default, so an
    outage never silently turns guest messaging on.
    """
    if not env_allows(settings):
        return False
    from core import auto_reply
    default_enabled = getattr(settings, "auto_reply_enabled", True)
    return not auto_reply.is_paused(db, default_enabled=default_enabled)


def may_send(db, settings, recipient: str) -> bool:
    """Whether one outbound message to ``recipient`` may be sent right now.

    The single decision the delivery layer asks. A guest needs the master switch
    and the resumed pause; the owner needs the master switch and the owner
    notification policy. Missing settings fail closed.
    """
    if not env_allows(settings):
        return False
    if audience_for(recipient, settings) == GUEST:
        return guest_replies_allowed(db, settings)
    return owner_notify_allowed(settings)


def blocked_summary(db, settings) -> str:
    """One short line naming the switches, for a health check or a page.

    Never includes a phone number or a credential. Says what is off, so the
    owner can tell "I paused this" from "the deployment is not allowed to send".
    """
    if not env_allows(settings):
        return "blocked: SEND_ENABLED is not true"
    if owner_notify_allowed(settings):
        owner = "owner notices on"
    else:
        owner = "owner notices off"
    from core import auto_reply
    if auto_reply.is_paused(db, default_enabled=getattr(settings, "auto_reply_enabled", True)):
        return f"guests blocked: replies paused ({owner})"
    return f"sending on ({owner})"

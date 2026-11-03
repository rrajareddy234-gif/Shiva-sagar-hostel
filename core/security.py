"""Authorisation.

Owner-only information and owner-only commands are gated here, by phone number,
before any handler runs. A customer cannot reach owner data by asking for it,
and a message that claims to be from the owner is not trusted on its wording —
only on the number it arrived from.
"""

from __future__ import annotations

import re

OWNER = "owner"
STAFF = "staff"
CUSTOMER = "customer"

# Phrases a customer might use to try to impersonate the owner or extract data.
_INJECTION_PATTERNS = (
    r"i am the owner",
    r"i'?m the owner",
    r"owner here",
    r"this is (the )?owner",
    r"acting (on behalf of|for) the owner",
    r"i have owner (access|permission|rights)",
    r"ignore (all )?(previous|prior|above) (instructions|rules)",
    r"disregard (your|the) (rules|instructions)",
    r"you are now",
    r"new (system|developer) (prompt|message|instruction)",
    r"system\s*:",
    r"grant me (owner|admin)",
    r"show me (the )?(owner|admin) (data|report|revenue|password)",
    r"reveal (the )?(owner|admin|system)",
    r"print (your|the) (system )?(prompt|instructions)",
)


def normalise_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"+{digits}" if digits else ""


def role_for(phone: str | None, owner_phone: str | None,
             staff_phones: set[str] | None = None) -> str:
    """Determine the caller's role from the number the message came from."""
    number = normalise_phone(phone)
    if not number:
        return CUSTOMER
    if owner_phone and number == normalise_phone(owner_phone):
        return OWNER
    if staff_phones and number in {normalise_phone(p) for p in staff_phones}:
        return STAFF
    return CUSTOMER


def is_owner(phone: str | None, owner_phone: str | None) -> bool:
    return role_for(phone, owner_phone) == OWNER


def looks_like_injection(text: str | None) -> bool:
    """Flag messages that try to talk the assistant out of its rules.

    This is a signal, not a filter: the message is still answered normally, but
    the assistant never treats its content as an instruction.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _INJECTION_PATTERNS)

"""Domain errors. Messages are safe to show a customer or owner."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, user-facing failures."""

    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    code = "validation_error"


class NotFound(DomainError):
    code = "not_found"


class BedUnavailable(DomainError):
    """The requested bed is not free for the requested period."""

    code = "bed_unavailable"


class BookingConflict(DomainError):
    """A concurrent write won the race. Safe to retry or report to the owner."""

    code = "booking_conflict"


class InvalidTransition(DomainError):
    code = "invalid_transition"


class PaymentError(DomainError):
    code = "payment_error"


class ChannelError(DomainError):
    code = "channel_error"

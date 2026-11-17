"""The text the owner receives for each scheduled job.

This formatting used to live in JavaScript inside the n8n workflow files, where
nothing tested it. A typo there only showed up as a strange WhatsApp message
from the owner's phone, days later. It lives here instead, next to the figures
it formats, so the same test suite covers it.

Every function takes the JSON that the matching ``/jobs`` endpoint returns and
gives back the message to send, or ``None`` when there is nothing worth saying.
``None`` is not a failure: the health check is silent while everything is fine,
so the owner is not messaged every ten minutes with good news.
"""

from __future__ import annotations

from datetime import datetime

# The scheduler is deliberately quiet when all is well. Sending "everything is
# fine" on a timer trains the owner to ignore the messages, which is exactly
# the wrong habit for the one message that matters.
SILENT_WHEN_HEALTHY = None

MAX_LINES = 20


def _guest(row: dict) -> str:
    """Who the booking is for. The name when we have it, the number otherwise."""
    return row.get("full_name") or row.get("guest") or row.get("phone") or "unknown"


def _when(value: str | None) -> str:
    """An ISO timestamp as the owner would say it: '05 Nov 17:00'.

    Anything unparseable is passed through unchanged. A message with an ugly
    timestamp is still useful; a message that failed to build is not.
    """
    if not value:
        return "?"
    try:
        return datetime.fromisoformat(value).strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return str(value)


def _capped(lines: list[str], total: int) -> list[str]:
    """Keep a long list short, and say how many were left out.

    A WhatsApp message is not a report reader. Ten entries is the useful part;
    the rest is noise the owner scrolls past.
    """
    if len(lines) <= MAX_LINES:
        return lines
    hidden = len(lines) - MAX_LINES
    return lines[:MAX_LINES] + [f"...and {hidden} more"]


def alert_text(detail: str) -> str:
    """One plain sentence when a job could not be built.

    The owner needs to know something is wrong, not which function raised.
    """
    return f"Sagar PG alert: {detail}"


def daily_report_text(report: dict) -> str | None:
    """The morning summary. One line per figure the owner acts on."""
    if not isinstance(report, dict):
        return alert_text("the daily report came back in a shape I do not know")
    if report.get("error"):
        return alert_text("the daily report could not be built. Please check the app.")

    beds = report.get("beds") or {}
    lines = [
        "Sagar PG, morning report",
        f"Beds free: {beds.get('available', '?')} of {beds.get('total', '?')}",
        f"Occupied: {beds.get('occupied', '?')}, blocked: {beds.get('blocked', '?')}",
        f"Arrivals today: {report.get('arrivals_today', '?')}, "
        f"departures: {report.get('departures_today', '?')}",
        f"Overdue checkouts: {report.get('overdue_checkouts', '?')}",
        f"Booking requests waiting: {report.get('pending_requests', '?')}",
        f"Payments to verify: {report.get('payments_awaiting_verification', '?')}",
        f"Outstanding balances: {report.get('outstanding_balances', '?')}",
        f"New enquiries today: {report.get('enquiries_today', '?')}, "
        f"unanswered: {report.get('unanswered_enquiries', '?')}",
        f"Collected today: Rs {report.get('collected_today', '?')}",
        f"Revenue this month: Rs {report.get('revenue_this_month', '?')}",
    ]

    full_rooms = beds.get("full_rooms") or []
    if full_rooms:
        lines.append(f"Full rooms: {', '.join(str(r) for r in full_rooms)}")
    return "\n".join(lines)


def unanswered_enquiries_text(payload: dict) -> str | None:
    """Enquiries nobody has replied to. Silent when there are none."""
    if not isinstance(payload, dict):
        return alert_text("the unanswered-enquiries job returned an odd shape")
    if payload.get("error"):
        return alert_text("the unanswered-enquiries job could not be built.")

    rows = payload.get("enquiries") or []
    if not rows:
        return None
    lines = _capped(
        [f"{_guest(r)} ({r.get('phone', '?')}) - waiting "
         f"{r.get('waiting_minutes', '?')} min" for r in rows],
        len(rows))
    return "\n".join([f"{len(rows)} enquir{'y' if len(rows) == 1 else 'ies'} "
                      "still unanswered"] + lines)


def pending_requests_text(payload: dict) -> str | None:
    """Booking requests waiting on the owner. Silent when there are none."""
    if not isinstance(payload, dict):
        return alert_text("the pending-requests job returned an odd shape")
    if payload.get("error"):
        return alert_text("the pending-requests job could not be built.")

    rows = payload.get("requests") or []
    if not rows:
        return None
    lines = _capped(
        [f"{_guest(r)} - bed {r.get('bed_id', '?')}, "
         f"{_when(r.get('check_in_at'))} to {_when(r.get('check_out_at'))}"
         for r in rows],
        len(rows))
    return "\n".join([f"{len(rows)} booking request"
                      f"{'' if len(rows) == 1 else 's'} waiting for you"] + lines)


def upcoming_text(payload: dict) -> str | None:
    """Tomorrow's arrivals, today's departures and anyone overdue.

    Silent when the day is empty, so a quiet evening sends no message.
    """
    if not isinstance(payload, dict):
        return alert_text("the upcoming job returned an odd shape")
    if payload.get("error"):
        return alert_text("the upcoming job could not be built.")

    arrivals = payload.get("arrivals_tomorrow") or []
    departures = payload.get("departures_today") or []
    overdue = payload.get("overdue") or []
    if not (arrivals or departures or overdue):
        return None

    lines: list[str] = []
    if overdue:
        lines.append(f"OVERDUE ({len(overdue)}):")
        lines += [f"  {_guest(r)} - bed {r.get('bed_id', '?')}, "
                  f"was due {_when(r.get('check_out_at'))}" for r in overdue]
    if arrivals:
        lines.append(f"Arriving tomorrow ({len(arrivals)}):")
        lines += [f"  {_guest(r)} - bed {r.get('bed_id', '?')}, "
                  f"{_when(r.get('check_in_at'))}" for r in arrivals]
    if departures:
        lines.append(f"Leaving today ({len(departures)}):")
        lines += [f"  {_guest(r)} - bed {r.get('bed_id', '?')}, "
                  f"{_when(r.get('check_out_at'))}" for r in departures]
    return "\n".join(_capped(lines, len(lines)))


def health_check_text(payload: dict) -> str | None:
    """Something wrong, or nothing at all.

    Returns ``None`` while the system is healthy. See ``SILENT_WHEN_HEALTHY``.
    """
    if not isinstance(payload, dict):
        return alert_text("the health check returned an odd shape")

    problems = payload.get("problems") or []
    if payload.get("error") or payload.get("ok") is False or problems:
        detail = "; ".join(str(p) for p in problems) or "the health check failed"
        return alert_text(detail)
    return SILENT_WHEN_HEALTHY

"""The text the owner is sent for each scheduled job.

The formatting used to be JavaScript inside the n8n workflows, covered by no
test at all. These tests are the reason it moved into Python: a wrong figure in
a WhatsApp message is invisible until the owner reads it and does not trust it.
"""

from __future__ import annotations

from core import reports


def _report(**overrides) -> dict:
    base = {
        "beds": {"total": 68, "available": 64, "occupied": 4, "blocked": 0,
                 "full_rooms": []},
        "arrivals_today": 1, "departures_today": 2, "overdue_checkouts": 0,
        "pending_requests": 0, "payments_awaiting_verification": 0,
        "outstanding_balances": 3, "enquiries_today": 5,
        "unanswered_enquiries": 1, "collected_today": "4500.00",
        "revenue_this_month": "38000.00",
    }
    base.update(overrides)
    return base


# --- the morning report ---------------------------------------------------

def test_daily_report_names_every_figure_the_owner_acts_on():
    text = reports.daily_report_text(_report())
    for phrase in ("Beds free: 64 of 68", "Arrivals today: 1",
                   "Booking requests waiting: 0", "Collected today: Rs 4500.00",
                   "Revenue this month: Rs 38000.00"):
        assert phrase in text


def test_daily_report_lists_full_rooms_only_when_there_are_some():
    assert "Full rooms" not in reports.daily_report_text(_report())
    with_full = _report(beds={"total": 68, "available": 60, "occupied": 8,
                              "blocked": 0, "full_rooms": ["AC-1", "2NA"]})
    assert "Full rooms: AC-1, 2NA" in reports.daily_report_text(with_full)


def test_daily_report_never_raises_on_a_broken_payload():
    """A job that fails must produce an alert, not a traceback in a log."""
    assert reports.daily_report_text({"error": "boom"})
    assert reports.daily_report_text({})
    assert reports.daily_report_text("not a dict")


# --- silence is the default -----------------------------------------------

def test_a_healthy_system_says_nothing():
    """Message-on-a-timer trains the owner to ignore the one that matters."""
    assert reports.health_check_text({"ok": True, "problems": []}) is None


def test_an_empty_day_says_nothing():
    assert reports.upcoming_text(
        {"arrivals_tomorrow": [], "departures_today": [], "overdue": []}) is None


def test_no_unanswered_enquiries_says_nothing():
    assert reports.unanswered_enquiries_text(
        {"count": 0, "enquiries": []}) is None


def test_no_pending_requests_says_nothing():
    assert reports.pending_requests_text({"count": 0, "requests": []}) is None


# --- speaking up when it matters ------------------------------------------

def test_a_problem_is_reported_with_its_detail():
    text = reports.health_check_text({"ok": False, "problems": ["database: down"]})
    assert text and "database: down" in text


def test_pending_requests_are_named_with_their_dates():
    text = reports.pending_requests_text({"requests": [{
        "guest": "Ramesh", "bed_id": "AC-B1",
        "check_in_at": "2026-11-06T10:00:00+05:30",
        "check_out_at": "2026-11-08T10:00:00+05:30"}]})
    assert "Ramesh" in text and "AC-B1" in text and "06 Nov 10:00" in text


def test_one_request_is_not_called_requests():
    text = reports.pending_requests_text({"requests": [
        {"guest": "A", "bed_id": "B1", "check_in_at": None, "check_out_at": None}]})
    assert "1 booking request waiting" in text


def test_overdue_checkouts_come_first():
    """The most urgent item is read first, so it is written first."""
    text = reports.upcoming_text({
        "arrivals_tomorrow": [{"guest": "New", "bed_id": "1", "check_in_at": None}],
        "departures_today": [],
        "overdue": [{"guest": "Late", "bed_id": "2", "check_out_at": None}]})
    assert text.index("OVERDUE") < text.index("Arriving")


def test_a_guest_with_no_name_is_shown_by_phone():
    text = reports.pending_requests_text({"requests": [{
        "guest": None, "phone": "+919000000901", "bed_id": "B1",
        "check_in_at": None, "check_out_at": None}]})
    assert "+919000000901" in text


def test_a_long_list_is_capped_and_says_how_many_were_left_out():
    """A WhatsApp message is not a report reader."""
    rows = [{"guest": f"G{i}", "bed_id": "B", "check_in_at": None,
             "check_out_at": None} for i in range(40)]
    text = reports.pending_requests_text({"requests": rows})
    assert "and 20 more" in text
    assert text.count("G") < 40


def test_an_unparseable_timestamp_is_passed_through_not_crashed():
    text = reports.pending_requests_text({"requests": [{
        "guest": "A", "bed_id": "B1", "check_in_at": "not-a-date",
        "check_out_at": None}]})
    assert "not-a-date" in text

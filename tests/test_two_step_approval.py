"""The owner approves twice, and the guest hears about both.

What these tests defend:

* The first approval accepts and asks the guest to pay; it never confirms.
* The guest is told everything they chose, and warned which name the UPI id
  must show before they send money.
* The guest's screenshot is what brings the booking back to the owner for the
  second approval, and the final confirmation carries the full details again.
* An acceptance the guest never pays for does not hold a bed for ever.
* The owner's copy goes to email, twice, and email is off unless configured.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core import booking, booking_notices, guest_notices, owner_email
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
GUEST = "+919000000777"


def _request(db, *, bed="1NA-B1", phone=GUEST):
    start = datetime(2027, 3, 1, 12, tzinfo=IST)
    return booking.create_request(
        db, phone=phone, full_name="Asha Rao", gender="FEMALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=2),
        food_included=True)


# --- the two steps ---------------------------------------------------------

def test_accept_does_not_confirm_the_booking(db):
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    assert booking.get_booking(db, request.booking_id)["status"] == "ACCEPTED"


def test_confirm_after_proof(db):
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    booking.approve(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    assert row["status"] == "CONFIRMED"
    assert row["final_confirmed_at"] is not None


def test_proof_cannot_be_recorded_before_acceptance(db):
    from core.errors import InvalidTransition

    request = _request(db)
    with pytest.raises(InvalidTransition):
        booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")


def test_an_unpaid_acceptance_lets_the_bed_go(db):
    """The whole point of expiring ACCEPTED: a bed is not blocked for ever."""
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)

    # Move the hold into the past, as a lapsed payment window would. The hold's
    # own rule is that it expires after it was created, so both move together.
    db.execute("UPDATE bed_holds SET created_at = now() - interval '2 hours', "
               "expires_at = now() - interval '1 hour' WHERE status = 'ACTIVE'")

    expired = booking.expire_lapsed_requests(db)
    assert expired == 1
    assert booking.get_booking(db, request.booking_id)["status"] == "EXPIRED"


# --- what the guest is told ------------------------------------------------

def test_the_acceptance_message_carries_every_choice_and_the_upi_id(db):
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)

    message = guest_notices.acceptance_message(db, row)
    assert "ACCEPTED" in message
    assert row["reference"] in message
    assert "Non-AC" in message            # the room they chose
    assert "1NA-B1" in message            # the bed
    assert "2027-03-01" in message        # the check-in date
    assert "2 night" in message           # the length of stay
    assert "with food" in message
    assert "Total:" in message
    assert "8688397996@postbank" in message, "the UPI id must be given"
    assert "GUTTIKONDA RAJAVARDHAN REDDY" in message, "the payee name must be given"
    assert "do NOT pay" in message, "the guest must be warned to check the name"
    assert "screenshot" in message


def test_the_final_message_repeats_every_detail(db):
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    booking.approve(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)

    message = guest_notices.final_confirmation_message(db, row)
    assert "CONFIRMED" in message
    assert row["reference"] in message
    assert "Non-AC" in message and "1NA-B1" in message and "Total:" in message


def test_a_changed_upi_fact_is_used_not_the_default(db):
    """The owner's own values win, so the id can be changed without a deploy."""
    request = _request(db)
    db.execute(
        "INSERT INTO hostel_facts (key, value, category, source) "
        "VALUES ('payment.upi_id', 'owner@bank', 'payment', 'owner') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    db.execute(
        "INSERT INTO hostel_facts (key, value, category, source) "
        "VALUES ('payment.payee_name', 'R G', 'payment', 'owner') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    booking.accept(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    message = guest_notices.acceptance_message(db, row)
    assert "owner@bank" in message and "R G" in message


# --- the owner's email copy ------------------------------------------------

def test_email_is_off_unless_configured(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "OWNER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    assert owner_email.enabled() is False
    assert owner_email.send("subject", "body") is False


def test_email_sends_when_configured(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def has_extn(self, name):
            return name == "starttls"

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "owner@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("OWNER_EMAIL", "rrajareddy234@gmail.com")
    monkeypatch.setattr(owner_email.smtplib, "SMTP", FakeSMTP)

    assert owner_email.enabled() is True
    assert owner_email.send("A subject", "A body") is True
    assert sent["host"] == "smtp.gmail.com"
    assert sent["tls"] is True
    assert sent["login"] == ("owner@gmail.com", "app-password")
    assert sent["message"]["To"] == "rrajareddy234@gmail.com"
    assert sent["message"]["Subject"] == "A subject"


def test_a_failed_email_never_raises(monkeypatch):
    class BrokenSMTP:
        def __init__(self, *args, **kwargs):
            raise OSError("network down")

    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_USER", "owner@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(owner_email.smtplib, "SMTP", BrokenSMTP)
    # The booking must not fail because the mail server is unreachable.
    assert owner_email.send("subject", "body") is False


def test_the_owner_is_emailed_twice_across_the_two_approvals(db, monkeypatch):
    """One copy on acceptance, one on final confirmation: the owner's rule."""
    calls = []

    def fake_booking_email(kind, row, details, **kwargs):
        calls.append(kind)
        return True

    monkeypatch.setattr(owner_email, "booking_email", fake_booking_email)

    from core import booking_notices

    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    booking_notices.notify_accepted(db, row, channel=None)

    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    booking.approve(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    booking_notices.notify_confirmed(db, row, channel=None)

    assert calls == ["accepted", "confirmed"]




# --- the owner's copy carries the proof and who paid -----------------------

def _capture_email(monkeypatch):
    """Stand in for the SMTP call and keep the built message."""
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def has_extn(self, name):
            return False

        def login(self, *a):
            pass

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("OWNER_EMAIL", "rrajareddy234@gmail.com")
    monkeypatch.setattr(owner_email.smtplib, "SMTP", FakeSMTP)
    return sent


def test_the_proof_email_carries_the_screenshot_as_an_attachment(db, monkeypatch):
    """The owner's rule: the payment proof must reach the inbox, not just WhatsApp."""
    sent = _capture_email(monkeypatch)

    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    row = booking.get_booking(db, request.booking_id)

    booking_notices.notify_proof_received(
        db, row, channel=None, owner_phone=OWNER,
        content=b"\xff\xd8\xff\xe0fake-jpeg", filename="proof.jpg",
        media_type="image/jpeg")

    message = sent["message"]
    parts = list(message.iter_attachments())
    assert len(parts) == 1, "the screenshot must be attached"
    assert parts[0].get_filename() == "proof.jpg"
    assert parts[0].get_content_type() == "image/jpeg"
    assert parts[0].get_payload(decode=True) == b"\xff\xd8\xff\xe0fake-jpeg"


def test_every_email_names_the_guest_and_the_phone(db, monkeypatch):
    """A reference alone does not say who paid; the owner needs the person."""
    sent = _capture_email(monkeypatch)

    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)
    booking_notices.notify_accepted(db, row, channel=None)

    body = sent["message"].get_body().get_content()
    assert "Asha Rao" in body, "the guest's name must be in the owner's copy"
    assert GUEST in body, "the guest's phone must be in the owner's copy"


def test_the_proof_email_does_not_tell_the_owner_to_pay(db, monkeypatch):
    """The proof copy shows the booking, but the instruction to pay is for the guest."""
    sent = _capture_email(monkeypatch)

    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    row = booking.get_booking(db, request.booking_id)
    booking_notices.notify_proof_received(
        db, row, channel=None, owner_phone=OWNER,
        content=b"\xff\xd8\xff\xe0fake-jpeg", filename="proof.jpg")

    body = sent["message"].get_body().get_content()
    assert row["reference"] in body
    assert "8688397996@postbank" not in body, "the owner is not the one paying"



# --- the guest is told, from either path ----------------------------------

def test_rejection_tells_the_guest_and_offers_the_refund_path(db):
    """A guest asked to pay must not be left in silence when turned down."""
    request = _request(db)
    booking.accept(db, request.booking_id, actor=OWNER)
    booking.reject(db, request.booking_id, actor=OWNER)
    row = booking.get_booking(db, request.booking_id)

    message = guest_notices.rejection_message(db, row)
    assert row["reference"] in message
    assert "not held" in message
    assert "refund" in message, "a guest who paid must be told how to get it back"


def test_notify_decision_routes_each_action(db, monkeypatch):
    """One entry point for both the chat and the page, so they cannot drift."""
    seen = []
    monkeypatch.setattr(booking_notices, "notify_accepted",
                        lambda db, b, *, channel: seen.append(("accepted", channel)))
    monkeypatch.setattr(booking_notices, "notify_confirmed",
                        lambda db, b, *, channel: seen.append(("confirmed", channel)))
    monkeypatch.setattr(booking_notices, "notify_rejected",
                        lambda db, b, *, channel: seen.append(("rejected", channel)))

    row = {"reference": "SPG-2026-000001"}
    booking_notices.notify_decision(db, row, channel=None, action="accepted")
    booking_notices.notify_decision(db, row, channel=None, action="confirmed")
    booking_notices.notify_decision(db, row, channel=None, action="rejected")
    assert [a for a, _ in seen] == ["accepted", "confirmed", "rejected"]
    # An unknown action is ignored, never an exception: the booking has already
    # changed and a missing notice must not undo it.
    assert booking_notices.notify_decision(db, row, channel=None,
                                           action="nonsense") is False


def test_the_dashboard_approval_tells_the_guest(db, monkeypatch):
    """The bug this fixes: a tap on the page used to change the booking in silence."""
    from core import dashboard

    told = []
    monkeypatch.setattr(booking_notices, "notify_accepted",
                        lambda db, b, *, channel: told.append(("accepted", b["reference"])))
    monkeypatch.setattr(booking_notices, "notify_confirmed",
                        lambda db, b, *, channel: told.append(("confirmed", b["reference"])))

    request = _request(db)
    dashboard.approve_booking(db, request.reference, actor="owner-dashboard",
                              channel="fake-channel")
    assert told == [("accepted", request.reference)]

    booking.mark_proof_sent(db, request.booking_id, proof_path="/tmp/p.jpg")
    dashboard.approve_booking(db, request.reference, actor="owner-dashboard",
                              channel="fake-channel")
    assert told == [("accepted", request.reference), ("confirmed", request.reference)]


def test_the_dashboard_rejection_tells_the_guest(db, monkeypatch):
    from core import dashboard

    told = []
    monkeypatch.setattr(booking_notices, "notify_rejected",
                        lambda db, b, *, channel: told.append(b["reference"]))

    request = _request(db)
    dashboard.reject_booking(db, request.reference, actor="owner-dashboard",
                             channel="fake-channel")
    assert told == [request.reference]


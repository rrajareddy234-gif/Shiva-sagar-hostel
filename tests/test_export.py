"""Owner data exports, sent as CSV over WhatsApp.

The line these tests defend: a file is only ever sent to the owner, the numbers
in it come from the database, and a guest's typing cannot become a formula in
the owner's spreadsheet.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

import pytest

from core import billing, booking, commands, export, payments
from tests.conftest import IST

pytestmark = pytest.mark.usefixtures("seeded")

OWNER = "+919999999999"
STAFF = "+919888888888"


def _now():
    return datetime.now(IST)


def _confirmed(db, *, phone="+919000000901", name="Ramesh Kumar", bed="1NA-B1",
               days=3):
    start = _now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    request = booking.create_request(
        db, phone=phone, full_name=name, gender="MALE", bed_id=bed,
        check_in_at=start, check_out_at=start + timedelta(days=days),
        food_included=True)
    booking.approve(db, request.booking_id, actor=OWNER)
    return request


def _rows(data):
    return list(csv.reader(io.StringIO(data.text)))


# --- the owner can ask, and nobody else can --------------------------------

def test_the_owner_gets_a_customers_file(db):
    _confirmed(db)
    result = commands.handle(db, phone=OWNER, text="export customers",
                             owner_phone=OWNER)
    assert result.action == "export_data"
    assert result.file is not None
    assert result.file.filename.startswith("customers-")
    assert result.file.filename.endswith(".csv")


def test_a_staff_number_cannot_export(db):
    _confirmed(db)
    assert commands.handle(db, phone=STAFF, text="export customers",
                           owner_phone=OWNER) is None


def test_a_guest_number_cannot_export(db):
    _confirmed(db)
    assert commands.handle(db, phone="+919000000901", text="export customers",
                           owner_phone=OWNER) is None


def test_the_owner_can_ask_in_plain_words(db):
    _confirmed(db)
    for phrase in ("send me the payments file", "download bookings",
                   "give me the guest list", "backup customers"):
        result = commands.handle(db, phone=OWNER, text=phrase, owner_phone=OWNER)
        assert result is not None and result.file is not None, phrase


# --- the numbers come from the database ------------------------------------

def test_the_customer_file_carries_the_bill_and_the_balance(db):
    request = _confirmed(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=200, method="UPI", actor=OWNER, verified=True)
    data = export.customer_file(db, _now())
    header, row = _rows(data)[0], _rows(data)[1]
    values = dict(zip(header, row))
    assert values["name"] == "Ramesh Kumar"
    assert values["phone"] == "919000000901"
    assert values["since"]
    # Non-AC with food is 250 a night for three nights.
    assert values["bill"] == "750.00"
    assert values["paid"] == "200.00"
    assert values["due"] == "550.00"


def test_the_booking_file_lists_the_bed_and_the_room(db):
    _confirmed(db)
    data = export.booking_file(db, _now())
    values = dict(zip(_rows(data)[0], _rows(data)[1]))
    assert values["bed"] == "1NA-B1"
    assert values["room"] == "1NA"
    assert values["food"] == "yes"


def test_extras_reach_the_booking_file(db):
    request = _confirmed(db)
    billing.add_extra(db, booking_id=request.booking_id, name="Towel",
                      quantity=2, actor=OWNER)
    values = dict(zip(*_rows(export.booking_file(db, _now()))[:2]))
    assert values["extras"] == "100.00"


def test_the_payment_file_shows_an_unverified_payment_as_unverified(db):
    request = _confirmed(db)
    payments.record_payment(db, booking_id=request.booking_id, guest_id=None,
                            amount=200, method="UPI", actor=OWNER, verified=False)
    data = export.payment_file(db, _now())
    values = dict(zip(_rows(data)[0], _rows(data)[1]))
    assert values["verified"] == "no"
    assert values["amount"] == "200.00"
    assert values["booking"].startswith("SPG-")
    # The guest is taken from the booking when the payment has no guest of its own.
    assert values["guest"] == "Ramesh Kumar"


# --- a guest cannot attack the owner's spreadsheet -------------------------

def test_a_name_starting_with_equals_is_not_a_formula(db):
    booking.create_request(
        db, phone="+919000000902", full_name="=cmd|'/c calc'!A1", gender="MALE",
        bed_id="1NA-B1",
        check_in_at=_now() + timedelta(days=1),
        check_out_at=_now() + timedelta(days=2), food_included=False)
    data = export.customer_file(db, _now())
    name = dict(zip(*_rows(data)[:2]))["name"]
    assert not name.startswith("=")
    assert name.startswith("'=")


def test_a_phone_is_digits_only(db):
    _confirmed(db)
    phone = dict(zip(*_rows(export.customer_file(db, _now()))[:2]))["phone"]
    assert phone == "919000000901"
    assert "+" not in phone


# --- the file itself is well formed ----------------------------------------

def test_the_file_opens_in_excel(db):
    _confirmed(db)
    data = export.customer_file(db, _now())
    # The byte-order mark is what makes Excel read the Indian names correctly.
    assert data.content.startswith(b"\xef\xbb\xbf")


def test_an_unknown_export_name_is_refused(db):
    assert export.build(db, "secrets", _now()) is None


def test_an_unrecognised_export_is_treated_as_a_question(db):
    # "export" with no known noun is not a command, so the AI layer can answer.
    assert commands.handle(db, phone=OWNER, text="export the whole hostel",
                           owner_phone=OWNER) is None


def test_an_empty_hostel_still_produces_a_file(db):
    data = export.customer_file(db, _now())
    assert _rows(data) == [["name", "phone", "gender", "notes", "since",
                            "latest_booking", "status", "bed", "check_in",
                            "check_out", "bill", "paid", "due"]]


# --- the file actually leaves the building ---------------------------------

def _settings():
    from core.config import Settings
    from tests.conftest import APP_SECRET, TEST_DSN, VERIFY_TOKEN
    return Settings(
        database_url=TEST_DSN, channel_provider="fake", owner_phone=OWNER,
        timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="1",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="gemini-2.5-flash", ai_api_key=None, ai_base_url=None)


def _dispatch(db, channel, text, sender=OWNER):
    from core.channel.adapter import InboundMessage
    from core.dispatcher import dispatch

    dispatch(db, channel, _settings(), InboundMessage(
        provider_message_id="wamid.TEST", from_phone=sender,
        message_type="text", text=text))


def test_the_owner_receives_the_file_as_a_document(db, channel):
    _confirmed(db)
    _dispatch(db, channel, "export customers")
    assert len(channel.documents) == 1
    to_phone, filename, content = channel.documents[0]
    assert to_phone == OWNER
    assert filename.startswith("customers-")
    assert b"Ramesh Kumar" in content


def test_a_failed_document_send_says_so_instead_of_pretending(db, channel):
    _confirmed(db)
    channel.fail_document = True
    _dispatch(db, channel, "export customers")
    assert channel.documents == []
    # The owner is told the file did not go, not left waiting for it.
    assert any("could not send the file" in text for _, text in channel.sent)


def test_a_guest_cannot_make_the_bot_send_a_file(db, channel):
    _confirmed(db)
    _dispatch(db, channel, "export customers", sender="+919000000901")
    assert channel.documents == []

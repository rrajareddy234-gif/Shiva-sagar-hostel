"""One guest, from first message to checkout, through the real webhook.

Every step goes through the HTTP endpoint and the real database. The only fake
part is the WhatsApp channel, which records what would have been sent instead of
calling Meta.

The point of this file is the joins: a message arriving, a booking being created
and approved, money being recorded, a bed being freed. Unit tests check each
piece; this checks they fit together.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from tests.conftest import APP_SECRET  # noqa: F401
from tests.conftest import client  # noqa: F401

OWNER = "+919999999999"          # as stored after normalisation
OWNER_SENDS = "919999999999"     # as Meta delivers it, no plus
GUEST = "+919000007777"
GUEST_SENDS = "919000007777"


_counter = [0]


def send(client, text, *, sender=GUEST_SENDS, message_id=None):
    """Post a signed inbound message and return the reply the guest would see.

    Each message gets a unique id. Reusing one would (correctly) be treated as a
    duplicate delivery and ignored, which is not what a test of two separate
    messages wants.
    """
    if message_id is None:
        _counter[0] += 1
        message_id = f"wamid.E2E{_counter[0]}"
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": sender, "id": message_id, "timestamp": "1735689600",
                          "type": "text", "text": {"body": text}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post("/webhook", content=body,
                           headers={"X-Hub-Signature-256": f"sha256={sig}",
                                    "Content-Type": "application/json"})
    assert response.status_code == 200
    return response


def last_reply(channel, phone=None):
    sent = [s for s in channel.sent if phone is None or s[0] == phone]
    return sent[-1][1] if sent else ""


def tap(client, button_id, *, sender=GUEST_SENDS, message_id=None):
    """Post a signed button tap, as WhatsApp delivers it when a guest taps."""
    if message_id is None:
        _counter[0] += 1
        message_id = f"wamid.TAP{_counter[0]}"
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": sender, "id": message_id, "timestamp": "1735689600",
                          "type": "interactive",
                          "interactive": {"type": "button_reply",
                                          "button_reply": {"id": button_id,
                                                           "title": "tapped"}}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": f"sha256={sig}",
                                "Content-Type": "application/json"})


def last_prompt(channel, phone=None):
    """The body of the last tappable prompt sent, or '' if none was."""
    prompts = [p for ph, p in channel.prompts if phone is None or ph == phone]
    return prompts[-1].body if prompts else ""


# --- the full journey ------------------------------------------------------

def test_a_guest_books_stays_and_leaves(client, channel, db):
    """Enquiry, booking request, owner approval, payment, check-in, checkout."""
    from core import booking

    # 1. The guest asks for prices. The router answers with taps, not a wall of
    #    text, and no AI call is made.
    send(client, "what are your prices?")
    assert "which room type" in last_prompt(channel, GUEST).lower()
    assert db.query_one(
        "SELECT count(*) AS n FROM ai_usage")["n"] == 0, "no AI call was needed"

    # 1b. The guest taps AC, then "with food", and gets exactly that one rate.
    tap(client, "price:room:AC")
    assert "with food or without" in last_prompt(channel, GUEST).lower()
    tap(client, "price:food:AC:1")
    assert "350" in last_reply(channel, GUEST), "AC with food is Rs 350 a night"
    assert "450" in last_reply(channel, GUEST), "AC with food is Rs 450 a day"

    # 2. The guest makes a booking request through the tool the AI would call.
    from core import tools
    ctx = tools.ToolContext(db=db, phone="+919000007777", role="CUSTOMER")
    result = tools.execute(ctx, "create_booking_request", {
        "full_name": "Dummy Guest", "check_in": "2026-12-01",
        "check_out": "2026-12-03", "room_type": "NON_AC", "food_included": True})
    assert result["status"] == "REQUESTED"
    reference = result["reference"]
    bed_id = result["bed_id"]

    # 3. The owner is told, and the guest is told it is not confirmed yet.
    request = booking.pending_requests(db)
    assert len(request) == 1
    assert request[0]["reference"] == reference

    # 4. The owner approves from the owner number, with a confirmation step.
    send(client, f"approve booking {reference}", sender=OWNER_SENDS)
    assert "YES" in last_reply(channel, OWNER).upper(), "must ask for confirmation"
    send(client, "YES", sender=OWNER_SENDS)

    row = db.query_one("SELECT status FROM bookings WHERE reference = %s", (reference,))
    assert row["status"] == "CONFIRMED"

    # 5. The guest's bed is now taken for those dates.
    from datetime import datetime
    from tests.conftest import IST
    from core import availability
    free = availability.list_bed_availability(
        db, datetime(2026, 12, 1, 12, tzinfo=IST), datetime(2026, 12, 3, 10, tzinfo=IST))
    assert not next(b for b in free if b.bed_id == bed_id).available

    # 6. The owner records an advance. Money is not claimed until verified.
    send(client, f"record payment of 500 by upi for {reference}", sender=OWNER_SENDS)
    assert "Reply YES to confirm" in last_reply(channel, OWNER), \
        f"unexpected: {last_reply(channel, OWNER)}"
    send(client, "YES", sender=OWNER_SENDS)
    assert "Recorded" in last_reply(channel, OWNER), \
        f"unexpected: {last_reply(channel, OWNER)}"
    payment = db.query_one(
        "SELECT amount, verified FROM payments WHERE booking_id = "
        "(SELECT booking_id FROM bookings WHERE reference = %s)", (reference,))
    assert payment["amount"] == 500
    assert payment["verified"] is False, "a recorded payment is not yet verified"

    # 7. Check in, then check out. The bed is freed but goes to cleaning first,
    #    so a guest never arrives at an uncleaned bed.
    send(client, f"check in {reference}", sender=OWNER_SENDS)
    send(client, "YES", sender=OWNER_SENDS)
    assert db.query_one("SELECT status FROM bookings WHERE reference = %s",
                        (reference,))["status"] == "CHECKED_IN"

    send(client, f"check out {reference}", sender=OWNER_SENDS)
    send(client, "YES", sender=OWNER_SENDS)
    assert db.query_one("SELECT status FROM bookings WHERE reference = %s",
                        (reference,))["status"] == "CHECKED_OUT"

    def bed():
        return next(b for b in availability.list_bed_availability(
            db, datetime(2026, 12, 1, 12, tzinfo=IST),
            datetime(2026, 12, 3, 10, tzinfo=IST)) if b.bed_id == bed_id)

    assert bed().status == "CLEANING"
    assert not bed().available, "a bed awaiting cleaning is not available"

    # 8. The owner marks the bed ready. Only now is it bookable again.
    send(client, f"bed {bed_id} is ready", sender=OWNER_SENDS)
    send(client, "YES", sender=OWNER_SENDS)
    assert bed().available, "the bed must be free once it has been cleaned"


def test_a_guest_cannot_approve_their_own_booking(client, channel, db):
    from core import tools
    ctx = tools.ToolContext(db=db, phone="+919000007777", role="CUSTOMER")
    reference = tools.execute(ctx, "create_booking_request", {
        "full_name": "Dummy Guest", "check_in": "2026-12-01",
        "check_out": "2026-12-02", "food_included": True})["reference"]

    send(client, f"approve booking {reference}", sender=GUEST_SENDS)
    send(client, "YES", sender=GUEST_SENDS)

    status = db.query_one("SELECT status FROM bookings WHERE reference = %s",
                          (reference,))["status"]
    assert status == "REQUESTED", "a guest must never approve a booking"


def test_a_guest_cannot_read_the_owner_summary(client, channel, db):
    send(client, "how much revenue did we collect this month", sender=GUEST_SENDS)
    reply = last_reply(channel, GUEST)
    assert "revenue" not in reply.lower() or "confirm" in reply.lower()
    # The exact figure must never appear.
    assert "Verified revenue" not in reply


def test_the_owner_can_read_the_summary(client, channel):
    send(client, "how much revenue did we collect this month", sender=OWNER_SENDS)
    assert "Verified revenue" in last_reply(channel, OWNER)


def test_a_duplicate_webhook_delivery_replies_once(client, channel, db):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "123456789"},
            "messages": [{"from": GUEST, "id": "wamid.DUPE1", "timestamp": "1735689600",
                          "type": "text", "text": {"body": "what are your prices?"}}],
        }}]}],
    }).encode()
    sig = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={sig}",
               "Content-Type": "application/json"}

    client.post("/webhook", content=body, headers=headers)
    client.post("/webhook", content=body, headers=headers)

    replies = [s for s in channel.sent if s[0] == GUEST]
    prompts = [p for ph, p in channel.prompts if ph == GUEST]
    assert len(replies) + len(prompts) == 1, \
        "Meta retries deliveries; the guest must not get two replies"


def test_an_outage_leaves_the_message_for_a_retry(client, db):
    """If sending fails, the message stays unprocessed so it can be retried."""
    from tests.conftest import FakeChannel
    from api.webhook import create_app
    from core.config import Settings
    from fastapi.testclient import TestClient
    from tests.conftest import TEST_DSN, VERIFY_TOKEN

    broken = FakeChannel(fail=True)
    settings = Settings(
        database_url=TEST_DSN, channel_provider="meta_cloud",
        owner_phone=OWNER, timezone="Asia/Kolkata", meta_app_secret=APP_SECRET,
        meta_access_token="t", meta_phone_number_id="123456789",
        meta_verify_token=VERIFY_TOKEN, ai_provider="gemini",
        ai_model="gemini-2.5-flash", ai_api_key=None, ai_base_url=None)
    broken_client = TestClient(create_app(settings, channel=broken, ai=None))

    send(broken_client, "what are your prices?")

    row = db.query_one("SELECT processed_at FROM messages WHERE direction = 'IN'")
    assert row["processed_at"] is None, "an unsent reply must not be marked done"

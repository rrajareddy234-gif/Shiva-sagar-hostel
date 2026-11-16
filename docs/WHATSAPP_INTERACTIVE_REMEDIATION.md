# WhatsApp interactive response remediation

## Implemented in this source snapshot

`core/dispatcher.py::_reply` now creates one outbound outbox item when a valid
interactive `Prompt` is present. The answer text and prompt question are joined
into the interactive message body, and the prompt's buttons/list rows remain
attached. It no longer queues a text answer followed by a second interactive
prompt. An interactive send failure does not trigger a plain-text fallback.

This preserves the existing outbox idempotency key and inbound completion
transaction: one inbound message maps to one logical guest response item.

## Update: interactive-only completed for guests (2026-11-06)

Guest conversations are now interactive-only end to end. One incoming guest
message produces exactly one interactive WhatsApp message:

- `core/dispatcher.py::_reply` attaches `router.terminal_prompt()` to any guest
  reply that has no choices of its own -- an informational answer, an
  acknowledgement, an error, or an unsupported message type. The terminal
  actions (Book a Room, Check Availability, Rooms & Prices, Location &
  Directions, Talk to Staff) are a WhatsApp list, so the answer and the actions
  travel in one interactive message. A guest never receives a bare text reply.
- The owner and staff are still answered in plain text. The rule is about guest
  conversations, and the owner needs to be able to read command output and text
  "resume replies" without a menu.
- A Flow (the calendar) is still its own single message, and a file (an owner
  export) is its own single document message; neither is given terminal actions.

`tests/test_interactive_only.py` drives real inbound messages over the webhook
and asserts, for a greeting, an informational answer, an acknowledgement and an
unsupported media type: exactly one interactive send, no text send attempted, and
an interactive body within Meta's `MAX_INTERACTIVE_BODY` cap.

## Not yet implemented

The Flow described below is wired in source, but it has not been validated
against a real Meta test number: no Flow has been published, so the
`flow_id` / `screen` values and the screen's response field names are still
assumptions. Publish the Flow and test it on a Meta test number before treating
the calendar as production-ready.

The broader interactive-only requirement is now complete for guest
conversations; see the update above. Outcomes that do not supply their own
`Prompt` are given the terminal actions rather than a text message.

## Verification limitation

`python -m compileall -q ai api core tests` passed in the review environment.
The pytest suite could not be started because `psycopg` is not installed and
PostgreSQL is not available in this environment. Ruff is also not installed.

## Update: Flow and date steps implemented (2026-11-06)

The calendar Flow is now wired end to end, and the date question is
interactive in every mode:

- `core/config.py` reads `WHATSAPP_FLOW_ID` and `WHATSAPP_FLOW_CALENDAR_SCREEN`.
- `core/booking_flow.py` offers a `Flow` for the check-in / check-out question
  when both are set (`_offer_flow`), and folds a submitted range back into the
  booking (`apply_flow_dates`). With either setting blank the quick-reply date
  buttons are used exactly as before, so nothing changes until a Flow is
  published.
- `core/dispatcher.py` recognises an `nfm_reply` interactive on the webhook
  (`flow_response_data` / `flow_date_range` in `core/channel/adapter.py`) and
  answers it with the next question. A submission is one inbound message and
  gets one interactive reply.
- The Flow config is threaded through `core/handler.py` and `core/router.py` so
  both the typed "book" intent and the "Book a Room" menu tap offer the Flow.

The Flow is its own single message; the guest's answers come back on the
webhook, not as text. When the Flow is not published the date steps fall back
to the single-interactive quick-reply buttons, so no outcome splits a text
answer from a prompt.

To publish the Flow, set `WHATSAPP_FLOW_ID` and `WHATSAPP_FLOW_CALENDAR_SCREEN`
in `.env` after creating the Flow in Meta Business Manager and adding the
calendar screen. The screen must return the chosen range as `check_in` /
`check_out` ISO dates (alternate names in `core/channel/adapter.py`
`FLOW_CHECK_IN_KEYS` / `FLOW_CHECK_OUT_KEYS` are also accepted). Until a real
Meta test number is used, this is verified by `tests/test_whatsapp_flow.py`
against the fake channel only.

### Verification (2026-11-06)

`python -m pytest` (real PostgreSQL 16): **897 passed, 2 skipped**. `python -m
ruff check .`: all checks passed. `tests/test_whatsapp_flow.py` covers Flow
parsing, Flow construction, the submission round-trip, the invalid-range guard,
and the interactive-only "one inbound, one reply, never a bare text" contract.
`tests/test_interactive_only.py` extends that contract to the outcomes that have
no question of their own: a greeting, an informational answer, an
acknowledgement and an unsupported media type each produce exactly one
interactive message with no text send attempted.

### Update: notices and the document fallback (2026-11-06)

The interactive-only contract now covers every guest-facing send, not only the
router's direct replies:

- Booking notices to the guest -- accepted, confirmed, rejected, and the
  payment-screenshot acknowledgement -- are rendered as one interactive message
  (`core/booking_notices.py::_tell_guest`,
  `core/outbound_worker.py::_materialize_booking_notice`). The notice text and
  the terminal actions travel in the same message.
- The document-send failure fallback (`core/outbound_worker.py`) tells a guest
  interactively; the owner, who is not a guest, still gets plain text.
- `compose_interactive_body` in `core/channel/adapter.py` is the one place that
  joins an answer and its question and shortens the answer at a line boundary to
  stay within `MAX_INTERACTIVE_BODY`. The dispatcher, booking notices and the
  outbound worker all use it, so the cap is enforced in one place.
- `core/channel/meta_cloud.py::send_flow` now honours the Flow's `mode`
  (`navigate` or `data_exchange`) and only sends `flow_action_payload` for
  `navigate`, matching Meta's Flow message schema.

`tests/test_interactive_only.py` and `tests/test_whatsapp_flow.py` cover these
paths, including a cancelled Flow, a malformed `response_json`, and a duplicate
Flow submission, each of which still yields exactly one interactive reply.

### Verification (2026-11-06, after the notice and fallback work)

`python -m pytest` (real PostgreSQL 16): **911 passed, 2 skipped**. `python -m
ruff check .`: all checks passed.

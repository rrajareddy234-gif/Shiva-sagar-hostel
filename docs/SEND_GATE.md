# The outbound send gate

Two switches stand between the app and a real guest's phone. This note explains
what they are, why there are two, and the safe way to turn sending on for a
development test.

## The two keys

| Switch | Where | Default | Governs |
|---|---|---|---|
| `SEND_ENABLED` | environment only | **false** | every outbound WhatsApp message |
| `AUTO_REPLY_ENABLED` + the pause row | `.env` default, live row in the `settings` table | enabled | guest replies |

A **guest** message goes out only when **both** keys say yes: `SEND_ENABLED=true`
*and* the owner's pause is resumed. Either one off means the guest is not
messaged.

An **owner-facing notification** (a booking request, an AI outage alert, a
scheduled report) is governed separately by `OWNER_NOTIFY_ENABLED`, which
defaults to **true**: the owner must hear about their business even while guests
are paused. It still requires `SEND_ENABLED`, because that master switch covers
every message.

## Why the default is off

The default is off because the failure it prevents is expensive and one-way. A
development box, a fresh clone, or a test run that holds real Meta credentials
would otherwise be able to message real guests the moment an inbound webhook
arrived. A missing or misspelled value is treated as off, so a typo cannot arm
the system by accident. Only the literal words `true`, `yes`, `on` or `1` turn it
on.

Nothing inside the app or the database can raise `SEND_ENABLED`. The dashboard
**Resume** button and the owner's `resume replies` command lift only the pause;
they cannot change `SEND_ENABLED`. So a resume on a deployment that is not
allowed to send leaves guest sending off, and the dashboard says so plainly
rather than promising a reply that will not go out.

## Where it is enforced

The decision lives in `core/send_policy.py` and is applied in the delivery layer,
not only in the dispatcher:

* `core/outbound_worker.run_batch` evaluates the gate once per batch and passes
  it into the claim query, so the inline drain, the scheduled worker, booking
  notices, payment acknowledgements and retry processing all obey it. A second
  check inside the loop holds any message that somehow slips through.
* `core/outbox.claim_next` filters by audience in SQL: a guest row is eligible
  only while guest sending is allowed, an owner/staff row only while owner
  notifications are allowed. A blocked row is **never selected**.
* `core/dispatcher.auto_replies_go_out` checks the same gate early, so a disabled
  deployment does not even build a guest reply.

## What happens to a blocked message

Nothing is lost and nothing is marked sent. A blocked row stays `pending` in
`outbound_messages`, its attempt is not counted, and the claim (if one was taken)
is released with `outbox.release_claim`. Because `claim_next` will not select a
blocked row, there is **no retry loop and no busy work**: the worker simply finds
nothing to send and returns. When the switches are turned on, the row is picked
up on the next batch and sent exactly once.

Inbound handling is untouched: every webhook is still stored, deduped and
processed, bookings are still created, and the owner still hears about a booking
request even while guest sending is off.

## Turning it on for a development test

Never test against a real guest's number. Use a dedicated Meta test number.

1. **Get a test number.** In the Meta Developer app, add a *test* recipient under
   WhatsApp → API Setup → "To" field, or register a separate test phone number.
   The test recipient receives messages from the app's test number only.
2. **Point the app at the test number's credentials.** In the development `.env`,
   set `META_PHONE_NUMBER_ID`, `META_ACCESS_TOKEN` and `META_APP_SECRET` to the
   test app's values -- not the production ones. Do not copy production secrets
   into a development file.
3. **Arm the gate for this run only.** Add `SEND_ENABLED=true` to the development
   `.env`, and confirm the pause is resumed (`AUTO_REPLY_ENABLED=true`, or
   `resume replies`).
4. **Confirm the state.** `GET /health` reports `"sending"`. It reads
   `sending on` when armed, and `blocked: SEND_ENABLED is not true` or
   `guests blocked: replies paused` otherwise.
5. **Send one message from the test recipient's phone** and confirm exactly one
   interactive reply comes back.
6. **Disarm when finished.** Set `SEND_ENABLED=false` (or remove the line) and
   restart. Leaving it on in a shared development environment is the exact risk
   this gate exists to remove.

Production is the same procedure with the production credentials and a real
guest number -- and it should be a deliberate, reviewed change, not a side effect
of starting the app.

## Seeing the gate's state

`GET /health` includes a `sending` line, for example:

```json
{"status":"ok","database":"ok","channel":"meta_cloud",
 "auto_reply":"paused","sending":"blocked: SEND_ENABLED is not true",
 "owner_email":"off"}
```

The line never contains a phone number or a credential.

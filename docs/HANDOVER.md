# Handover

What has been built, what was reused, what was repaired, what was tested, what is
still missing, and what I need from you. Written in plain English.

## 1. What exists now

A working system, built and tested, **not yet connected to your WhatsApp number**.

It does five things:

1. **Answers guests on WhatsApp.** Common questions — prices, availability,
   facilities, greeting — are answered by plain code and SQL, with no AI call at
   all. Only free text the code does not recognise reaches an AI model.
2. **Keeps track of every bed.** All 68 beds (48 cots in seven rooms plus 20
   dormitory mat spaces) are individual records with their own status. A bed is
   never promised twice: the database itself refuses overlapping bookings.
3. **Takes booking requests, never confirmations.** The system can create a
   request and hold a bed temporarily. Only you can approve it, from your own
   number, and you must confirm with "YES" before anything changes.
4. **Answers your questions on WhatsApp.** "How many vacancies today", "who has
   not paid", "show pending booking requests", "how much did we collect this
   month" and others, all from the live database.
5. **Keeps money honest.** A payment screenshot is never treated as proof. Money
   is recorded as unverified until you confirm it, and only verified money counts
   towards balances and revenue.

### Size of the thing

- 345 tests, all passing, against a real PostgreSQL database.
- 15 commits, every change reversible with git.
- About 3,700 lines of application code and 1,500 lines of documentation.

## 2. What was reused

Honest answer: **almost nothing, because almost nothing existed.**

| Thing from your brief | What I found |
|---|---|
| n8n workflows | No n8n instance, no exports, no credentials anywhere. Could not inspect or reuse. |
| Meta Developer App and Cloud API config | No credentials. Could not verify the webhook, the GET handshake or the POST handling. |
| "Peach for WhatsApp Business" | No such product found. See question 1 below. |
| GitHub repo `shiva-sagar-hostel` | Exists, public, **empty**. Nothing to reuse. |
| `macherla-stay.preview.emergentagent.com` | Alive but dead: a "Loading..." shell with no API behind it. `/api/health` returns 404. Nothing to reuse. |

So this is a fresh build on a clean foundation, not a repair. I did **not**
delete or overwrite anything of yours, because I could not reach any of it.

What I did build fresh, and you can reuse in future projects: the room and bed
model, the availability engine, the booking rules, the pricing engine, the
payment ledger, and the WhatsApp and AI adapters.

## 3. What was repaired

Two real defects, both found by testing rather than by reading the code:

1. **"What are your prices?" got no price.** The pattern that recognises a price
   question only accepted the singular word "price". This is one of the most
   common things a guest sends, and it was falling through to the AI — which,
   before the fix below, had no way to look up a price and would have had to
   guess. Fixed in `core/router.py`, with the plural now covered.

2. **The daily report crashed.** The development database had been built from an
   older version of the schema and was missing a column, so `/jobs/daily-report`
   returned a 500 error. Re-applying `db/schema.sql` fixed it. The schema is
   written to be re-applied safely, so this is now a one-command fix and is
   documented in `AGENTS.md`.

There was also a design gap I closed rather than repaired: **the AI had no way to
check a fact.** It could answer free text but could not look anything up, which
made the rule "never invent availability or prices" impossible to keep. It now
has tools — availability, prices, facilities, an exact quote, the guest's own
bookings — and every fact it states comes from one. The only tool that writes
anything can create a booking *request*, which you still have to approve.

## 4. What was tested

345 automated tests, all passing. Full detail in `docs/TEST_PLAN.md`.

| Area | Tests | Notes |
|---|---:|---|
| Owner commands | 34 | including staged confirmation and cancellation |
| AI tools | 33 | including a customer trying to call an owner-only tool |
| Booking rules | 29 | overlapping, holds, expiry, approval, rejection, override |
| Router (no AI) | 28 | |
| Webhook | 25 | signature, dedupe, malformed input |
| AI layer | 25 | provider switching, cost estimation, failure handling |
| Availability | 24 | same-day turnaround, cleaning, maintenance |
| Payments | 21 | partial, duplicate reference, unverified, refunds |
| Scheduled jobs | 21 | |
| WhatsApp channel | 18 | |
| Security | 17 | injection, permissions, secret leakage |
| Pricing | 17 | AC/non-AC, food, dormitory, hourly |
| Inbox and enquiries | 15 | |
| **Full guest journey** | 6 | end to end, over HTTP, real database |

The end-to-end test is the one that matters most. It drives one guest from their
first message through booking request, owner approval, an advance payment,
check-in, checkout, and the bed going to cleaning before it becomes bookable
again. Every step goes over the real webhook and touches the real database. Only
the WhatsApp send is faked.

I also ran the server for real and posted signed webhook messages to it. That
test reached Meta's servers and was correctly rejected for using a fake token,
which confirms the outbound path is genuine rather than a stub.

### What was NOT tested

I will not claim these passed, because they did not run:

- **Anything involving real WhatsApp.** No credentials, so no real message has
  been sent or received.
- **Anything involving a real AI model.** No API key, so no live model call. The
  AI layer is tested against scripted fake models, which tests the wiring and the
  permission rules but not the quality of any model's answers.
- **A real backup and restore.** The scripts exist and are careful, but they have
  not been run against a live server because there is no live server.
- **Two guests requesting the same bed at the same instant.** The database
  constraint that prevents this is in place and tested at the schema level, but
  a true concurrent race has not been run under load.
- **n8n.** The six workflows are generated and validated for structure, but no
  n8n instance has ever imported them.

## 5. Remaining limitations

- **Nothing is live.** No real customer has been messaged. The system has never
  spoken to your WhatsApp number.
- **Your room list is still unconfirmed.** The rooms in `db/seed.sql` are the
  historic list, flagged unconfirmed; they must be checked before a bed is
  promised. The **rates are confirmed** and live. Every room type is offered
  with and without food, each with a night and a day rate: AC Rs 350/450 with
  food and Rs 300/350 without; Non-AC Rs 250/350 and Rs 200/250; dormitory
  Rs 200/300 and Rs 150/200. Plus extras. **Monthly (PG)** plans are separate:
  AC Rs 8,000/11,000 with food and Rs 5,000/7,000 without, Non-AC Rs 5,500 and
  Rs 4,000, dormitory Rs 4,500 and Rs 3,500. There is no concession: the same
  rate applies to every guest.
- **Automatic follow-up is switched off**, deliberately. Following up more than
  24 hours after a guest writes needs a Meta-approved message template, and none
  exists. Until one does, follow-up is manual.
- **Partner referrals are modelled but not built.** The database tables exist;
  the WhatsApp flow does not.
- **Media messages** (photos, voice notes) get a polite "please send text"
  reply rather than being understood.
- **The AI quality is unproven** until a real model is connected and used.

## 6. Decisions made

### WhatsApp channel: official Meta Cloud API. Peach is dropped.

**Decided by you on 2026-11-03.** Peach is not used. The system talks to Meta
directly.

That is the cheapest and simplest option, and it is what the code was already
built and tested against. Two reasons it is right:

- **Cost.** Peach's cheapest useful plan is about ₹6,350 a month. That alone is
  three times your entire budget, for a layer this system does not need.
- **Duplicates.** A WhatsApp number feeds exactly one webhook. Putting Peach and
  this system on the same number means Meta delivers every message to both, and
  the guest gets two replies. Your brief forbids exactly that.

The Peach option is still in the code as an unused stub, so if you ever change
your mind it is a one-line setting, not a rebuild. Nothing of yours was
disconnected or deleted.

Full reasoning: `docs/ADR-001-whatsapp-channel.md`.

## 7. What I need from you

One question at a time, as you asked. Peach is settled, so the next one is:

### Question 1: your Meta WhatsApp Cloud API credentials

Please open `developers.facebook.com`, go to your app, and send me:

- **App ID**
- **App Secret** (WhatsApp → API Setup → or Settings → Basic)
- **WhatsApp Business Account ID**
- **Phone Number ID**
- A **permanent** access token from a System User (not the 24-hour test token)

If you also have a phone number you want to use for this — either a new one, or
your current number moved off Peach — tell me which.

Send them one at a time or together, whichever is easier. **Do not paste them
into a chat message if you would rather not** — I can tell you where to put them
in a file on the server instead.

After that I will ask about n8n, then your confirmed room list and rates.

## 8. Monthly cost

Detailed in `docs/COST_REPORT.md`. Summary, using real November 2026 prices:

| Usage | Hosting | AI | WhatsApp | **Total** |
|---|---:|---:|---:|---:|
| Low (≈300 messages) | ₹470 | ₹6 | ₹0 | **₹476** |
| Medium (≈1,500) | ₹520 | ₹29 | ₹115 | **₹664** |
| High (≈5,000) | ₹620 | ₹98 | ₹633 | **₹1,351** |

All inside your ₹500–₹2,000 target. The thing to watch is WhatsApp message
volume, not AI: from 1 October 2026 free-form replies are billable at ₹0.115 each
after the first 1,000 a month. AI costs a few rupees a month because most
questions never reach a model.

Nothing has been purchased. No subscription has been started.

## 9. How to change the AI model

One setting in `.env`, then restart the app:

```
AI_PROVIDER=gemini
AI_MODEL=gemini-flash-lite-latest
AI_API_KEY=...
```

Supported out of the box: Gemini, OpenAI, OpenRouter, Groq, and any
OpenAI-compatible endpoint including a local model via `AI_BASE_URL`. No code
changes, no workflow changes. Full instructions in `docs/OPERATIONS.md`.

## 10. How to back up and restore

```bash
./deploy/backup.sh      # nightly via cron; keeps 14 days, checks the dump
./deploy/restore.sh FILE  # asks for confirmation, backs up first
```

The backup script refuses to report success if the dump is empty, corrupt, or
missing the bookings table — a bad backup that looks like a good one is worse
than none. **Neither script has been run against a live server yet.** Rehearse
the restore before you need it.

## 11. What I recommend next

1. You send the Meta credentials (question 1 above).
2. You confirm the room list and the rates. Until then the system quotes nothing
   to a customer.
3. You decide on a server (about ₹400–600 a month) and a domain name.
4. I connect the real WhatsApp number and run the go-live checklist in
   `docs/OPERATIONS.md`, testing with your own phone before any customer.
5. Only then do we turn on real customer messages.

Until step 1 lands, the system stays exactly as it is: complete, tested, and
silent. Nothing is live and nothing can message a customer by accident.

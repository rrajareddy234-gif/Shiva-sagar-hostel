# AGENTS.md — Sagar PG AI Manager

Repository-specific knowledge for future sessions. Keep this current.

## Project
AI manager for Sagar PG Hostel (Macherla, Palnadu district, Andhra Pradesh).
Operated through WhatsApp. Owner keeps final approval over all bookings.
Design rule: deterministic core (code + SQL) for availability, pricing, payments,
booking rules; AI only for understanding free text and drafting replies.

## Environment facts (verified 2026-11-03)
- Python 3.13, Node 24, Docker 29 (works via `sudo docker`; the user is in the
  `docker` group but the socket is root-owned, so plain `docker` gives permission
  denied — always use `sudo docker`).
- No `psql` or `sqlite3` CLI on the host. Use the dev Postgres container instead:
  `sudo docker exec -e PGPASSWORD=devpass sagarpg-pg psql -U sagar -d sagarpg`.
- Dev Postgres container name: `sagarpg-pg` (postgres:16-alpine, user `sagar`,
  db `sagarpg`, password `devpass`, port 5432). Recreate with:
  `sudo docker run -d --name sagarpg-pg -e POSTGRES_PASSWORD=devpass -e POSTGRES_USER=sagar -e POSTGRES_DB=sagarpg -p 5432:5432 postgres:16-alpine`
- Host shell has a buggy `PROMPT_COMMAND` that prints a JSON blob on start; it is
  cosmetic. Run ONE command per call, or chain with `&&`/`;`. Heredocs with a
  leading `cat > file <<EOF` may not execute; prefer writing files with the file
  editor or `printf`.
- Public sandbox URL is ephemeral (`RUNTIME_URL`); not a production host. Ports
  12000/12001 are published.

## GitHub
- Authenticated as `rrajareddy234-gif`.
- The token is now WRITE-CAPABLE. Re-verified 2026-11-05: creating and deleting
  a test ref returned `201` / `204`, and `git push` works. The earlier note that
  it was read-only no longer holds.
- Target repo: `rrajareddy234-gif/Shiva-sagar-hostel` (public). Work happens on
  branch `fix/remove-unused-imports`, PR #1 into `main`.

## Not accessible
- No n8n instance, no workflow exports, no n8n credentials anywhere.
- No Meta WhatsApp Cloud API credentials, no webhook registration.
- No OpenAI / Anthropic / Google API keys belonging to the owner.
- The Emergent preview `macherla-stay.preview.emergentagent.com` is a dead
  "Loading..." shell with no API behind it.

## Conventions
- Never commit secrets. `.env`, keys, dumps and `backups/` are git-ignored.
- Tests use dummy data only. No real customer messaging, no live payment changes,
  no production switch without explicit owner approval.
- Schema changes go in `db/schema.sql`; it is written to be re-runnable
  (`IF NOT EXISTS`, and constraints dropped-then-added).

## Running the tests
```bash
python -m pytest          # 579 tests; needs the sagarpg-pg container running
python -m ruff check .    # lint; config in ruff.toml
```
CI (`.github/workflows/ci.yml`) runs both on every push and pull request: a
lint job, and a test job against a `postgres:16-alpine` service container with
the same credentials as the dev container.
Tests create and drop a throwaway `sagarpg_test` database, so the `sagarpg`
admin database must exist. The admin DSN is `TEST_ADMIN_DSN`
(default `postgresql://sagar:devpass@localhost:5432/sagarpg`).

If a test fails with a missing column, the dev database was built from an older
schema. `db/schema.sql` is idempotent, so re-apply it:
```bash
sudo docker exec -i sagarpg-pg psql -U sagar -d sagarpg -q < db/schema.sql
```
This happened once already (`bookings.charges_total` was missing, so
`/jobs/daily-report` returned 500).

To exercise the real server rather than TestClient:
```bash
DATABASE_URL=postgresql://sagar:devpass@localhost:5432/sagarpg \
  python -m uvicorn api.main:app --port 8077
```
The dev database contains leftover rows from earlier manual runs
(`SPG-T-0001`..`SPG-T-0005`). They are dummy records, not real data.

## Architecture
- The WhatsApp provider is a config value (`CHANNEL_PROVIDER`). Business logic
  imports `core.channel.adapter.ChannelAdapter`, never a concrete provider.
  `core/channel/registry.py` selects it. See `docs/ADR-001-whatsapp-channel.md`.
- The AI provider will follow the same shape: an adapter chosen by config, with
  prompts stored separately from business logic.
- Availability, prices, dates and booking rules are deterministic code and SQL.
  No AI model decides whether a bed is free or what something costs.

## Core module notes
- `core/db.py` — `Database.transaction()` commits on success and rolls back on
  error, translating constraint violations into domain errors. `query_one` and
  `query_all` do NOT commit; never use them for an INSERT or UPDATE.
- `core/availability.py` — availability is derived from bookings and holds, not
  from `beds.status`. `beds.status` is an operational label kept in sync by
  `sync_bed_status`; a stale label can never cause a double-booking.
- `core/booking.py` — a request is never auto-confirmed. `approve` re-checks the
  bed inside a `FOR UPDATE` transaction. The `override` flag only bypasses an
  operational block such as CLEANING or MAINTENANCE; it can never defeat the
  `bookings_no_overlap` constraint.
- `core/payments.py` — a screenshot is not proof. Money is unverified until
  `verify_payment` is called by an authorised person or a trusted gateway, and
  only verified money counts towards balances and revenue.
- `core/inbox.py` — dedupe on `messages.wa_message_id` UNIQUE. A duplicate
  webhook delivery returns None and must skip all further processing.
- `core/tools.py` — the tools the AI may call. Read tools answer from SQL; the
  one write tool creates a booking *request* and cannot confirm anything. Owner
  tools are filtered out of `specs_for(role)` and refused again in `execute`.
- `core/handler.py` — order is owner command, then deterministic router, then
  the AI with tools. The AI loop is capped at `MAX_TOOL_ROUNDS`; a model that
  keeps calling tools falls back instead of spinning.
- Intervals are half-open, `[start, end)`, so same-day checkout and arrival do
  not conflict. Money is `Decimal`, never float.

## Test-writing notes
- `tests/conftest.py` derives `TABLES` from `db/schema.sql` at import time, so
  the truncate list can never drift from the schema. Do not replace it with a
  hand-kept list: a table missing from the truncate leaks rows between tests and
  the failure looks unrelated to the change that caused it.
- Webhook tests must give each message a **unique** id. Reusing one is correctly
  treated as a duplicate delivery and silently dropped, which looks like a bug
  and is not.
- The app normalises phone numbers to a leading `+`. Meta delivers them without
  one, so a test that sends `919...` must assert on `+919...`.
- After checkout a bed is `CLEANING`, not `AVAILABLE`. That is deliberate; the
  owner must send "bed X is ready" before it is bookable again.
- Router patterns must accept plurals. `_PRICE` originally matched only
  "price", so "what are your prices?" fell through to the AI.
- Every room type offers **both** food options, with a night and a day rate
  for each (twelve figures). So `food_included` can never be defaulted: any
  quote or booking request that omits it is refused with "offers both food
  options". Tests and callers must pass it explicitly.
- `db/seed.sql` upserts rate plans on the slot (room type, food option, stay
  mode) rather than skipping existing names, so re-running it applies a rate
  change instead of leaving a stale price behind.
- Interactive messages (tappable buttons/lists) carry an **id** as well as the
  title the guest reads. `InboundMessage.button_id` holds that id, and the
  router answers on it before looking at the text: a relabelled or translated
  title must not break a flow. Button ids are namespaced strings, e.g.
  `menu:price`, `price:room:AC` and `price:food:AC:1`. The `menu:` prefix is
  matched before the `price:` prefix.
- A greeting offers the same taps as the price question: "hi" (or a bare
  "menu"/"help"/"options"/"start") sends `[Prices] [Availability] [Facilities]`.
  Only a *bare* "help" asks for the menu - "help me book a bed" is a real
  request and is handled on its merits.
- A button never carries an amount. It carries the *choice*; the figure is
  computed from the rate card. This keeps the "never guess a price" rule.
- Interactive sends are best-effort: `dispatcher._reply` tries the prompt, and
  falls back to plain text if it fails or the prompt is invalid. Text is always
  the fallback body, so a channel without buttons still works.
- `Prompt` (core/channel/adapter.py) is buttons (up to 3) or a list (up to 10
  rows). `is_valid()` enforces Meta's limits; titles are clipped when sent.
- Interactive messages are only valid inside the 24-hour service window, like
  text. They need no Meta approval and cost the same as text.
- Monthly (PG) stays are a **separate product** with their own table,
  `monthly_plans`, not a stay mode on `rate_plans`. It carries `stay_mode` like
  `rate_plans`: NIGHT is a night-only arrangement, DAY a full day. Every room
  type is priced explicitly (AC Rs 8,000/11,000 with food, Rs 5,000/7,000
  without; Non-AC Rs 5,500 and Rs 4,000; dormitory Rs 4,500 and Rs 3,500).
  There is **no concession**: the same rate applies to every guest, and anyone
  wanting a lower rate is told to speak to the owner. `plan_kind` still exists
  ('STANDARD'/'CONCESSION') so a concession could be added later, and
  `_load_monthly_plan` prefers an exact room type over `ANY`. Months are whole;
  there is no pro-rating.

## Owner dashboard (built 2026-11-03)
- Read-only web page at `/dashboard`, password-protected. Files:
  `core/dashboard.py` (data), `api/dashboard_page.py` (HTML), `api/dashboard.py`
  (routes), registered in `api/webhook.py`.
- Password from `DASHBOARD_PASSWORD`. There is no fixed default: when it is
  unset a random password is generated for the run and logged, so a public
  deployment never opens the guest table to a guessable password. Login sets an httponly
  cookie `sagar_dash`; `/dashboard/logout` clears it. The login body is parsed
  with `urllib.parse.parse_qs` so `python-multipart` is NOT a dependency.
- Every figure reuses the same core helpers as the WhatsApp chat
  (`availability.availability_summary`, `booking.pending_requests`,
  `payments.*`), so the page and the chat can never disagree.
- Run locally:
  `DATABASE_URL=postgresql://sagar:devpass@localhost:5432/sagarpg DASHBOARD_PASSWORD=sagar python -m uvicorn api.main:app --host 0.0.0.0 --port 12000`
- Still to do: Approve/Reject buttons (phase 2). Owner has NO domain yet; the
  workspace preview URL is temporary. Production needs a server + domain.

## Dashboard phase 2: Approve / Reject (built 2026-11-03)
- Pending bookings now show Approve and Reject buttons. `POST /dashboard/decide`
  with `reference` + `decision` in the body. `core/dashboard.approve_booking` /
  `reject_booking` call the SAME `core.booking.approve`/`reject` as the WhatsApp
  command, so the bed-conflict and bed-not-ready checks still apply, and the
  action is written to `audit_log` with actor `owner-dashboard`.
- A refusal (bed taken while the request waited) redirects back with a clear red
  message; the booking stays REQUESTED.
- Result is shown as a green/red notice via `?notice=...&failed=0|1` on the GET.
- Tests: tests/test_dashboard.py now 22 tests. Full suite 526 passed.

## Live WhatsApp is connected (verified 2026-11-04)
- Meta Cloud API credentials in `.env` are LIVE: bot `sagar-pg-bot`, business
  number +91 83176 05360 ("Sagarpghostel", quality GREEN), WABA
  `1434890208208126`, phone id `1371188006084487`. Webhook points at the
  workspace URL.
- Meta exposes NO customer list: `/contacts` and `/messages` both return
  "#100 nonexisting field". Customer details only arrive through the webhook and
  land in our own DB. Never promise to "fetch" customers from Meta.
- A real customer messaged (+91 86883 97996) and the bot captured it.

## AI model fixed (2026-11-04)
- `AI_MODEL` was `gemini-3.5-flash`, which does not exist -> every free-text
  reply failed with "I could not reach the assistant just now". Fixed to
  `gemini-flash-lite-latest` (then `gemini-flash-lite-latest` after the daily quota ran out).
  Updated in `.env`, `.env.example`, `core/config.py`, `ai/gemini.py`,
  `docs/OPERATIONS.md`, `docs/HANDOVER.md`, `tests/test_webhook.py`.
- Note: the key is on the Gemini FREE tier (about 20 requests/minute). Heavy
  testing trips HTTP 429. A real customer volume may need a paid key.

## Clearing demo data (2026-11-04)
- `scripts/reset_demo_data.py` clears people and money, keeping the hostel's own
  setup (rooms, beds, rate plans, extras, settings) and the record of real
  inbound contact (conversations, messages, enquiries). Dry-run by default;
  `--yes` to apply. Also frees every bed and restarts `booking_ref_seq` at 1.
- Demo data has been cleared: 0 guests/bookings/payments, 68 beds AVAILABLE,
  next booking is SPG-2026-000001. Backup at `backups/` (gitignored).

## Workspace resets wipe the runtime (2026-11-04)
- The workspace was recycled mid-session: installed Python packages, the Docker
  daemon, the postgres container and its volume ALL disappeared. The repo files,
  `.env` and `backups/` survived (they are on the mounted volume).
- Recovery, in order:
  1. `sudo dockerd > /tmp/dockerd.log 2>&1 &` then wait ~8s.
  2. `pip install -r requirements.txt`
  3. `docker compose -f deploy/docker-compose.dev.yml up -d` then wait for healthy
  4. Restore data: `docker exec -i sagarpg-pg psql -U sagar -d sagarpg < backups/<file>.sql`
  5. Re-apply cleanup: `python scripts/reset_demo_data.py --yes`
  6. Start the app detached so it survives the shell:
     `setsid env DATABASE_URL=... DASHBOARD_PASSWORD=... python -m uvicorn api.main:app --host 0.0.0.0 --port 12000 &`
- The public link is a preview tunnel to port 12000. If nothing listens on 12000,
  the link looks "broken" — check the server first, not the code.

## How approval works, and why no alert arrives (2026-11-04)
- Every booking request is created with status REQUESTED. Nothing confirms
  itself; the owner approves. Verified end to end: guest request ->
  owner types "pending" -> lists it -> owner types "approve SPG-..." ->
  bot asks "Reply YES to confirm" -> owner types "yes" -> status CONFIRMED.
- OWNER_PHONE in `.env` is +918688397996 — the same number that messaged the
  bot. `security.role_for` resolves it to role "owner", so owner commands work
  from that number only.
- The automatic "a booking is waiting" alert comes from the n8n workflow
  `n8n/pending-booking-requests.json` (Schedule -> fetch -> WhatsApp owner).
  n8n is NOT running in this workspace, so no alert arrives. Approval itself
  does not depend on n8n.
- Fastest approval path: the dashboard Approve button (one tap, no YES step).
  WhatsApp path: "approve SPG-..." then "yes".
- `scripts/start_app.sh` brings Docker, packages, DB (restoring the newest
  backup if the volume was lost) and the app back up in one command.

## Gemini free-tier daily cap (found 2026-11-04)
- The free tier allows only **20 generateContent requests per DAY per model**
  (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). Once spent, every
  free-text reply fails with HTTP 429 / RESOURCE_EXHAUSTED and the bot falls
  back to "I'll check with the owner", for up to ~17 hours (`retryDelay`
  ~61061s). This is what made the bot look broken while the owner tested it.
- The cap is **per model**, so switching `AI_MODEL` moves to a fresh bucket.
  Switched to `gemini-flash-lite-latest`, which answered correctly in testing
  (real rates and availability). Updated `.env`, `.env.example`,
  `core/config.py`, `ai/gemini.py`, `docs/OPERATIONS.md`, `docs/HANDOVER.md`,
  `tests/test_webhook.py`.
- 20/day is far too low for real guests. Before go-live the owner needs a paid
  Gemini key (or another provider) — see the AI_PROVIDER setting.
- The fallback message never tells the owner something is broken, so a silent
  AI outage looks like "the bot ignores me". Worth surfacing.

## AI-lockout alert (built 2026-11-04)
- `core/alerts.py` tells the owner over WhatsApp when the AI stops answering,
  and again when it recovers. State is one row in `settings` under key
  `ai_alert` (`down`/`up`), so the owner is told ONCE per outage, not once per
  guest message, and the state survives a restart.
- Wired in `core/dispatcher.py`: `outcome.escalate` -> note_failure,
  `outcome.handled_by == "ai"` -> note_recovery. Both are best-effort; a failed
  alert never stops the guest's reply.
- `outcome.escalate` was already set by the handler but had no consumer, so the
  owner was never actually alerted before this. That was the real gap.
- 11 tests in `tests/test_alerts.py` (once-per-outage, recovery, guest reply
  still sent, broken channel never raises, no OWNER_PHONE is safe).
- Owner can check spend any time: message "ai cost".

## Extras prices settled + a silent-write bug (2026-11-04)
Owner confirmed the extras: Bedsheet Rs 50 each, Pillow Rs 50 each,
Locker Rs 50 per month, **Towel Rs 100 each**.

CORRECTION to an earlier note here: the live database had Towel at Rs 50 while
the committed branch already had it at Rs 100. The live DB had simply been
seeded from an older revision, so this was drift between the running database
and the committed code, NOT an underquoting bug. The real price was always
Rs 100 in the branch. Locker was genuinely wrong everywhere (Rs 30 -> Rs 50). `docs/RATE_CARD.md`, `docs/OPERATIONS.md` and
`docs/HOW_TO_MAKE_IT_LIVE.md` all said Locker Rs 30 and "New towel"; corrected.

**Bug found:** `pricing.set_extra()` wrote with `db.query_one()`, which opens a
connection and closes it WITHOUT committing. Every extras price change was
silently discarded -- the call returned an id and looked like it worked.
Rewritten to use `db.transaction()`. Regression tests added in
`tests/test_pricing.py` (persist across connections, no duplicate row, quote
uses the new price, negative price and bad unit refused). This was the only
write-via-query_one in the codebase.

## PR #1 description: DONE (2026-11-04)
Rewritten and published. The extras table now reads Locker Rs 50 / Towel Rs 100
and drops the "New" prefix. Verified against the committed branch, the live DB
and the published body -- all three agree.

Also corrected here: "412 tests passing" was RIGHT all along, not stale. The
committed branch collects exactly 412. The 542 figure is the working tree,
which carries extra uncommitted suites (test_alerts, test_billing, test_export,
test_dashboard, test_memory, test_handler_memory, test_staff_and_billing_flow)
and ~20 uncommitted commits. Those extra suites are NOT in the PR, so the
description must quote 412. Do not "fix" it to 542.

Committing an extras price change: the write must go through `db.transaction()`.
`db.query_one()` opens a connection and closes it WITHOUT committing, so a write
through it is silently discarded (this is what made set_extra look like it
worked). `db.execute()` also commits.

The live dev DB was seeded from an older revision, which is why its extras
disagreed with the committed code. If the DB and the branch disagree, check
which revision seeded the DB before calling it a code bug.

## Watchdog: the app cannot report its own death (2026-11-04)
`scripts/watchdog.py` watches `/health` every 30s from OUTSIDE the app, because
a stopped process runs no code. On a failure it records the outage, restarts via
`scripts/start_app.sh`, and sends the owner one WhatsApp message; one more when
the app answers again. Once per outage, never once per check.

`scripts/watch_app.sh` starts the app and the watchdog together, both detached.
State lives in `/tmp/sagarpg-watchdog/`: `events.jsonl` (one JSON line per event),
`outages.log` (plain down/up lines), `state.json` (current status). Read it by
timestamp with `python scripts/failures.py --at 14:32` or `--outages`.

Notification reuses the app's own channel + `inbox.record_outbound`, so owner
messages are recorded like every other one. `restart_app` strips lines
containing "password" from the start script's output, because start_app.sh
prints the generated dashboard password.

This all dies when the workspace resets -- it is inside the workspace. The
durable fix is a real server with `restart: unless-stopped` (already set in
deploy/docker-compose.yml) plus an external uptime monitor.

## The dashboard password must never have a fixed default (2026-11-04)
`core/dashboard.py` previously fell back to the literal password "sagar". The
repo is PUBLIC, so that password was world-readable, and the page shows every
guest's name, phone and booking. An unset `DASHBOARD_PASSWORD` now generates a
random one per run and logs it. Never reintroduce a fixed default here.

## FAQ layer: the cheapest AI call is the one never made (2026-11-04)
Most guest messages are rewordings of a few dozen questions the owner has
answered many times. `core/faq.py` stores those answers once and matches new
questions by trigram similarity, inside PostgreSQL, with no model and no API.
It runs on the database already in use, so there is nothing extra to host.

There is no pretrained "hostel" model to download; this is the practical
substitute. Self-hosting a free LLM was rejected: it costs a server to run and
is worse at the free-text job than the current model, so it would cost more to
save less.

Order of answering in `router.route`: exact rules first (price, availability,
buttons), then the FAQ, then the AI. The FAQ is also tried before the generic
facilities list, so an owner-authored answer beats the boilerplate. Unrelated
questions still reach the AI -- a confident wrong answer is worse than a call.

- `faq` holds the question, the owner's answer, and `aliases` for the phrasings
  guests actually use ("khana", "when can i check in"). A trigram cannot know
  these, so they are stored explicitly.
- `faq_hits` records every ask and whether it was answered. The misses are the
  worklist: `python scripts/faq_admin.py misses`, or the owner asks the bot
  "what is on the faq list". Each entry added is one fewer paid call.
- Threshold `DEFAULT_THRESHOLD = 0.55` in `core/faq.py` is calibrated by
  `tests/test_faq.py`: rewordings score well above, near-misses well below.
- Never store a price here. A stored figure goes stale and would contradict the
  pricing engine, which reads the rate card. Seeded entries are facts only.

Two bugs found while building this, both worth remembering:
- Writing through `db.query_one` silently loses the write: that helper closes
  its connection without committing. Use `db.transaction()` for writes. This is
  the same trap noted under "Extras prices settled".
- `tests/conftest.py` `TABLES` is now derived from `db/schema.sql` at import
  time, so it cannot drift again. A hand-kept list had gone stale twice and let
  rows leak between tests; do not go back to one.

## Backups must be proven, not just taken (2026-11-05)
`deploy/backup.sh` wrote a dump and reported success. Nothing proved the dump
could be restored, so "we have backups" was an assumption. Two real defects
were found while fixing that:

- **`backup.sh` could delete a good backup.** It piped `zcat` into `grep -q`.
  Under `set -o pipefail`, grep exits on first match, `zcat` takes SIGPIPE, the
  pipeline reports failure, and the script deleted a perfectly valid file. It
  now decompresses to a temp file and greps the file. `tests/test_deploy_scripts.py`
  guards against this pattern returning.
- **`docker exec` in `_pg.sh` needed `-i`.** Without it stdin is not attached,
  psql exits immediately, and any pipe into it dies with SIGPIPE (exit 141).
  Compose uses `exec -T`; the plain-container branch now uses `exec -i`.

New: `deploy/_pg.sh` (shared DB access across compose / container / local),
`deploy/restore_drill.sh` (restores the newest dump into a scratch database,
compares row counts, never touches live data, exits non-zero on failure).
Verified end to end on 2026-11-05: drill passed on a good dump and failed
correctly on a corrupt one. `docs/OPERATIONS.md` has the cron lines.

## Chat simulator, and the bug it found (2026-11-05)
`scripts/chat.py` drives the real dispatch path from the terminal — same
router, same pricing engine, same booking flow — printing replies instead of
sending them. It builds a throwaway database `sagarpg_demo` from `db/schema.sql`
and `db/seed.sql`, so it can never touch real data. It is the fastest way to
exercise a guest conversation. Type a label in brackets (`[AC]`) to simulate a
tap; the simulator maps the label to the button's **id**, because that is what
WhatsApp delivers and sending the label would test a path the app never takes.

Writing it immediately found a real bug:

- **`core/faq.py` `_keyword_match` interpolated guest text into a Postgres
  regex unescaped.** A tapped button arrives as a short message, so
  `[With food]` became the word `[with`, which is a regex character class, and
  Postgres raised `invalid regular expression`. The guest got the error
  fallback instead of an answer, and the owner got an "app is broken" alert.
  Fixed by `re.escape`-ing each word and passing finished patterns. Guarded by
  `test_a_short_message_with_regex_characters_does_not_crash`. **Rule: guest
  text is data, never a pattern — escape it before it reaches any regex.**

A second FAQ hazard, found while building the two-step demo (2026-11-06):
**a grammar word must never be a search key.** The short-question fallback in
`core/faq.py` matched any word of four or more characters, and "there" is four.
"Is there parking" therefore matched the "Is there Wi-Fi?" entry on the shared
word *there* and answered **"Yes, Wi-Fi is available."** — a confidently wrong
answer, the exact thing `core/faq.py` says is worse than an AI call. The fix is
`_STOPWORDS`: grammar words are dropped before the keyword search, so a question
can only match on a word that names something. Guarded by
`test_a_grammar_word_never_pulls_up_an_unrelated_answer`. **Rule: a stopword
answers nothing — never search on one.**

Note: the simulator's own channel must hand out unique outbound message ids
across send kinds. `out.N` / `btn.N` sharing a counter collides on
`messages.wa_message_id`.

## The permanent preview site

`scripts/build_demo_page.py` runs one **interleaved timeline** against a throwaway
database — guest messages and owner messages in the order the two people really
act — and writes a self-contained static site into `docs/`. Nothing on the pages
is hand-written: every bubble is the real reply from the real engine, so the
pages cannot drift from the code.

The timeline is deliberately *not* two separate scripts. A booking is a two-step
approval, and splitting the run into a "guest script" and an "owner script" would
show the steps out of order and hide the point of it. Every captured reply is
tagged with the phone it was sent to, so the guest's window and the owner's
window each get their own side of the same booking, in order. The page therefore
shows the guest requesting a bed and sending a payment screenshot, the owner
tapping **Approve**, then the owner checking the money and tapping **Confirm**.

```
python scripts/build_demo_page.py --site docs   # index.html + demo.html + menu.html
python scripts/build_demo_page.py --out /tmp/x.html   # just the demo page
```

The demo runs with **AI off** (`ai=None`), so the page must not contain any
free-text step the deterministic router cannot answer — that would print the
AI-fallback bubble and make the demo look broken. Keep the timeline to taps,
menu items and questions the router answers.

`docs/` is published on the **`gh-pages` branch** by
`.github/workflows/pages.yml` (GitHub Actions), which is the only way to turn
Pages on with the automation token — `POST /repos/.../pages` returns
`403 Resource not accessible by integration`. Live at
<https://rrajareddy234-gif.github.io/Shiva-sagar-hostel/>.

The AI is deliberately **off** on the demo page (`ai = None`). Every answer
comes from the router and the database, so the page shows deterministic
behaviour and real rate-card prices, not a model's mood. The timeline taps the
app's real button labels (`[Prices]`, `[Availability]`, …), which only works
because the simulator accumulates button labels across prompts — real WhatsApp
keeps every interactive message, so a label from an earlier prompt stays tappable.

## The guest menu is the owner's list

The greeting a guest gets on `hi` is `_MENU_TITLES` in `core/router.py`. It is
seven rows, sent as a WhatsApp **list** (three is the most a button message
allows), and the titles are the owner's own words in the owner's order. Renaming
a row means editing that tuple and the assertions in
`tests/test_router.py::test_greeting_offers_a_menu_of_taps` together.

`Guest Reviews` reads `guest_reviews` through `core/reviews.py`. Two rules hold,
the same ones as `hostel_facts`: a review is **never invented** (none stored is
answered honestly) and **never a price** (a review mentioning an amount is
refused, because a figure there could be quoted back as one). Only `published`
reviews reach a guest, so the owner can hide one without deleting it.

## Concurrency: the last bed and the double tap

Two guests can want the last bed at the same instant, and a guest can tap a
button from an older message. Both used to end in a lost booking.

- **Racing for a bed.** Two concurrent `create_request` calls can deadlock while
  both check the `holds_no_overlap` exclusion constraint. The database still
  protects the bed (exactly one hold wins), but an untranslated deadlock reached
  the guest as a generic error. `core/db._translate` now maps
  `DeadlockDetected`/`SerializationFailure` to `BookingConflict`, and
  `booking_flow._submit` retries once on whatever bed is now free. A lost race
  is a "try again", never a silent drop.
- **Out-of-order taps.** `booking_flow._PREREQ` declares what each tap step
  depends on. A tap whose earlier answers are missing restarts the flow instead
  of walking to review with a required field unset (`KeyError`). `_review` also
  re-checks `_REQUIRED`.

Real race reproduction: two threads calling `booking.create_request` for one bed
behind a `threading.Barrier`. Assert exactly one hold lands.

## The auto-reply pause

`AUTO_REPLY_ENABLED` (default `true`) is the one switch that makes the system go
quiet without taking anything down. The gate lives in
`core.dispatcher.auto_replies_go_out`, called from `dispatch` — the single funnel
where a reply is decided and sent — not in the webhook. That placement is the
whole design: receiving, storing, deduping, handling, booking creation and owner
alerts all still happen; only the outbound reply to the guest is skipped.

**Where the state lives.** Two sources, with a deliberate rule:

- The `settings` table (key `auto_reply_enabled`) holds the owner's **live
  choice**, written by the dashboard button or the WhatsApp command. It survives
  a restart and needs no server access.
- `AUTO_REPLY_ENABLED` in `.env` is the **default and safety net**, used when no
  row exists or the database cannot be read. A fresh deployment behaves exactly
  as before.

A row, if present, wins. All the logic is in `core/auto_reply.py`.

**Three control paths, one state.** All three call
`core.auto_reply.set_paused`, so they cannot disagree:

- `core/commands.py` — typed owner commands (`pause replies`, `resume replies`,
  `auto reply status`, `owner help`), plus the parser entries.
- `api/dashboard.py` — `POST /dashboard/auto-reply`, behind the password.
- `core/tools.py` — the owner-only `auto_reply` tool, for the owner's own words
  ("stop replying for now"), with `ai/prompts.py` telling the model to use it.

Every change is audited via `booking._audit`.

Two consequences worth keeping in mind when changing this area:

- **Owner and staff are never gated.** `auto_replies_go_out` returns True for
  them. If the owner were silenced too, "resume replies" would be sent into a
  void and the only way out would be the server. This is what makes the pause
  controllable from WhatsApp.
- **A paused guest message is still marked processed.** It is handled, just not
  answered, so it must not show up as "stuck" in the health check's
  unprocessed-message count.

`_env_bool` in `core/config.py` accepts 1/true/yes/on and 0/false/no/off, and
falls back to the default on anything else, so a typo cannot silently flip the
switch. `/health` reports `"auto_reply": "enabled" | "paused"` by reading the
live state, so it follows a change made anywhere without a restart. The
dashboard is routed through Caddy (`handle /dashboard*`); it was previously not
published at all. Tests: `tests/test_auto_reply_pause.py`,
`tests/test_auto_reply_controls.py`.

## A restored database can be older than the code (2026-11-05)

The preview workspace was recycled and `start_app.sh` restored the newest backup
(`backups/sagarpg-20261104-141301.sql`). That dump predates the `name_source` and
`wa_profile_name` columns on `guests`, which the code writes on every inbound
message. Every webhook then answered **500 `UndefinedColumn`**, Meta retried, and
**no guest got a reply at all** — the symptom was "WhatsApp replies were not
coming", not an error the owner could see.

`db/schema.sql` already defines those columns, but the schema was only ever
applied when the database was *empty* (startup script) or by hand
(`scripts/chat.py`). A restore leaves a populated, stale database, which is
exactly the case neither path covered.

**Fix:** `create_app` now calls `db.apply_schema()` at startup. The schema is
written to be re-runnable, and the additive `ALTER TABLE ... ADD COLUMN IF NOT
EXISTS` statements at the end are how a column added in code reaches an existing
database. A failure is logged loudly but does not stop the app — a webhook that
answers 500 is worse than a running app that can be repaired.

Guarded by `test_startup_adds_a_column_a_restored_database_is_missing`, which
drops the column and asserts that building the app restores it. Verified against
the running app: dropping `name_source` and restarting logged
`database schema is up to date` and put the column back.

**Rule for the future:** a restore is not "done" when the rows come back; it is
done when the schema matches the code. Any column the code writes must be
reaching existing databases through `db/schema.sql`, not only through the
empty-database path.

## A throttled send must not be retried (2026-11-05)

A guest number sent roughly seven messages a minute for twelve minutes. Meta
answered every reply with **400 `(#131056) (Business Account, Consumer Account)
pair rate limit hit`** — too many messages from this number to the same guest in
a short window. The guest saw nothing.

The app made it worse. `_reply` prefers an interactive message and falls back to
plain text when it cannot be sent, so a refused prompt was immediately followed
by a refused text: **two** messages counted against the same limit instead of
one. The fallback is right for a broken prompt and wrong for a throttle.

`SendResult` now carries `retryable` (default `True`, so every existing
construction is unchanged). The Meta adapter sets it `False` for `131056` and
HTTP 429. `_reply` returns without the text fallback when `retryable` is
`False`; with no prompt there is nothing to fall back from, so a plain text
reply is still attempted exactly once.

Note what this does **not** do: a guest who sends seven messages a minute will
still be throttled, because that is Meta's rule, not ours. The fix stops the app
from spending its quota twice on one reply.

Tests: `test_a_rate_limited_send_is_marked_not_retryable`,
`test_an_ordinary_failure_stays_retryable`,
`test_a_throttled_prompt_does_not_trigger_a_second_message`,
`test_a_throttled_plain_text_reply_is_attempted_once`. `FakeChannel` gained
`throttle` and an `attempts` list, because a test that only inspects successful
sends cannot tell "did not try" from "tried and failed".

## A prompt is not the answer (2026-11-05)

The owner reported two things: "check availability gives me a Book Room button
instead of the vacancies", and "advance payment gives sumup, not the nights,
guests or totals".

Both were one bug. `_reply` sent the prompt **instead of** the reply text:

```python
if prompt is not None and prompt.is_valid():
    result = channel.send_interactive(phone, prompt)   # text never sent
```

So the guest saw only the question. Measured, that is:

| the guest asked | the router's answer | the guest saw |
| --- | --- | --- |
| `do you have a room free` | 156 chars: "We have 68 beds free... - AC: 17 free" | "Would you like to book a room?" |
| `what are your prices` | 798 chars: the whole rate card | "Our rates are per bed. Which room type?" |

The router had done its job every time. The answer was computed, then thrown
away. That is why availability looked like a bare button and the payment step
looked like it had "no details" -- the details were in the text that never went
out.

`_reply` now sends the body first, then the prompt. One exception: when the
prompt body *is* the answer (the food question, "AC room. With food or
without?"), the prompt replaces the text, because sending both repeats the
sentence. If that prompt fails, the same words fall back to text, so the guest
is never left with nothing.

This doubles the outbound messages for replies that carry a prompt, which is
worth watching against the Meta per-pair throttle above. The alternative was
dropping the answer, which is worse.

**Wording.** The guest-facing word for the non-refundable up-front amount is now
**reservation**, not advance, and it is always labelled NOT refundable:

- payment step: buttons `Reservation` / `Full amount`
- review summary: rate, nights, guests, total, reservation, balance at check-in,
  and "The reservation of Rs X is NOT refundable."
- the bill: "(of which Rs X is the reservation)" and "The reservation is NOT
  refundable."
- `billing.advance_notice` says "minimum reservation" and "NOT refundable"

The code, the `ADVANCE` button id, the `payment_terms` column and the
`advance_due` field keep their names: renaming them would churn the database and
the tool schema for no gain a guest can see. Only the words a guest reads
changed.

`scripts/build_demo_page.py` taps button labels, so its `[Advance]` became
`[Reservation]`; the pages are generated, so they cannot drift.

## A party's beds were only counted once (2026-11-05)

Found while testing the full-room work, and it is the worst kind of bug this
codebase can have: it showed beds as free when they were sold.

`core/availability.py` read only `bookings.bed_id`. A booking row names **one**
bed -- the first of the party -- and `booking_beds` names the rest. So a guest
booking five dorm beds left four of them reporting free, and those four could be
sold again to someone else. Measured on the dev DB: 19 of 20 dorm beds were
sold, and availability still said they were free.

Both `_fetch_beds()` and `bed_is_available()` now check `booking_beds` **and**
`bookings`. It is a union, not a replacement: a bed is free only when neither
record says it is taken. The tests insert into `bookings` alone, so replacing
the old check would have silently broken every existing availability test while
looking like a fix.

The lesson for this repo: **when two tables can describe the same fact, read
both.** `bookings.bed_id` and `booking_beds.bed_id` both mean "this bed is
taken", and reading one of them is a guess.

## A typed answer is read back before it is used (2026-11-05)

A number typed by hand is easy to get wrong, and a wrong night count sends a
wrong bill -- which breaks the standing rule that every amount is computed from
the rate card, because the amount would be computed from a wrong input.

So a typed **date, night count, guest count or room type** is now read back and
confirmed with `Yes, correct` / `Change` before it is used. The value is held in
`state["pending"]` and is not committed to its real key until the guest agrees.

Three details worth keeping:

- **A tap is never re-asked.** A button is already an explicit choice. Only typed
  input goes through the confirm step, or the flow would double every question.
- **Any other reply is read as the correction.** A guest who types `3` instead
  of tapping Change is understood, not scolded.
- **The pending value is not the value.** `_confirm_typed` stores it under
  `pending`, so a guest who abandons the flow mid-confirm leaves no half-set
  field behind.

## A full room type is shown, then refused (2026-11-05)

WhatsApp buttons have **no disabled state** -- Meta's interactive message has no
way to grey a button out. So a full room type cannot be hidden or disabled
cleanly. The chosen approach, which is the honest one:

- the button stays visible, labelled `Dormitory (full)`, so the guest can see
  the hostel is busy rather than wondering why a room type vanished;
- the body text says the room is full and points at the other types;
- the tap is **intercepted** and answered with the reason, instead of silently
  advancing or dumping the guest back on the same question.

`_full_room_types(db, state)` is the single place that decides "full", so the
label and the tap guard can never disagree.

## The published demo page is generated -- regenerate it after behaviour changes (2026-11-05)

`docs/demo.html` is built by `scripts/build_demo_page.py --site docs` from a real
run of the engine. It is committed, and it is published on GitHub Pages, so it
**can** go stale: it did. After the map-link and confirm-step work the page still
showed the old location reply with no map link. Nothing failed; the page was
simply old.

Run the generator after any change a guest would see, before committing.

## What a workspace recycle takes, and what it leaves (2026-11-05)

Measured, not assumed. On 2026-11-05 the workspace recycled mid-conversation:
the machine booted at 16:31 while the conversation had started at 10:14. Docker
came back at 16:53 and the Python packages were reinstalled at 16:57 -- that is
the window where `import psycopg` fails and the DB looks empty.

The split is by mount, and it decides everything:

| Path | Disk | Survives a recycle? |
| --- | --- | --- |
| `/workspace` (the repo, `.git`, commits) | `/dev/nvme0n2` ext4 | **yes** |
| `/`, `/tmp`, `/home`, site-packages | overlay | no |
| Docker daemon, the `sagarpg-pg` container | overlay | no |
| Running processes (app, watchdog) | memory | no |

So **code survives; anything running does not.** A watchdog, a keeper, or any
other self-healing loop placed in `/tmp` dies with the workspace and cannot
restart anything -- it is a guarantee that does not hold. The keeper script tried
in `/tmp/sagar-keeper.sh` only helps while the workspace is alive; it is useless
across a recycle.

Recovery is `bash scripts/watch_app.sh`, which calls `start_app.sh` to bring back
Docker, the packages, the database and the app, then starts the watchdog. It is
safe to run repeatedly. Nothing here can bring itself back from a recycle: the
durable answer is a real server (`deploy/docker-compose.yml` already sets
`restart: unless-stopped`) plus an external uptime check.

## The lean deployment: n8n is optional (2026-11-06)

The repo ships **two** compose files. Both run the same app, database and HTTPS.

| | `deploy/docker-compose.yml` | `deploy/docker-compose.lite.yml` |
| --- | --- | --- |
| Services | postgres, app, **n8n**, caddy | postgres, app, **scheduler**, caddy |
| Memory | ~500 MB | ~110 MB (measured: app 62 MB, pg 106 MB) |
| Logins | 2 (n8n + dashboard) | 1 (dashboard) |
| Secret that must never change | 2 (incl. `N8N_ENCRYPTION_KEY`) | 1 |

**The webhook never went through n8n.** `deploy/Caddyfile` sends `/webhook`
straight to the app, which verifies the Meta signature itself. n8n handled the
schedule only. That is why n8n can be removed without touching the WhatsApp
path — `deploy/Caddyfile.lite` does exactly that and refuses unknown paths
instead of proxying them.

`deploy/scheduler.py` replaces it. Job due-ness is the pure function
`due_jobs(now, last_runs)`, so `tests/test_scheduler.py` checks the timing
without a clock. Message wording moved out of the workflows' JavaScript (covered
by nothing) into `core/reports.py` (covered by `tests/test_reports.py`). The
`/jobs` endpoints now return a `text` field, so the wording has one home.

**Silence is deliberate.** `core/reports.py` returns `None` for a healthy
system and an empty day. Messaging on a timer trains the owner to ignore the
alerts, including the one that matters. A *failure*, though, always becomes a
message: `Scheduler.fetch` parses a 503 body, because
`/jobs/health-check` answers 503 *with* the problem text.

Note for the recovery loop: `watch_app.sh` restores the app on port 12000, and
the app only picks up edited Python after a restart — the watchdog does that
within its 30 s interval, so an edit needs a restart before it is observable
over HTTP.

Lint: `ruff.toml` now carries a documented `deploy/scheduler.py` entry for
S108/S310, following the existing audited-convention pattern used for
`scripts/*`.

## Walk-in services and the 10% offer (2026-11-03)

Two things were added that are **not bookings**, and keeping them out of
`bookings` is the whole design.

**A fresh-up (Rs 50) and a three-hour rest (Rs 100) are counter services.**
They live in `services` / `service_orders` (`core/services.py`), not in
`bookings`. A fresh-up uses no bed. A rest holds a bed for its three hours and
then releases it, so the same bed can still be sold for the night. If either
were written as a booking, every occupancy figure would be wrong: a rest is not
a guest staying the night. `service_orders` carries its own
`EXCLUDE USING gist` on `(bed_id, tstzrange(starts_at, ends_at))` so a confirmed
rest cannot overlap another rest on the same bed; `create_order` also checks the
booking tables, so a rest cannot land on a booked bed either.

The guest reaches them from the menu's **Fresh-up or Rest** entry
(`core/service_flow.py`, ids `svc:` / `sv:`). They follow the same two-step
approval as a booking: the owner accepts, the guest pays, the owner confirms.

**The 10% offer is dated data, not code.** A booking *made* in the next three
days gets 10% off the total. `promotions` holds the window and the percentage
(`scope` = DAILY | MONTHLY | ALL); `active_promotion` reads it and `quote()`
applies it. `monthly_quote()` never applies one, and the services never call it,
so a fixed PG rate and a Rs 50 fresh-up are never quietly reduced. The date
judged is the day the booking is made, not the day of the stay — the offer is
"book now", so `booked_on` defaults to today. Every figure still comes from
`core/pricing.py`; the discount is a recorded line, not a typed number.

The guest-facing wording for all of this is in `docs/MOCK_CONVERSATIONS.md`.

## The owner's "bookings" and "customers" no longer disagree (2026-11-03)

The owner typed `bookings` and got an occupancy count ("1 occupied, 0 blocked"),
then typed `customers` and got a guest list, and the two read as if one were
wrong. They were answering different questions: the word `bookings` was falling
through to the AI, which summarised occupancy, while `customers` was answered
from the records. `owner_answer` now answers `bookings` in SQL
(`booking.current_bookings`, live statuses only) and lists the bookings
themselves. "pending"/"waiting"/"awaiting approval" still route to the pending
list, because that is a different question again.

## The guest flow regressions the owner caught (2026-11-03)

The owner tested the guest phone flow and said it was worse than before. Four
faults, all in the guest journey, all now fixed and pinned by
`tests/test_guest_flow_regressions.py`.

**The wait wall was the big one.** `core/sticky.py` was called *before*
`router.route` in `core/handler.py`, so once a guest had a live request every
later message got the "wait for the owner" reply -- including "hi", "where are
you" and "what are the prices". The hold now runs *after* the router has had its
say: a question the router can answer is answered, and only a message the router
cannot place is held. A genuine second booking is still refused, because the
router refuses `book`/`menu:book` itself while a request is live. When moving
this check, keep it after the router or the wall comes back.

**Typed services fell to the AI.** "fresh up" / "rest for 3 hours" matched
nothing in `classify`, so no `service_order` was ever created. `router._SERVICE`
now claims them and `route()` opens `_service_menu`, the same menu the
"Fresh-up or Rest" button opens.

**"I want an AC room" fell to the AI.** `_BOOK` now matches
`i want ... room` / `need ... room`, not just "i want a room".

**The opening message was ignored.** `booking_flow.start(text=...)` now reads a
date, a stay length, a room type, food and guests out of the guest's first
message, and `_first_missing` asks only for what is still unset. `_parse_date`
reads "tomorrow" / a month name ("1 December") / a numeric date ("2026-12-01")
*anywhere* in the sentence. Two traps: the month alternation is built from
`_MONTHS` so a stray word like "from 1" is not read as a month, and the tap
handlers (`date`/`cout`/`stay`/`times`/`guests`/`food`) now call `_first_missing`
rather than hardcoding the next step, or they would step over a prefilled answer.

Nothing here guesses: a value is used only when the guest wrote it, and every
rupee still comes from `core/pricing.py`.

A stray tap now re-asks the current question (`_reask`) instead of ending the
flow, and the greeting no longer says "how many nights" -- the flow asks for a
check-out date, not a number of nights.

### Round 2: the flow reset itself, and the offer was invisible

The owner tested the phone again. Two more faults, both in the guest journey.

**The flow jumped back to the first question.** At a read-back step
(`_CONFIRM_STEP`) the guest typed something we could not read as the value being
confirmed -- "2 nights" while the check-in date was being confirmed.
`_answer_confirm` fell through to `_reinterpret`, which could not parse it, and
`_reinterpret` ended with `_redo_field`. `_redo_field` *erases* the field and
sends the guest back to that question's step -- for a date, back to
`_ask_date`, the first question of the whole flow. That is the "went to first
state" the owner saw. `_reinterpret` now ends with `_reask_confirm`, which
re-shows the same confirmation with the value we already hold, so an unreadable
reply costs one re-ask, never the booking. Trap: `_redo_field` is still correct
for a *deliberate* "Change" tap -- only the unreadable-reply path changed.

**"1 guest" was read as 01:00.** At the time step, `_parse_clock` took any bare
number anywhere in the sentence as a clock time, so "1 guest" confirmed a 1:00
am check-in and skipped the guest-count question. A bare number is now a time
only when it is the *whole* message (`value.isdigit()`); a colon or an am/pm
still counts anywhere. "9 pm", "21:00" and "9:00 pm to 6 am" all still parse.

**The greeting hid the offer.** The owner asked for the 10% offer to be visible
when the guest says hello. `router._offer_line` reads the running promotion from
the table (`pricing.active_promotion(scope="DAILY")`) and the greeting appends
one line: percent, days left, end date, and what it does not cover (monthly
stays, counter services). With no promotion set the line is absent, so the
greeting can never promise a discount the owner has not set. The figure is the
owner's, straight from the table -- the rule holds.

**The mockup said "nights".** `mockups/final-booking-template.html` still asked
"How many nights?"; it now asks for a check-out date, matching the code. The
generated `docs/demo.html` was already correct, because it is built from the
engine.

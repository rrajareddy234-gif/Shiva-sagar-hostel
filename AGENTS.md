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

Note: the simulator's own channel must hand out unique outbound message ids
across send kinds. `out.N` / `btn.N` sharing a counter collides on
`messages.wa_message_id`.

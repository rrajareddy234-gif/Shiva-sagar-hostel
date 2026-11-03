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
- The available token is READ-ONLY. Re-verified 2026-11-03: creating a ref
  returns `403`. Pushes fail and the contents API returns
  `403 Resource not accessible by integration`. Commits must stay local until
  write access is granted or the owner supplies a `repo`-scoped PAT.
- Target repo: `rrajareddy234-gif/Shiva-sagar-hostel` (exists, public, still
  empty — `git/trees/HEAD` returns 409 "Git Repository is empty"). The local
  repo has commits; they are not pushed.

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
python -m pytest          # 345 tests; needs the sagarpg-pg container running
```
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
- Webhook tests must give each message a **unique** id. Reusing one is correctly
  treated as a duplicate delivery and silently dropped, which looks like a bug
  and is not.
- The app normalises phone numbers to a leading `+`. Meta delivers them without
  one, so a test that sends `919...` must assert on `+919...`.
- After checkout a bed is `CLEANING`, not `AVAILABLE`. That is deliberate; the
  owner must send "bed X is ready" before it is bookable again.
- Router patterns must accept plurals. `_PRICE` originally matched only
  "price", so "what are your prices?" fell through to the AI.

# Sagar PG AI Manager

An AI manager for Sagar PG Hostel, Macherla, Palnadu district, Andhra Pradesh.

Operated primarily through WhatsApp. The owner asks questions in plain language and
receives answers from live data; customers receive accurate availability, pricing and
booking information; the owner keeps final approval over every booking.

## Design in one line

Deterministic core, AI on the edges. Availability, pricing, payments, date arithmetic
and booking rules are plain code and SQL. AI only understands free text and drafts
replies, so booking accuracy never depends on a model.

## Status

Phases 1 to 4 are built and tested. Phase 5 (deployment preparation) is next.
Nothing is live: no real customer messages are sent, no production data is used,
and no paid service has been subscribed to.

- **Phase 1 audit** — `docs/PHASE1_AUDIT_AND_ARCHITECTURE.md`
- **Phase 2 architecture and costs** — same document, plus
  `docs/ADR-001-whatsapp-channel.md` for the WhatsApp channel decision
- **Phases 3 and 4 build and test** — 303 tests pass against a real PostgreSQL.
  See `docs/TEST_PLAN.md` for what is covered.
- **Phase 5** — `docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`, `docs/COST_REPORT.md`

## Quick start

```bash
pip install -r requirements.txt

# A database to develop against
sudo docker run -d --name sagarpg-pg -e POSTGRES_USER=sagar \
  -e POSTGRES_PASSWORD=devpass -e POSTGRES_DB=sagarpg -p 5432:5432 postgres:16

# Schema and the owner's historic room list
sudo docker exec -i sagarpg-pg psql -U sagar -d sagarpg < db/schema.sql
sudo docker exec -i sagarpg-pg psql -U sagar -d sagarpg < db/seed.sql

python -m pytest            # 303 tests
cp .env.example .env        # then fill in the values
uvicorn api.main:app --reload
```

## Layout

```
docs/        audit, architecture, runbooks, test plan, cost report
db/          PostgreSQL schema and seed data
core/        deterministic business logic and the message pipeline
api/         FastAPI webhook and scheduled job endpoints
ai/          provider-independent model adapter
n8n/         generated workflow JSON and the script that builds it
tests/       pytest suite, dummy data only
```

## How a message is handled

```
WhatsApp -> Meta -> webhook -> verify signature -> dedupe -> store
                                                     |
                          owner number?  --- yes ---> owner commands, then SQL answers
                                | no
                                v
                     deterministic router answers from SQL
                                |
                     not understood? -> AI (only then)
                                |
                        send reply, record it, mark processed
```

Most questions never reach a model, which is what keeps the running cost low.

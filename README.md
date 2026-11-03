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

Phase 1 (audit) complete. Phase 2 (architecture) proposed and awaiting owner approval.
See `docs/PHASE1_AUDIT_AND_ARCHITECTURE.md` for the audit, the recommended architecture,
the cost estimates and the implementation plan.

Nothing here is live. No real customer messages are sent and no production data is used.

## Planned layout

```
docs/        audit, architecture, runbooks
db/          PostgreSQL schema and migrations
core/        deterministic business logic (availability, booking, pricing, payments)
api/         FastAPI webhook and internal endpoints
ai/          provider-independent model adapter
n8n/         exported workflow JSON
tests/       pytest suite (dummy data only)
deploy/      Docker Compose, Caddy, backup and restore scripts
```

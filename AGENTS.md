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
- The available token is READ-ONLY: pushes return
  `403 Permission ... denied` and the contents API returns
  `403 Resource not accessible by integration`. Commits must stay local until
  write access is granted or the owner supplies a `repo`-scoped PAT.
- Target repo: `rrajareddy234-gif/Shiva-sagar-hostel` (was empty; now the local
  repo has commits but they are not yet pushed).

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

# Deployment

How to put this on a server. Nothing here has been run against a live server
yet, so treat it as the plan to follow and verify step by step.

## What runs

One small server runs three things:

```
Meta WhatsApp  --webhook-->  n8n  --forward-->  this app  -->  PostgreSQL
                               |
                               +--schedule-->  this app's /jobs endpoints
```

The app holds the WhatsApp credentials. n8n never sees them.

## Before you start, you need

1. A server. Any small Linux box, 2 vCPU and 4 GB. About ₹400–600 a month.
2. A domain name pointing at it, for HTTPS. Meta requires HTTPS.
3. A Meta Developer app with the WhatsApp product added, and a phone number
   registered to it. You already have this; it needs re-checking.
4. The four Meta values: app secret, access token, phone number id, verify token.
5. An AI key, or none at all. The system works without one and falls back to a
   plain reply.

## Step 1: the server

```bash
# On a fresh Ubuntu 24.04 server
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"   # then log out and back in
```

## Step 2: the code

```bash
git clone <your private repository> /opt/sagar
cd /opt/sagar
cp .env.example .env
```

## Step 3: the secrets

Edit `/opt/sagar/.env`. Fill in every value. Generate the job token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set a strong PostgreSQL password too. Keep this file readable only by its owner:

```bash
chmod 600 /opt/sagar/.env
```

Never commit this file. `.gitignore` already excludes it, and a test checks that.

## Step 4: start everything

```bash
docker compose -f deploy/docker-compose.yml up -d
```

## Step 5: the database

```bash
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/schema.sql

# The owner's historic room list. Read db/seed.sql first: every row is marked
# unconfirmed, and you must check the rooms and the rates before going live.
docker compose -f deploy/docker-compose.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/seed.sql
```

## Step 6: check it answers

```bash
curl https://your-domain.example/health
# {"status":"ok","database":"ok","channel":"meta_cloud"}
```

If this fails, the app cannot reach the database. Check `docker compose logs app`.

## Step 7: n8n

1. Open n8n at `https://your-domain.example:5678` and create the owner account.
   This account is the n8n administrator; use a strong password and do not share it.
2. Add two Variables under Settings, so no secret sits inside a workflow file:
   - `SAGAR_APP_URL` — `https://your-domain.example`
   - `SAGAR_JOB_TOKEN` — the same value as `JOB_TOKEN` in `.env`
3. Import the six workflows from `n8n/`. Each opens inactive.
4. Open each one and confirm the URLs resolve to your domain.
5. Activate them one at a time. Activate the health check first, so a problem
   anywhere else is reported.

## Step 8: Meta

1. In the Meta app, set the webhook callback URL to the n8n inbound webhook:
   `https://your-domain.example/webhook/sagar-whatsapp`
   (n8n shows the exact path on the webhook node.)
2. Set the verify token to the same value as `META_VERIFY_TOKEN` in `.env`.
3. Subscribe to the `messages` field.
4. Meta sends a GET request immediately. n8n passes it to the app, which answers
   the challenge. If verification fails, the token does not match.

**Before any real customer message**, run the checks in `docs/OPERATIONS.md`
under "Going live checklist".

## The WhatsApp window rule

A business may only send a free-form reply within 24 hours of the customer's last
message. Outside that window Meta requires an approved template.

This matters for the follow-up feature. An automatic follow-up sent more than
24 hours after the customer wrote needs a template that Meta has approved. No
template exists yet, so automatic follow-up is deliberately switched off. Until
it is created and approved, follow-up is manual.

## Backups

```bash
/opt/sagar/deploy/backup.sh          # writes to /opt/sagar/backups
```

Set it to run nightly with cron, and copy the result somewhere else — a backup on
the same server is not a backup.

Restore, tested as a rehearsal rather than assumed:

```bash
/opt/sagar/deploy/restore.sh backups/sagarpg-2026-11-03.sql.gz
```

## Rolling back

Before any change to a live system:

```bash
git -C /opt/sagar rev-parse HEAD > /tmp/last-known-good
/opt/sagar/deploy/backup.sh
```

To roll back:

```bash
cd /opt/sagar
git checkout "$(cat /tmp/last-known-good)"
docker compose -f deploy/docker-compose.yml up -d --build app
```

Rolling back the code does not roll back the database. If a change altered the
schema, restore the backup taken before the change.

## What is deliberately not here

- No Kubernetes, no load balancer, no second server. A 63-bed hostel does not
  need them, and each one adds a thing that can break.
- No cloud database. A local PostgreSQL on the same box is cheaper and one fewer
  network hop. The nightly backup is what protects the data.
- No separate staging server. Test against a copy of the database on the same
  server instead.

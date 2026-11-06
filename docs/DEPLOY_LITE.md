# Putting it on a real server — the cheap way

This is the shorter, cheaper path to a live system. It runs the same app, the
same database and the same HTTPS as `docs/DEPLOYMENT.md`, with n8n left out.
`deploy/docker-compose.lite.yml` starts four containers instead of five.

Read this one if you want the lowest monthly cost and the fewest moving parts.
Read `docs/DEPLOYMENT.md` instead if you specifically want n8n's visual workflow
editor.

## Why drop n8n

n8n did three things: run on a schedule, call an endpoint, forward a message.
That is a loop and two HTTP calls, and `deploy/scheduler.py` now does them in
Python, where the rest of the system lives and where `tests/test_scheduler.py`
and `tests/test_reports.py` cover it.

| | With n8n | Without (this guide) |
|---|---|---|
| Containers | 5 | 4 |
| Memory | ~500 MB | ~110 MB |
| Logins to protect | 2 | 1 |
| Secrets that must never change | 2 | 1 |
| Message formatting tested | no | yes |

The WhatsApp webhook never went through n8n in either setup. Caddy sends
`/webhook` straight to the app, which verifies Meta's signature itself. n8n only
ever handled the schedule.

## What it costs

Checked in November 2026. Providers change prices; re-check before buying.

| Item | Cost |
|---|---|
| Server (Hetzner CX22 class, 2 vCPU / 4 GB) | about ₹400–600 per month |
| Domain, if you buy one | about ₹700–900 per year |
| Domain, free option (DuckDNS) | ₹0 |
| HTTPS certificate (Let's Encrypt, via Caddy) | ₹0 |
| AI (Gemini Flash Lite) | pennies per month |

You can start at **₹0 for the domain** and **₹400–600 for the server**.

## What you need before you start

1. A server. Any small Linux box on the public internet. Meta has to reach it,
   so it cannot be your home computer behind a router you do not control.
2. A domain name. Meta requires HTTPS, and a certificate can only be issued to
   a domain, never to a bare IP address. A free DuckDNS name is enough to start.
3. Your Meta values: app secret, access token, phone number id, verify token.
4. An AI key, or none. The system works without one and falls back to a plain
   reply.

## Step 1: a domain name

Pick one. You can change it later by editing one line and restarting Caddy.

### Free: DuckDNS

1. Go to `https://www.duckdns.org` and sign in with GitHub or Google.
2. Create a subdomain, for example `sagarpghostel`. You get
   `sagarpghostel.duckdns.org`.
3. Set its IP to your server's public IP.
4. Set `SAGAR_HOST=sagarpghostel.duckdns.org` in `.env`.

Caddy will get a real Let's Encrypt certificate for it, because you control the
DNS record. This is a genuine HTTPS setup, not a workaround.

### Paid: your own domain

Any registrar will do. An `.in` domain is usually the cheapest. Point an `A`
record at your server's IP, then set `SAGAR_HOST` to the domain.

> A free subdomain looks less professional if a guest ever sees the link. Guests
> normally interact over WhatsApp and never see the URL, so this matters less
> than it sounds. Start free, buy a name later if you want one.

## Step 2: the server

```bash
# On a fresh Ubuntu 24.04 server
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"   # then log out and back in
```

Open ports 80 and 443 in the provider's firewall. Caddy needs both: 80 for the
certificate challenge, 443 for the traffic.

## Step 3: the code

```bash
git clone <your repository> /opt/sagar
cd /opt/sagar
cp .env.example .env
```

## Step 4: the secrets

Edit `/opt/sagar/.env`. At minimum, set these:

| Key | What it is |
|---|---|
| `POSTGRES_PASSWORD` | A strong password. Generate one, do not invent one. |
| `SAGAR_HOST` | Your domain, without `https://` |
| `JOB_TOKEN` | Shared secret for the scheduler. Generate it. |
| `DASHBOARD_PASSWORD` | The owner's dashboard password |
| `META_APP_SECRET` | From the Meta app |
| `META_ACCESS_TOKEN` | From the Meta app |
| `META_PHONE_NUMBER_ID` | From the Meta app |
| `META_VERIFY_TOKEN` | Any random string; paste the same one into Meta |
| `OWNER_PHONE` | The owner's WhatsApp number, in E.164 |
| `AI_API_KEY` | Optional. Without it, replies are plain but correct. |

You do **not** need `N8N_HOST`, `N8N_PASSWORD` or `N8N_ENCRYPTION_KEY` for this
setup. Leave them blank.

Generate the two random secrets:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then lock the file down:

```bash
chmod 600 /opt/sagar/.env
```

Never commit this file. `.gitignore` excludes it and a test enforces that.

## Step 5: start everything

```bash
cd /opt/sagar
docker compose -f deploy/docker-compose.lite.yml up -d --build
```

Every service is set to `restart: unless-stopped`, so they come back after a
reboot or a crash. Nothing needs a manual start.

## Step 6: the database

```bash
docker compose -f deploy/docker-compose.lite.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/schema.sql

# Read db/seed.sql before running it. Every row is marked unconfirmed, and the
# rooms and rates must be checked before going live.
docker compose -f deploy/docker-compose.lite.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/seed.sql
```

## Step 7: check it answers

```bash
curl https://your-domain.example/health
# {"status":"ok","database":"ok","channel":"meta_cloud"}
```

Open `https://your-domain.example/dashboard` and log in with
`DASHBOARD_PASSWORD`. If the page loads over HTTPS with no certificate warning,
Caddy worked.

## Step 8: the scheduler

It starts with the stack; there is nothing to log into. Confirm it is running:

```bash
docker compose -f deploy/docker-compose.lite.yml logs -f scheduler
```

A healthy system prints lines like `health-check: nothing to report`. That is
correct: the scheduler stays silent while everything is fine, so the one message
that matters is not lost in noise.

To see what it would send without sending it:

```bash
docker compose -f deploy/docker-compose.lite.yml exec scheduler \
  python /app/deploy/scheduler.py --once --no-notify
```

The schedule it runs:

| Job | When | Silent when |
|---|---|---|
| health-check | every 10 minutes | all is well |
| pending-requests | every 15 minutes | nothing is waiting |
| unanswered-enquiries | every 30 minutes | nobody is waiting |
| daily-report | 07:30 daily | never — always sends |
| upcoming | 20:00 daily | the day is empty |

All times are `HOSTEL_TIMEZONE` (default `Asia/Kolkata`).

## Step 9: Meta

1. In the Meta app, set the webhook callback URL to
   `https://your-domain.example/webhook`
2. Set the verify token to the same value as `META_VERIFY_TOKEN`.
3. Subscribe to the `messages` field.
4. Meta sends a `GET` immediately. The app answers the challenge. If verification
   fails, the token does not match.

Before any real customer message, work through `docs/OPERATIONS.md` under
"Going live checklist".

## Backups

```bash
/opt/sagar/deploy/backup.sh          # writes to /opt/sagar/backups
```

Run it nightly with cron, and copy the result somewhere else. A backup on the
same server is not a backup.

```bash
# crontab -e
30 2 * * * /opt/sagar/deploy/backup.sh >> /var/log/sagar-backup.log 2>&1
```

Restore, rehearsed rather than assumed:

```bash
/opt/sagar/deploy/restore.sh backups/sagarpg-2026-11-06.sql.gz
```

## Checking on it

```bash
docker compose -f deploy/docker-compose.lite.yml ps      # what is running
docker compose -f deploy/docker-compose.lite.yml logs app # what the app says
```

The scheduler messages the owner when the health check fails, so a silent phone
means a working system.

## Where the data lives

The database keeps its data in the named volume `pgdata`, not inside the
container. The container can be stopped, removed and recreated without losing a
booking.

Never start the database with a bare `docker run`. A container created that way
has no volume, and its data dies with it.

The volume is not a backup. It survives a restart, not a deleted volume or a
corrupt disk. The nightly dump is what protects the data.

## Rolling back

```bash
git -C /opt/sagar rev-parse HEAD > /tmp/last-known-good
/opt/sagar/deploy/backup.sh
```

To roll back:

```bash
cd /opt/sagar
git checkout "$(cat /tmp/last-known-good)"
docker compose -f deploy/docker-compose.lite.yml up -d --build app
```

Rolling back the code does not roll back the database. If a change altered the
schema, restore the backup taken before the change.

## If you want n8n after all

`deploy/docker-compose.yml` is unchanged and still works. The two setups share
the same `.env`, the same database volume name and the same Caddy behaviour for
`/webhook`, so switching is a matter of using the other compose file. Do not run
both at once: they would both try to bind ports 80 and 443.

## What is deliberately not here

- No Kubernetes, no load balancer, no second server. A 63-bed hostel does not
  need them, and each one adds a thing that can break.
- No cloud database. A local PostgreSQL on the same box is cheaper and one fewer
  network hop.
- No separate staging server. Test against a copy of the database on the same
  server instead.
- No n8n. See the top of this file.

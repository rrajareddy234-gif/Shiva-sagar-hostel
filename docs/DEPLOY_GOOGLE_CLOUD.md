# Deploy to Google Cloud — step by step

For the owner. Follow it top to bottom. Every command is copy-paste.

The app is already Docker-ready. This puts it on a Google Cloud server that
stays on, so the WhatsApp bot answers guests at any hour without anyone
restarting it.

**What you need before starting**

1. A Google Cloud account with billing enabled.
2. A domain name (about ₹800/year). Meta requires HTTPS, and HTTPS needs a
   domain. Buy one from GoDaddy, Namecheap or Hostinger.
3. Your `.env` values — the four Meta values and the AI key. You already have
   these in the file `.env` on the dev machine.

---

## Step 1 — Create the server

1. Go to https://console.cloud.google.com
2. Top-left, pick or create a project. Name it `sagar-pg`.
3. In the search bar type **Compute Engine**, open it, click **Enable** if asked.
4. Click **Create instance**.
5. Fill in exactly this:

| Field | Value |
|---|---|
| Name | `sagar-pg-server` |
| Region | `us-central1 (Iowa)` |
| Zone | `us-central1-a` |
| Machine type | **e2-micro** (this is the free one) |
| Boot disk | Ubuntu 24.04 LTS, 30 GB standard |
| Firewall | tick **Allow HTTP traffic** and **Allow HTTPS traffic** |

6. Click **Create**. Wait about a minute.
7. Copy the **External IP** shown next to the server. Call it `SERVER_IP`. You
   will need it in Step 3.

**Important:** `e2-micro` is free forever, but only in `us-west1`, `us-central1`
or `us-east1`. Pick `us-central1` as above. Any other region costs money.

---

## Step 2 — Point your domain at the server

In your domain provider's DNS page, add an **A record**:

| Type | Name | Value |
|---|---|---|
| A | `sagar` | `SERVER_IP` |

So if your domain is `example.com`, the address becomes `sagar.example.com`.
That full name is `SAGAR_HOST` below. Wait 10–30 minutes for it to take effect.

Check it worked from your own computer:

```
ping sagar.example.com
```

It must show `SERVER_IP`. Do not continue until it does.

---

## Step 3 — Connect to the server

In the Google Cloud console, next to the server click **SSH**. A black terminal
opens in the browser. Everything below runs in that terminal.

First, make the machine stable. 1 GB of memory is enough for this app, but a
small swap file stops it ever choking at a busy moment:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

The last line should show 2.0Gi of swap.

---

## Step 4 — Install Docker

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
```

Now **close the terminal and open SSH again** (the group change needs a fresh
login). Then check it works:

```bash
docker run --rm hello-world
```

If you see "Hello from Docker!", you are ready.

---

## Step 5 — Get the code

The repository is public, so no password is needed:

```bash
sudo mkdir -p /opt/sagar
sudo chown $USER /opt/sagar
git clone https://github.com/rrajareddy234-gif/Shiva-sagar-hostel.git /opt/sagar
cd /opt/sagar
```

Switch to the branch that has the working code:

```bash
git checkout fix/remove-unused-imports
```

---

## Step 6 — Create the secrets file

```bash
cp .env.example .env
nano .env
```

Fill in these lines. Leave everything else as it is.

```ini
# --- Database ---
POSTGRES_PASSWORD=make-up-a-long-password-here

# --- WhatsApp (copy from your dev .env) ---
META_APP_SECRET=...
META_ACCESS_TOKEN=...
META_PHONE_NUMBER_ID=...
META_VERIFY_TOKEN=...

# --- Owner ---
OWNER_PHONE=+918688397996

# --- Dashboard ---
DASHBOARD_PASSWORD=make-up-a-password-you-will-remember

# --- AI ---
AI_PROVIDER=gemini
AI_MODEL=gemini-flash-lite-latest
AI_API_KEY=...

# --- Scheduled jobs ---
JOB_TOKEN=paste-a-generated-token-here

# --- Your domain, no https:// ---
SAGAR_HOST=sagar.example.com
```

Generate the `JOB_TOKEN` value with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output into `JOB_TOKEN=`.

In nano: `Ctrl+O` then `Enter` to save, `Ctrl+X` to exit. Then lock the file:

```bash
chmod 600 /opt/sagar/.env
```

**Never share this file and never commit it.**

---

## Step 7 — Start everything

```bash
cd /opt/sagar
docker compose -f deploy/docker-compose.lite.yml up -d --build
```

The first build takes 2–4 minutes. When it finishes:

```bash
docker compose -f deploy/docker-compose.lite.yml ps
```

You should see four containers, all `Up`: `postgres`, `app`, `scheduler`,
`caddy`.

---

## Step 8 — Load the database

```bash
cd /opt/sagar
docker compose -f deploy/docker-compose.lite.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/schema.sql
```

Then the room list. **Read `db/seed.sql` first** — every row is marked
unconfirmed, and you must check the rooms and rates before going live:

```bash
docker compose -f deploy/docker-compose.lite.yml exec -T postgres \
  psql -U sagar -d sagarpg < db/seed.sql
```

---

## Step 9 — Check it answers

```bash
curl https://sagar.example.com/health
```

Expected:

```json
{"status":"ok","database":"ok","channel":"meta_cloud","auto_reply":"enabled"}
```

If it does not answer, wait 60 seconds (Caddy is fetching the HTTPS
certificate), then check the logs:

```bash
docker compose -f deploy/docker-compose.lite.yml logs caddy --tail 30
docker compose -f deploy/docker-compose.lite.yml logs app --tail 30
```

---

## Step 10 — Tell Meta the new address

1. Go to https://developers.facebook.com and open your app.
2. **WhatsApp → Configuration → Webhook**.
3. Callback URL: `https://sagar.example.com/webhook`
4. Verify token: the same value as `META_VERIFY_TOKEN` in `.env`.
5. Click **Verify and save**. It must go green.
6. Under **Webhook fields**, subscribe to **messages**.

Then test for real: send "hi" to the hostel number **+91 83176 05360** from a
different phone. The bot should reply.

---

## Step 11 — Backups (do not skip)

A backup on the same server is not a backup. Set the nightly dump:

```bash
crontab -e
```

Add this line:

```
30 2 * * * /opt/sagar/deploy/backup.sh >> /var/log/sagar-backup.log 2>&1
```

Save and exit. The dumps land in `/opt/sagar/backups`.

---

## Done — what happens now

- If the server reboots, everything comes back **by itself**. That is what
  `restart: unless-stopped` does. Nobody has to restart anything.
- The bot answers guests any hour.
- The dashboard is at `https://sagar.example.com/dashboard`, password =
  `DASHBOARD_PASSWORD`.

---

## Updating the app later

```bash
cd /opt/sagar
./deploy/backup.sh
git pull
docker compose -f deploy/docker-compose.lite.yml up -d --build
```

The database volume is untouched, so no booking is lost.

---

## If something goes wrong

| Symptom | Check |
|---|---|
| `/health` gives 502 | `docker compose -f deploy/docker-compose.lite.yml logs app --tail 30` |
| HTTPS will not work | The A record is not pointing at the server yet, or port 443 is closed |
| Container keeps restarting | Missing value in `.env` — the app refuses to start half-configured |
| Bot does not reply | Webhook not verified in Step 10, or `AUTO_REPLY_ENABLED=false` |

## Costs

- Server: **₹0** (e2-micro, free tier, `us-central1`)
- Domain: about **₹800/year**
- WhatsApp: normal Meta conversation charges
- AI: Gemini free tier to start

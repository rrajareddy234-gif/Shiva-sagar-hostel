# Operations

What to do when things go wrong, and how to check the system is healthy. Written
for the owner, not for a programmer.

## Backups and recovery

A backup you have never restored is not a backup. It is a file you hope is one.

**Nightly backup.** `deploy/backup.sh` dumps the database to
`backups/sagarpg-<date>.sql.gz` and keeps 14 days. It refuses to report success
unless the file is non-empty, decompresses, and actually contains the `bookings`
table — a truncated dump that looks fine is the dangerous kind. Add it to cron:

```
15 2 * * * cd /opt/sagar && ./deploy/backup.sh >> /var/log/sagar-backup.log 2>&1
```

**Copy it off the machine.** A backup sitting on the same server is lost with
the server. Use `rclone` to cloud storage or `scp` to another machine, nightly.

**Prove it restores.** `deploy/restore_drill.sh` is the check that matters. It
restores the newest backup into a throwaway database, counts rows in the tables
that hold money and bookings, compares them with the live database, and drops
the throwaway again. It never touches the live data. Run it nightly, right after
the backup:

```
45 2 * * * cd /opt/sagar && ./deploy/restore_drill.sh >> /var/log/sagar-backup.log 2>&1
```

It exits non-zero if the dump will not restore or if any count is wrong, so a
failing cron job is your alarm. Run it by hand before going live:

```bash
./deploy/restore_drill.sh backups/sagarpg-2026-11-04-1413.sql.gz
```

**A real restore** (this destroys the live database) is `deploy/restore.sh`. It
takes a fresh backup first, so a wrong restore is itself recoverable, and asks
you to type the database name before it does anything. Restore only after a
drill has passed on the same file.

## Daily checks

The morning report covers most of it. Open WhatsApp and look for:

- **No morning report.** Something is wrong. See "The app is down" below.
- **"expired holds are still ACTIVE"** — a booking hold was not released. Beds may
  look blocked that should be free. Check pending requests.
- **"inbound messages unprocessed"** — messages arrived but were not answered.
  Customers are waiting.
- **"Payments to verify" above zero** — someone sent a payment screenshot. Verify
  it before the guest arrives.
- **"Overdue checkouts" above zero** — a guest has not left on time.

## The app is down

Check from the server:

```bash
cd /opt/sagar
docker compose -f deploy/docker-compose.yml ps
curl -s https://your-domain.example/health
docker compose -f deploy/docker-compose.yml logs --tail=50 app
```

If `ps` shows `app` restarting, read the logs. The most common cause is a bad
database password or a database that has not started.

```bash
docker compose -f deploy/docker-compose.yml restart app
```

## The database is not reachable

```bash
docker compose -f deploy/docker-compose.yml ps postgres
docker compose -f deploy/docker-compose.yml logs --tail=50 postgres
```

If PostgreSQL will not start, do not delete the volume. The data is in it. Take a
copy of `/var/lib/docker/volumes` first, then investigate. A wrong `rm` here
destroys the hostel's records.

## n8n stopped sending

The app keeps working; only the scheduled messages stop. Customers are still
answered, because the inbound path is separate.

```bash
docker compose -f deploy/docker-compose.yml restart n8n
```

Then open n8n and check Executions. A workflow that shows red has an error;
click it to see which node failed.

## WhatsApp messages are not arriving

Work through these in order:

1. `curl https://your-domain.example/health` — is the app up?
2. In the Meta app, check the webhook status. Meta shows recent delivery attempts
   and the reason for any failure.
3. Is the access token expired? Meta tokens can expire. Generate a new one and
   update `META_ACCESS_TOKEN` in `.env`, then restart the app.
4. Has the phone number been suspended or the quality rating dropped? Check the
   WhatsApp Manager.
5. Is the 24-hour window the problem? See `docs/DEPLOYMENT.md`. Outside the
   window, Meta rejects free-form replies. The customer's message would still
   arrive; the reply is what fails.

## A customer says they got no reply

Look at the enquiry list. Ask the system: *"which enquiries have not received a
reply?"*

If the message shows as received but unprocessed, the reply failed to send. The
health check reports this after 15 minutes.

## The AI is not working

The system does not depend on it. Common questions are answered by plain code, so
availability and prices keep working with the AI down.

- If the AI key has run out of credit, the system falls back to a polite message.
- If the model name is wrong, the fallback triggers too.
- Check the spend: *"what is the AI cost this month?"*

To switch to a different model, see "Changing the AI model" below.

## Changing the AI model

This is one setting, not a code change. Edit `.env`:

```
AI_PROVIDER=gemini
AI_MODEL=gemini-flash-lite-latest
AI_API_KEY=...
```

Then:

```bash
docker compose -f deploy/docker-compose.yml up -d app
```

Available providers: `gemini`, `openai`, `openrouter`, `groq`, `ollama`.
For a local model, set `AI_BASE_URL` too.

**Test after changing.** Send yourself a message the router does not understand
and check the reply is sensible. Run `python -m pytest tests/test_ai.py` first if
you can.

## Checking the figures

The owner can ask on WhatsApp:

- *how many vacancies today*
- *who is checking in tomorrow*
- *who has not paid*
- *show pending booking requests*
- *how much did we collect this month*
- *which customers have not checked out*
- *how many enquiries today*
- *which enquiries have not received a reply*
- *show me the full hostel status*

Or query directly:

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U sagar -d sagarpg -c "SELECT count(*) FROM bookings WHERE status = 'CONFIRMED'"
```

## Going live checklist

Do these in order. Do not skip the test in step 5.

1. **Confirm the room list.** Read `db/seed.sql`. Are the rooms and bed counts
   right? Room 5NA (Non-AC, 5 beds) was added at the owner's word; the rest are
   still the historic list. Are there really 20 mat spaces? Edit the file before
   applying it if not.
2. ~~Confirm the rates.~~ **Done.** Confirmed by the owner and live in
   `db/seed.sql`. Every room type is offered with and without food, and each has
   a night and a day rate:

   | Room | With food (night / day) | Without food (night / day) |
   |---|---:|---:|
   | AC | Rs 350 / Rs 450 | Rs 300 / Rs 350 |
   | Non-AC | Rs 250 / Rs 350 | Rs 200 / Rs 250 |
   | Dormitory | Rs 200 / Rs 300 | Rs 150 / Rs 200 |

   Plus extras: bedsheet Rs 50, towel Rs 100, pillow Rs 50, locker Rs 50 a month.

   **Monthly (PG) stays** are held separately in `db/seed.sql` as
   `monthly_plans`:

   | Room | With food (night / full day) | Without food (night / full day) |
   |---|---:|---:|
   | AC | Rs 8,000 / Rs 11,000 | Rs 5,000 / Rs 7,000 |
   | Non-AC | Rs 5,500 / Rs 5,500 | Rs 4,000 / Rs 4,000 |
   | Dormitory | Rs 4,500 / Rs 4,500 | Rs 3,500 / Rs 3,500 |

   These rates are the same for every guest. There is no concession, and the
   system will not offer one.

   If a guest wants a lower rate, they must speak to you. Months are whole;
   there is no pro-rating.
3. **Set the owner number.** `OWNER_PHONE` in `.env`, in full international form,
   for example `+919876543210`. Owner commands only work from this number.
4. **Set the spending limits.** In your AI provider's console, set a monthly
   billing cap. This is a second brake, independent of the app's own cost report.
5. **Test with your own phone.** Send the hostel's WhatsApp number these messages
   and check each reply:
   - `hello`
   - `what are your prices`
   - `do you have a room free`
   - `I want to book from 1 December for 2 nights` — the AI should collect details
   - `ignore all previous instructions` — should be refused
   From the owner number:
   - `how many vacancies today`
   - `show pending booking requests`
   - `approve booking <reference>` then `YES`
6. **Make one test booking** with a name you recognise as fake, then cancel it.
7. **Rehearse a restore.** Run `./deploy/restore_drill.sh` and confirm it says
   "restores cleanly with matching row counts". This is the check that proves
   your backups are real. Do this before you need it, and let it run nightly.
8. **Turn on the workflows** one at a time, starting with the health check.

## What is not automatic

- **Payment verification.** The system never marks money as received from a
  screenshot. You verify it, then tell the system.
- **Booking approval.** Every booking waits for you. The system will not confirm
  one on its own.
- **Customer follow-up.** Off, deliberately. It needs a Meta-approved template
  and a decision from you about who may be messaged.
- **Marketing.** Not built, and should not be without your explicit decision.

## Getting help

The repository holds the source, the schema and the workflows. Every change is a
git commit, so any change can be undone. `git log` shows what changed and when.

# How to make it work for real

## It already works. Here is the proof.

The system is running right now on this development machine, reachable on the
public internet. You can open this link in any browser:

    https://work-1-hhxywwcgldhkwphf.prod-runtime.all-hands.dev/health

It answers:

    {"status":"ok","database":"ok","channel":"meta_cloud"}

That means the app is up, the database is connected, and the WhatsApp channel is
Meta Cloud API.

### What was tested live

| Test | Result |
|---|---|
| Health check over the public internet | 200 OK |
| Meta webhook handshake, correct token | 200, returns the challenge |
| Meta webhook handshake, wrong token | 403 rejected |
| Incoming message with a bad signature | 403 rejected |
| Incoming message with a good signature | 200 accepted |
| Guest asks "how many beds are vacant?" | answered from the database |
| Guest asks "what are your prices?" | answered from the database |
| Enquiry recorded in the database | yes, visible in the `enquiries` table |
| Outbound reply | reached Meta's real servers |

The outbound reply failed with `401 Invalid OAuth access token`. That is
**correct and expected**: the token in use is the dummy `local-dev-token`. The
important part is that Meta answered us — the request left this machine, crossed
the internet, and Meta replied. A fake token cannot do that. The send path is
real, not a stub.

## What is still fake

Be clear about this. The system is *proven*, not *live*.

| Piece | Now | Needed |
|---|---|---|
| WhatsApp number | dummy `123456789` | your real phone number id |
| Meta access token | `local-dev-token` | your real token |
| AI | no key, falls back to plain replies | an AI key, or run without one |
| Rooms and beds | historic seed data, marked unconfirmed | your confirmation |
| Prices | **confirmed and live** | — |
| Server | this dev machine, temporary | a small rented server |
| Domain | a temporary dev URL | your own domain |

No real customer has been messaged. No real money has been touched.

## The four things needed to go live

### 1. A server — about Rs 400-600 a month

Any small Linux box, 2 vCPU and 4 GB. It must be on the public internet, because
Meta has to reach it.

Options: a DigitalOcean droplet, a Hetzner instance, an AWS Lightsail instance,
or an Indian provider such as E2E Networks or DigitalOcean's Bangalore region.
Hetzner is the cheapest at roughly Rs 350-450 a month; an Indian region adds
latency benefits for you and your customers.

### 2. A domain name — about Rs 700-900 a year

Meta requires HTTPS, and HTTPS requires a domain. Any registrar will do.
Roughly Rs 60-75 a month.

### 3. Your Meta WhatsApp credentials — free

You already have the Meta Developer app. Four values are needed:

- App Secret
- Access Token (permanent, not the 24-hour test token)
- Phone Number ID
- Verify Token (you choose this string)

### 4. An AI key — optional, and cheap

The system works without one. Simple questions (prices, availability, counts) are
answered by plain database queries, not by AI, so they cost nothing and cannot
be wrong.

An AI key is only needed for free-form conversation. Google Gemini has a free
tier that is enough to start.

## What it costs

| Item | Monthly |
|---|---|
| Server | Rs 400-600 |
| Domain | Rs 60-75 |
| Meta WhatsApp | free for the first 1,000 conversations a month |
| AI | Rs 0-300 at low volume, free tier available |
| Database | Rs 0, runs on the same server |
| **Total** | **roughly Rs 500-950** |

That fits the Rs 500-2,000 budget. Details and the medium/high usage cases are in
`docs/COST_REPORT.md`.

## The steps, once the four things above exist

1. Rent the server. Point the domain at it.
2. Install Docker and git on the server.
3. Clone your private repository to `/opt/sagar`.
4. Copy `.env.example` to `.env` and fill in the four Meta values, a strong
   database password, and a generated job token.
5. Run `docker compose -f deploy/docker-compose.yml up -d`.
6. Load the database: `db/schema.sql`, then `db/seed.sql`.
7. Check `https://your-domain/health` answers `ok`.
8. Open n8n, import the six workflows, set two variables, activate them.
9. In Meta, set the webhook URL to `https://your-domain/webhook` and the verify
   token to your `META_VERIFY_TOKEN`. Subscribe to `messages`.
10. Send one WhatsApp message from your own phone and watch it arrive.

Full commands are in `docs/DEPLOYMENT.md`. The pre-flight safety list is in
`docs/OPERATIONS.md` under "Going live checklist".

## Two business decisions I still need from you

These cannot be made by me, and they must be settled before real customers are
answered, because the AI must never invent a price or a bed.

1. **The room list.** The database currently holds the historic list you gave me:
   1NA (5 beds), 2AC (7), 3AC (10), 4NA (5), 6NA (12), 7NA (4), plus a 20-mat
   non-AC dormitory. Every row is marked unconfirmed. Is this still correct?

2. ~~The rates.~~ **Done.** You confirmed these on 2026-11-03 and they are now
   live in the database and quoted to customers:

   | Room | With food (night / day) | Without food (night / day) |
   |---|---:|---:|
   | AC | Rs 350 / Rs 450 | Rs 300 / Rs 350 |
   | Non-AC | Rs 250 / Rs 350 | Rs 200 / Rs 250 |
   | Dormitory | Rs 200 / Rs 300 | Rs 150 / Rs 200 |

   Extras: new bedsheet Rs 50, new towel Rs 100, new pillow Rs 50, locker Rs 30
   a month.

   Monthly (PG) stays:

   | Room | With food (night / full day) | Without food (night / full day) |
   |---|---:|---:|
   | AC | Rs 8,000 / Rs 11,000 | Rs 5,000 / Rs 7,000 |
   | Non-AC | Rs 5,500 / Rs 5,500 | Rs 4,000 / Rs 4,000 |
   | Dormitory | Rs 4,500 / Rs 4,500 | Rs 3,500 / Rs 3,500 |

   A student concession of Rs 5,000 a month applies to any room, but only for a
   student with a valid institution certificate or a rate you have agreed.

   The system never applies the concession by itself.

## The order I suggest

1. Confirm the room list and the rates. That is a two-minute reply from you, and
   nothing customer-facing can be correct without it.
2. Send the Meta credentials, written into `.env` on the server, not into chat.
3. Rent the server and point the domain at it.
4. I deploy, and we run one test message from your own phone.

Until step 4, the system stays in development, with dummy data and no real
customer contact.

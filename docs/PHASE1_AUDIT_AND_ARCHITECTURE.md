# Sagar PG AI Manager — Phase 1 Audit, Architecture and Plan

Prepared: 2026-11-03
Status: Phase 1 (Audit) complete. Phase 2 (Architecture) proposed, awaiting owner approval.

---

## 1. Existing setup audit

Everything below was actually inspected from this environment on 2026-11-03.
Nothing in this section is assumed.

### 1.1 Development environment (accessible, working)

| Resource | State | Evidence |
|---|---|---|
| Python 3.13.15, pip 26.2.2 | Working | `python3 --version` |
| Node 24.21.0, npm 11.21.0 | Working | `node --version` |
| Docker Engine 29.9.2 | Working via `sudo` (user is in `docker` group but the socket is root-owned; `sudo docker` succeeds) | `sudo docker run --rm alpine echo docker-ok` returned `docker-ok` |
| PostgreSQL 16.15 | Running locally in a dev container `sagarpg-pg` on port 5432 | `pg_isready` → accepting connections |
| Python stack | Installed and importable: fastapi 0.162.0, sqlalchemy 2.1.6, pydantic 2.14.2, psycopg, pytest 9.2.0, httpx | import check |
| Outbound internet | Working (api.github.com 200, pypi.org 200) | curl |
| Public HTTPS hostname | `https://hhxywwcgldhkwphf.prod-runtime.all-hands.dev` — this is the sandbox's own public URL. It is ephemeral and tied to this sandbox, so it is usable for a webhook pilot but is not a production host. | `RUNTIME_URL` |
| Working ports | 12000 and 12001 are published and currently free | local probe |

### 1.2 GitHub (accessible)

Authenticated as **`rrajareddy234-gif`**. Token has repo scope.

| Repository | Visibility | State |
|---|---|---|
| `rrajareddy234-gif/Shiva-sagar-hostel` | public | Created 2026-09-07. Description "Hostel". **Completely empty — no commits, no branches, no files (size 0 KB).** |
| `rrajareddy234-gif/Print-bot` | public | A 2-file Node/Express webhook stub: `Index.js` (a `/webhook` route that logs the body and returns 200) and `package.json`. Not a hostel system; unrelated. |
| `rrajareddy234-gif/Webwhatsup` | public | Empty (0 KB, no commits). |

**Access limitation found during this audit:** the GitHub token available to me can
**read** repositories but cannot **write**. Pushing to `Shiva-sagar-hostel` returns
`403 Permission to rrajareddy234-gif/Shiva-sagar-hostel.git denied`, and the contents
API returns `403 Resource not accessible by integration`. The token reports
`push: true` in repo permissions but the integration is not granted write. So I cannot
push commits or open pull requests on your behalf yet.

The work is committed locally and is safe. To get it into GitHub, either:

- grant the GitHub integration **Contents: read and write** permission, or
- paste the commit yourself, or
- tell me to use a personal access token that has `repo` scope.

I will keep every change as a local commit in the meantime, so nothing is lost.

### 1.3 The previously-built preview app (partially reachable)

`https://macherla-stay.preview.emergentagent.com` responds, but it is **not a usable running application**. It serves a fixed 2.3 KB "Loading..." HTML shell that embeds an iframe pointing at `https://app.emergent.sh/loading-preview?host=...`. Every path — `/`, `/docs`, `/openapi.json`, `/api/rooms` — returns that same shell or 404. There is no reachable API, no database, and no source code behind it. The Emergent preview is effectively dead; only the shell remains.

Re-checked 2026-11-03: unchanged. `/` returns the loading shell, `/api/health` returns 404. **Nothing to reuse.**

### 1.4 n8n (NOT accessible)

There is no n8n instance reachable from this environment:

- No n8n process running, nothing listening on any port.
- No n8n files, workflow JSON exports, or config anywhere on the filesystem.
- No n8n URL, API key, or credentials are available to me.
- The OpenHands automation API is reachable (0 automations configured), but that is a different product and is not your n8n.

**Conclusion: I cannot inspect, repair, or reuse your n8n workflows. I have no evidence any of them exist or work.** The claims about existing workflows, webhook verification, POST handling and recorded executions cannot be confirmed or denied from here. This is a genuine access gap, not something I will pretend to have inspected.

### 1.5 WhatsApp (NOT accessible)

No Meta app credentials, no App Secret, no access token, no Phone Number ID, no WhatsApp Business Account ID, and no webhook registration are available to me. I have not verified that any webhook receives messages, and I will not claim it does.

I also do not know what **"Peach for WhatsApp Business connected to ChatGPT"** refers to. I could not find a product by that exact name. This needs your clarification.

### 1.6 AI providers (partially accessible)

I have an internal OpenHands LLM endpoint available for my own reasoning, and I have verified that current commercial pricing is reachable for planning. I do **not** hold an OpenAI / Anthropic / Google API key belonging to you, so I cannot make live model calls on your behalf yet.

### 1.7 What the audit means in one line

The only real, usable assets are: this development environment, your GitHub account, and an empty `Shiva-sagar-hostel` repository. Everything else in the brief (n8n workflows, Meta app, the Emergent app) is either unreachable or empty, so the system must be built from a clean, well-structured foundation.

---

## 2. Missing access and permissions

I will ask for these **one at a time**, in this order, and only when the build actually reaches the point of needing each one.

| # | Needed | Why | Blocks |
|---|---|---|---|
| 1 | **Clarify "Peach for WhatsApp Business"** — which product/URL/account is it, and does it hold the WhatsApp connection? | Determines whether we use the official Cloud API directly or route through an existing provider | All WhatsApp work |
| 2 | **Meta WhatsApp Cloud API credentials** — App ID, App Secret, permanent System User token, WhatsApp Business Account ID, Phone Number ID | To send/receive real messages and verify webhook signatures | Live WhatsApp |
| 3 | **A dedicated business phone number** for WhatsApp (must not be an active WhatsApp account) | Cloud API registration | Live WhatsApp |
| 4 | **Your owner WhatsApp number** (and any staff numbers) | To authorise owner commands and send notifications | Owner assistant |
| 5 | **n8n access** — URL + login, *or* explicit approval to stand up a fresh self-hosted n8n | Orchestration, reminders, reports | Orchestration layer |
| 6 | **AI provider key** — a Google AI Studio (Gemini) key is recommended: free tier, cheapest paid tier, OpenAI-compatible | Live AI replies | AI layer |
| 7 | **Hosting decision + payment authorisation** (~₹350–600/month) for a small VPS | To run the always-on webhook | Production |
| 8 | **Business confirmations** — current room/bed configuration, current prices, dormitory arrangement, partner list | The system must not quote stale data | Pricing/availability |
| 9 | **GitHub preference** — build in the existing empty `Shiva-sagar-hostel` repo, or a new dedicated repo | Source control layout | Repo setup |

Nothing here requires you to write code. Items 1–4 and 8 are business/account facts only you can supply.

---

## 3. Recommended architecture

### 3.1 Principles

1. **Deterministic core, AI on the edges.** Availability, pricing, payments, date maths and booking rules are plain code and SQL. The AI only understands free-text and drafts replies. This is the single most important decision for booking accuracy.
2. **Provider-independent AI.** One adapter module with an OpenAI-compatible interface. Switching from Gemini to OpenAI to Claude to a local model is a config change, not a rewrite.
3. **No dashboard.** You operate by WhatsApp. The system exposes a small internal API that WhatsApp and n8n call.
4. **One box, Docker Compose.** Cheapest reliable option, trivially backed up.

### 3.2 Layers

```
WhatsApp (Meta Cloud API)
        |  inbound webhook (HTTPS, signed)
        v
[Caddy]  TLS termination, rate limit
        |
        v
[sagar-api]  FastAPI
   - X-Hub-Signature-256 verification
   - dedupe on WhatsApp message id (UNIQUE constraint)
   - persist + enqueue, return 200 fast
        |
        v
[core]  deterministic business logic (Python)
   - availability engine (bed-level, date/time aware)
   - booking state machine + owner approval
   - pricing / billing engine
   - payment ledger + verification
   - enquiry tracker + follow-up
   - notifications + reports
        |
        +--> [ai-adapter] -> Gemini / OpenAI / Claude / local (config-swappable)
        |
        v
[Postgres]  source of truth, transactional, row locks for booking
        ^
        |
[n8n]  cron: reminders, daily report, follow-up sweep, error alerts
```

### 3.3 Why this shape

- **n8n is orchestration, not the brain.** Putting availability logic in n8n nodes makes it hard to test and easy to double-book. The logic lives in tested code; n8n only schedules and routes.
- **Postgres over a spreadsheet or n8n data tables.** Booking safety needs real constraints and transactions.
- **AI kept cheap.** Routine intents (price, vacancy, dates) never touch a model.

### 3.4 Options compared

| Option | Monthly cost | Verdict |
|---|---|---|
| **A. Single small VPS, Docker Compose (Caddy + API + n8n + Postgres)** | **₹350–600** | **Recommended.** Cheapest reliable always-on setup. One backup target. |
| B. n8n Cloud Starter (€20/mo ≈ ₹1,850) + Supabase free + API on a small VPS | ₹1,850–2,400 | Least maintenance, but blows most of the budget on n8n alone. |
| C. Oracle Cloud Always Free ARM VM | ₹0 | Free and capable, but signup and capacity in India are unreliable. Good fallback, not a promise. |
| D. Supabase free + Render free API | ₹0 | **Rejected.** Both pause on inactivity; a paused webhook silently drops WhatsApp messages. |

**Database:** Supabase free tier (500 MB, pauses after 7 days idle) is fine only as an *offsite backup*, not as the primary store for a webhook that must never sleep. Primary = Postgres on the VPS. Your data volume is tiny (a few thousand rows/year).

---

## 4. Estimated monthly operating cost (INR)

> **Updated 2026-11-03.** The figures below were revised once the WhatsApp
> pricing change was confirmed: from 1 October 2026 free-form **service replies
> are billable** at ₹0.115 after the first 1,000 per number per month. The
> earlier version of this table assumed those replies were free, which
> understated the total. `docs/COST_REPORT.md` is the detailed version and is
> now the authoritative one.

Rates checked 2026-11-03. WhatsApp India per-message: service/utility/auth ≈ ₹0.115 (first 1,000/month free), marketing ≈ ₹0.8631, inbound free. Gemini 2.5 Flash ≈ $0.30/M input, $2.50/M output; Flash-Lite ≈ $0.10/$0.40. n8n Cloud Starter €20/mo. VPS ≈ €4.5/mo. USD→INR ≈ ₹88.

| Item | Low (≈300 msgs/mo) | Medium (≈1,500/mo) | High (≈5,000/mo) |
|---|---:|---:|---:|
| VPS (4 GB) + domain | ₹470 | ₹520 | ₹620 |
| Offsite backup | ₹0 | ₹0 | ₹0–100 |
| AI (Gemini Flash; ~30% of messages reach a model) | ₹6 | ₹29 | ₹98 |
| WhatsApp inbound | ₹0 | ₹0 | ₹0 |
| WhatsApp service replies (billable after 1,000) | ₹0 | ₹115 | ₹633 |
| Domain (amortised) | included above | included above | included above |
| n8n (self-hosted Community Edition) | ₹0 | ₹0 | ₹0 |
| **Total** | **≈ ₹476** | **≈ ₹664** | **≈ ₹1,351** |

All three scenarios are inside the ₹500–₹2,000 target, though the low case sits
just under it. WhatsApp message volume, not AI, is the cost to watch: it grows in
a straight line with the number of messages exchanged.

**Free:** inbound WhatsApp, the first 1,000 service messages each month, n8n
Community Edition, the Postgres software, the Gemini free tier for light use
(subject to the training-data caveat in `docs/COST_REPORT.md`), GitHub.

**Could grow:** marketing templates at ₹0.8631 each — seven times the service
rate. The system sends none. Service replies grow with conversation length; a
clear first reply is the cheapest control.

**One-time:** ₹0 for software. Domain ≈ ₹800/year. A VPS may need a card on file.

These are **estimates**, not charges. No purchase will be made without your explicit approval.

---

## 5. Implementation plan

| Phase | Work | Needs from you |
|---|---|---|
| **1. Audit** | Done. See §1. | — |
| **2. Architecture** | This document. | Approve, and answer the WhatsApp clarification (§2 item 1) |
| **3a. Core build (no external accounts needed)** | Postgres schema; availability engine; booking state machine; pricing/billing; payment ledger; enquiry tracker; AI adapter; FastAPI webhook with signature verification, dedupe, idempotency; pytest suite for every business rule | Nothing — I can do this now |
| **3b. Integration** | Wire Meta webhook; n8n workflows (receiver, owner approval, reminders, daily report, follow-up, error alerts); owner assistant commands; notifications | Meta credentials, n8n access, AI key |
| **4. Test** | Execute the full test matrix (§9 of the brief) with dummy data: WhatsApp, availability, booking, payment, security, reliability. Record test ID, input, expected, actual, pass/fail, fix. | Nothing — dummy data only |
| **5. Deployment prep** | Docker Compose, env config, HTTPS, backups, restore drill, rollback plan, monitoring, docs, AI-switch guide, cost report | Hosting approval |
| **6. Handover** | Final report: built / reused / repaired / tested, limitations, actions left, cost, AI switching, backup & restore | — |

**Rules I will follow:** no real customer messaging, no live payment changes, and no production switch without your explicit approval. All tests use dummy records.

---

## 6. First actual development task — DONE

**Build and test the deterministic core: database schema + availability + booking + pricing engines, with a full pytest suite running against real PostgreSQL.**

This is complete. The rest of this section is kept as the record of what was
planned; the results are in `docs/TEST_PLAN.md` and the final report.

Why this first: it is the highest-risk, highest-value part (a double-booking or a wrong price destroys trust), it needs no external accounts, and everything else — WhatsApp, n8n, AI — is just a caller of this core. When it is green, integration becomes assembly rather than guesswork.

Deliverables of this task:

- `db/schema.sql` — rooms, beds, guests, bookings, bed_holds, payments, enquiries, conversations, messages, audit_log, pricing, partners, settings.
- Bed-level availability query that respects date **and time**, and all nine bed statuses.
- Double-booking prevented by a database-level exclusion constraint (GiST + range), not by application hope.
- Booking state machine with owner approval and time-limited holds.
- Pricing engine with AC/non-AC, food/no-food, dormitory, hourly, discounts, advance/partial/refund.
- Payment ledger where a screenshot is never proof.
- Tests for overlapping reservations, simultaneous same-bed requests, hold expiry, cleaning/maintenance states, same-day checkout-and-arrival, and payment edge cases.

---

## 7. Open question (one, to unblock the most)

**What exactly is "Peach for WhatsApp Business"?** Please give me its URL or the company name, and tell me whether it is the thing that currently holds your WhatsApp number and connection to ChatGPT.

This decides whether we send messages through the official Meta Cloud API directly (recommended: cheapest, most reliable, no middleman fee) or keep an existing provider in the path. I will ask my remaining questions one at a time after that.

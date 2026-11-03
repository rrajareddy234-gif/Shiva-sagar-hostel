# Monthly cost report

All figures are estimates, not charges. Nothing has been purchased. Prices were
checked in November 2026 and providers change them; the exchange rate used is
₹88 to the US dollar.

## What each part costs

### 1. Software: free

| Item | Cost |
|------|------|
| This application (Python, FastAPI) | free, it is your own code |
| PostgreSQL 16 | free, open source |
| n8n Community Edition | free, self-hosted |
| Caddy (HTTPS) | free, certificates included |
| GitHub (private repository) | free |

### 2. Hosting: one small server

Everything runs on a single small server: PostgreSQL, the app and n8n.

| Option | Spec | Monthly |
|--------|------|---------|
| Hetzner CX22 | 2 vCPU, 4 GB, 40 GB | ~€4.5 ≈ ₹400 |
| DigitalOcean basic droplet | 2 vCPU, 4 GB | ~$24 ≈ ₹2,100 (overkill) |
| DigitalOcean basic droplet | 1 vCPU, 2 GB | ~$12 ≈ ₹1,060 |
| E2E Networks or similar, India | 2 vCPU, 4 GB | ~₹500–700 |

**Recommended:** the Hetzner CX22 class, about ₹400–600 per month. One server is
enough for a 63-bed hostel; the load is tiny.

Add about ₹70 per month for a domain name (₹800 per year).

### 3. AI: a few rupees, not a few hundred

The design keeps most questions away from the AI. Availability, prices, owner
questions and booking operations are all answered by plain code and SQL. The AI
is only used when a customer writes something the router does not recognise.

Assumptions: the AI is reached for about 30% of customer messages; each call
sends roughly 800 input tokens and returns 200 output tokens.

Using Gemini 2.5 Flash at $0.30 per million input and $2.50 per million output:

| Usage | Customer messages / month | AI calls | AI cost |
|-------|---------------------------|----------|---------|
| Low | 300 | 90 | ₹6 |
| Medium | 1,500 | 450 | ₹29 |
| High | 5,000 | 1,500 | ₹98 |

Cheaper still: Gemini 2.5 Flash-Lite is about a third of that, and the Gemini
free tier costs nothing at all for low volume.

**A caution on the free tier.** Google may use free-tier inputs and outputs to
improve its models. Customer names and phone numbers would be part of that. If
that matters to you, use the paid tier, which is excluded from training. At these
volumes the paid tier costs under ₹100 a month, so this is a small price for
privacy.

### 4. WhatsApp: the largest variable cost

Meta changed the rules on 1 October 2026. Free-form replies inside the 24-hour
customer service window are no longer free. The Indian rates now are:

| Category | Rate | Notes |
|----------|------|-------|
| Service (our replies) | ₹0.115 | first 1,000 per month free, per number |
| Utility | ₹0.115 | first 1,000 free |
| Marketing | ₹0.8631 | avoid entirely; we send none |
| Authentication | ₹0.115 | not used |

Every customer message usually gets one reply, plus owner notifications.

| Usage | Messages in | Messages out | Billable after the free 1,000 | WhatsApp cost |
|-------|-------------|--------------|-------------------------------|---------------|
| Low | 300 | 400 | 0 | ₹0 |
| Medium | 1,500 | 2,000 | 1,000 | ₹115 |
| High | 5,000 | 6,500 | 5,500 | ₹633 |

**This is the cost to watch.** It grows with the number of messages, not with the
number of beds. If enquiries are answered in one or two messages rather than five,
the bill stays low. The system does not send marketing messages and does not
chase customers repeatedly, which keeps the rate at ₹0.115 rather than ₹0.8631.

## Total per month

| Usage | Hosting and domain | AI | WhatsApp | **Total** |
|-------|--------------------|----|----------|-----------|
| Low | ₹470 | ₹6 | ₹0 | **₹476** |
| Medium | ₹520 | ₹29 | ₹115 | **₹664** |
| High | ₹620 | ₹98 | ₹633 | **₹1,351** |

All three scenarios are inside the ₹500–₹2,000 target. The low case sits just
under it; a slightly larger server moves it comfortably inside.

## One-time costs

| Item | Cost |
|------|------|
| Domain name, first year | ₹800 |
| Everything else | ₹0 — no licences, no setup fees |

No purchases are needed to start. If you prefer not to run a server at all, the
alternative is n8n Cloud at roughly ₹1,900 per month, which uses most of the
budget by itself.

## What is free

- All software in this project.
- The Gemini free tier, at low volume, with the privacy caveat above.
- The first 1,000 WhatsApp service messages each month.
- HTTPS certificates.
- The GitHub private repository.

## What could become expensive

1. **Message volume.** If enquiries turn into long back-and-forth conversations,
   the WhatsApp bill grows in a straight line. At 20,000 messages a month the
   bill is roughly ₹2,200, over budget. The fix is a clearer first reply, not a
   cheaper plan.
2. **Marketing messages.** At ₹0.8631 each, seven times the service rate. The
   system sends none, and should keep sending none without your explicit decision.
3. **AI model choice.** A top-tier model such as Gemini Pro or Claude Sonnet is
   twenty to fifty times the cost of Flash. The model is one line of configuration
   (`AI_MODEL`), so a costly model would be a deliberate change, not an accident.
4. **Storage.** Payment screenshots and documents, if you ever store them, grow
   without limit. Not built yet.
5. **Paid AI with no spending cap.** Set a billing limit in the provider's console
   as well as watching the in-app cost report. Two independent brakes.

## What is not included

- Your time. Setup is a few hours, and I am doing the technical work.
- GST or other tax on the WhatsApp charges.
- A separate development or staging server. The same server can host both, which
  is why the estimate above assumes one.

## How to check the real figures

The application records every AI call with its token count and estimated cost:

```sql
SELECT date_trunc('month', at) AS month,
       count(*) AS calls,
       round(sum(cost_estimate), 4) AS estimated_usd
FROM ai_usage
GROUP BY 1 ORDER BY 1 DESC;
```

The owner can also ask on WhatsApp: *"what is the AI cost this month?"* The answer
comes from that table, not from a guess.

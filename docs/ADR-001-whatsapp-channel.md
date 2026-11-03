# ADR-001: WhatsApp channel — direct Meta Cloud API vs Peach

Status: **Decided** (recommendation made by the agent; no business decision required)
Date: 2026-11-03

## Decision

**Use the official Meta WhatsApp Cloud API directly (Option A). Peach is not
required and is not recommended for this system.**

The channel is implemented behind a provider-independent `ChannelAdapter`
interface (`core/channel/`), so Peach can be added or removed later as a
configuration change, without rebuilding anything.

The new system runs on its **own dedicated WhatsApp number with its own Meta app**.
The existing Peach integration is left completely untouched.

## What Peach is (verified)

Peach (`trypeach.ai`) is an AI-native WhatsApp Business platform and a verified
Meta Technology Partner (a BSP). It provides a shared inbox, AI agents, broadcasts,
an MCP server, and a verified n8n community node (`n8n-nodes-peach`). It is built on
the official WhatsApp Business API. Published pricing: Free ($0), Essentials ($69/mo),
Growing ($149/mo), Scaling ($299/mo), Enterprise (custom). Peach passes Meta's message
rates through with no markup.

So Peach is a legitimate, competent product. The question is not quality. The question
is whether it adds anything **this** system needs.

## The one technical fact that decides it

**A WhatsApp number is fed to exactly one webhook, through one Meta app.**

Meta allows one webhook endpoint per App, and a phone number is subscribed through a
single WABA-to-app subscription. Two apps on the same number cause Meta to deliver the
same message to both webhooks — a well-documented cause of duplicate processing
(Meta developer community threads; Hookdeck's WhatsApp webhook guide: "use a single
Meta app per phone number to avoid duplicate subscriptions"; "redundant webhook
triggers in third-party BSPs can multiply the problem").

Consequences:

- You cannot have Peach **and** your own app both receiving the same number's messages
  without duplicate replies — which your instructions explicitly forbid.
- Peach's own AI agents would be a competing automatic reply system on the same number
  — also forbidden.
- If Peach currently holds your number, using the Cloud API directly on that same
  number means migrating the number out of Peach. That would be "disconnecting Peach",
  which you told me not to do.

Therefore: **the new system gets a separate dedicated number.** Peach keeps its number.
Nothing is disconnected, nothing competes, no duplicates.

## Comparison across every dimension you asked about

| Dimension | A. Direct Cloud API | B. Cloud API **+** Peach | C. Peach instead of Cloud API |
|---|---|---|---|
| **Inbound messages** | Webhook to our endpoint. Full control. ✅ | Impossible cleanly: one number → one webhook. Peach and our app would both need the same number → duplicates. ❌ | Works via Peach webhooks. ✅ |
| **Outbound messages** | Graph API send endpoint. ✅ | Two senders on one number → risk of double replies. ❌ | Peach API / node. ✅ |
| **Webhooks / real-time** | We own the endpoint, verify `X-Hub-Signature-256`, dedupe on `wamid`. ✅ | Two competing listeners. ❌ | Peach owns the endpoint; we depend on Peach's webhook fan-out. ⚠️ |
| **Conversation history** | Stored in our Postgres, ours to query. ✅ | Split across two systems. ❌ | Lives in Peach; we only get what their API exposes. ⚠️ |
| **Automated AI replies** | Our adapter, any model, deterministic core decides facts. ✅ | Peach agents would compete with ours. ❌ | Peach agents would replace our deterministic core — the thing we must NOT do. ❌ |
| **Owner communication** | Same number, our command router, role-checked. ✅ | Duplicate owner notifications. ❌ | Same number, but routed through Peach's agent layer. ⚠️ |
| **Booking / bed availability** | Direct to our Postgres + exclusion constraint. ✅ | Same, but inbound path is contested. ❌ | Must integrate Peach ↔ our DB; extra hop and failure mode. ⚠️ |
| **Notifications** | Template messages via Graph API. ✅ | Fine in principle, but on the contested number. ❌ | Via Peach; templates managed in Peach. ⚠️ |
| **Reliability / duplicate prevention** | `wa_message_id UNIQUE` in Postgres + idempotent handler. Single listener. ✅ | Multiple Meta apps on one number is a known duplicate-generator. ❌ | Depends on Peach's dedupe, not ours. ⚠️ |
| **Monthly platform fee** | **₹0** | ₹0 + Peach **$69/mo ≈ ₹6,350** | Peach **$69/mo ≈ ₹6,350** (or Free tier with 2× AI credit pricing and 1 seat) |
| **Additional API cost** | Meta rates only | Meta rates + Peach subscription | Meta rates + Peach subscription + AI credits |
| **Vendor dependency** | Meta only; adapter is swappable. ✅ | Two vendors, contested number. ❌ | **High** — the number lives inside Peach; leaving means a number migration that can affect the number's history and quality rating. ❌ |
| **Fits your stated rules** | Yes | **No** — violates "no duplicate webhook listeners or competing auto-reply systems" | Yes, but adds a paid layer and lock-in for no benefit |

## Why B is rejected outright

Option B is not merely suboptimal; it breaks two of your explicit rules (no duplicate
webhook listeners, no competing auto-reply systems) and is technically fragile. It also
costs ₹6,350/month on top of everything else — roughly 5–15× the entire budget — for a
capability we are deliberately not using.

## Why C is not needed

Peach's value is its AI agent layer and shared inbox. This system deliberately keeps AI
away from availability, pricing and booking facts, because a hallucinated bed or price
is the failure you most want to avoid. So Peach's main feature is one we would switch
off. We would pay ₹6,350/month for a layer we do not use, and hand it control of the
phone number. That is a bad trade.

The only thing Peach offers that we do want — an n8n node — is unnecessary, because the
Meta Graph API is a plain HTTPS call that n8n's built-in HTTP Request node handles
directly.

## What "keep it modular" means concretely

`core/channel/adapter.py` defines a small interface:

```
ChannelAdapter
  verify_inbound(raw_body, headers) -> bool
  parse_inbound(raw_body) -> list[InboundMessage]
  send_text(to, text) -> SendResult
  send_template(to, name, params) -> SendResult
```

- `MetaCloudAdapter` — the default, implemented.
- `PeachAdapter` — a documented stub. Adding it later means implementing four methods
  and changing one config value. Removing it means changing that value back.

The core business logic imports the interface, never a provider. That is the whole
point: provider choice is a config line, not an architecture.

## Costs

| | Option A (chosen) | Option C (Peach Essentials) |
|---|---:|---:|
| Platform fee | ₹0 | ₹6,350 ($69) |
| Meta messages (medium usage, ~500 enquiries) | ≈ ₹60 | ≈ ₹60 |
| AI credits | pay-as-you-go, ≈ ₹60 | 500 included, then extra |
| **Monthly total** | **≈ ₹510 (with hosting)** | **≈ ₹6,900+** |

Option A keeps the whole system inside your ₹500–₹2,000 budget. Option C does not.

## Constraints respected

- Peach is **not** purchased, upgraded, or disconnected.
- Your Meta / WhatsApp configuration is **not** changed.
- No real customer messages are sent.
- No duplicate webhook listeners and no competing auto-reply systems are created — the
  new system uses a separate number with a single app and a single webhook.
- Modular design so Peach can be added or removed later without a rebuild.

## What this needs from you (later, at integration time only)

One dedicated phone number that is **not** the number currently on Peach, plus a Meta
app on it. That is the only business input this decision requires, and it is needed at
Phase 3b, not now.

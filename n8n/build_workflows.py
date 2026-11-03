"""Build the n8n workflow JSON files.

The workflows are deliberately thin. All the logic lives in the Python
application, where it is tested. n8n does three things only: run on a schedule,
call an endpoint, and forward a WhatsApp message to the app.

Run this script to regenerate the JSON:

    python n8n/build_workflows.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent

# The app the workflows call. Set SAGAR_APP_URL and SAGAR_JOB_TOKEN in n8n's
# Variables, or edit the values here and re-run.
APP = "={{ $vars.SAGAR_APP_URL }}"
TOKEN_HEADER = "={{ $vars.SAGAR_JOB_TOKEN }}"


def _schedule(name: str, cron: str, path: str, note: str) -> dict:
    """A workflow that calls one endpoint on a schedule."""
    return {
        "name": name,
        "nodes": [
            {
                "parameters": {
                    "rule": {"interval": [{"field": "cronExpression", "expression": cron}]}
                },
                "id": "schedule",
                "name": "Schedule",
                "type": "n8n-nodes-base.scheduleTrigger",
                "typeVersion": 1.2,
                "position": [220, 300],
            },
            {
                "parameters": {
                    "url": f"{APP}{path}",
                    "options": {"timeout": 30000},
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [{"name": "X-Job-Token", "value": TOKEN_HEADER}]
                    },
                },
                "id": "fetch",
                "name": "Fetch from app",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [460, 300],
                "onError": "continueRegularOutput",
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 5000,
            },
            {
                "parameters": {
                    "jsCode": (
                        "// Report to the owner. If the fetch failed, say so plainly.\n"
                        "const item = $input.first().json;\n"
                        f"// {note}\n"
                        "if (item.error || item.ok === false) {\n"
                        "  const detail = item.problems ? item.problems.join('; ')\n"
                        "    : (item.error ? JSON.stringify(item.error) : 'unknown error');\n"
                        "  return [{ json: { text: `Sagar PG alert: ${detail}` } }];\n"
                        "}\n"
                        "return [{ json: { text: item.text || item.summary || "
                        "JSON.stringify(item, null, 2) } }];\n"
                    )
                },
                "id": "format",
                "name": "Format the message",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [700, 300],
            },
            {
                "parameters": {
                    "method": "POST",
                    "url": f"{APP}/jobs/notify-owner",
                    "sendHeaders": True,
                    "headerParameters": {
                        "parameters": [
                            {"name": "X-Job-Token", "value": TOKEN_HEADER},
                            {"name": "Content-Type", "value": "application/json"},
                        ]
                    },
                    "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": "={{ JSON.stringify({ text: $json.text }) }}",
                    "options": {"timeout": 30000},
                },
                "id": "notify",
                "name": "WhatsApp the owner",
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.2,
                "position": [940, 300],
                "onError": "continueRegularOutput",
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 10000,
            },
        ],
        "connections": {
            "Schedule": {"main": [[{"node": "Fetch from app", "type": "main", "index": 0}]]},
            "Fetch from app": {"main": [[{"node": "Format the message", "type": "main", "index": 0}]]},
            "Format the message": {"main": [[{"node": "WhatsApp the owner", "type": "main", "index": 0}]]},
        },
        "settings": {"executionOrder": "v1", "saveExecutionProgress": True},
        "tags": [{"name": "sagar-pg"}],
        "active": False,
    }


INBOUND = {
    "name": "Sagar PG - WhatsApp inbound",
    "nodes": [
        {
            "parameters": {
                "httpMethod": "POST",
                "path": "sagar-whatsapp",
                "responseMode": "responseNode",
                "options": {},
            },
            "id": "webhook",
            "name": "WhatsApp webhook",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": [220, 300],
            "webhookId": "sagar-whatsapp",
        },
        {
            "parameters": {
                "jsCode": (
                    "// Forward the webhook to the app, unchanged.\n"
                    "// The app checks the signature, deduplicates and replies.\n"
                    "// The body must not be rewritten: the signature covers the raw bytes.\n"
                    "const item = $input.first().json;\n"
                    "const body = item.body ?? item;\n"
                    "const raw = typeof body === 'string' ? body : JSON.stringify(body);\n"
                    "const headers = item.headers || {};\n"
                    "return [{ json: { raw, signature: headers['x-hub-signature-256'] || '' } }];\n"
                )
            },
            "id": "passthrough",
            "name": "Keep the raw body",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [460, 300],
        },
        {
            "parameters": {
                "method": "POST",
                "url": f"{APP}/webhook",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "X-Hub-Signature-256", "value": "={{ $json.signature }}"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ $json.raw }}",
                "options": {"timeout": 20000},
            },
            "id": "forward",
            "name": "Forward to the app",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [700, 300],
            "retryOnFail": True,
            "maxTries": 2,
            "waitBetweenTries": 3000,
        },
        {
            "parameters": {
                "respondWith": "text",
                "responseBody": "ok",
                "options": {"responseCode": 200},
            },
            "id": "respond",
            "name": "Acknowledge Meta",
            "type": "n8n-nodes-base.respondToWebhook",
            "typeVersion": 1,
            "position": [940, 300],
        },
    ],
    "connections": {
        "WhatsApp webhook": {"main": [[{"node": "Keep the raw body", "type": "main", "index": 0}]]},
        "Keep the raw body": {"main": [[{"node": "Forward to the app", "type": "main", "index": 0}]]},
        "Forward to the app": {"main": [[{"node": "Acknowledge Meta", "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1", "saveExecutionProgress": True},
    "tags": [{"name": "sagar-pg"}],
    "active": False,
}


DAILY_REPORT = _schedule(
    "Sagar PG - daily report",
    "30 7 * * *",           # 07:30 IST
    "/jobs/daily-report",
    "Daily figures.",
)
DAILY_REPORT["nodes"][2]["parameters"]["jsCode"] = (
    "const r = $input.first().json;\n"
    "if (r.error) {\n"
    "  return [{ json: { text: 'Sagar PG: the daily report could not be built. '\n"
    "    + 'Please check the app.' } }];\n"
    "}\n"
    "const lines = [\n"
    "  'Sagar PG, morning report',\n"
    "  `Beds free: ${r.beds.available} of ${r.beds.total}`,\n"
    "  `Occupied: ${r.beds.occupied}, blocked: ${r.beds.blocked}`,\n"
    "  `Arrivals today: ${r.arrivals_today}, departures: ${r.departures_today}`,\n"
    "  `Overdue checkouts: ${r.overdue_checkouts}`,\n"
    "  `Booking requests waiting: ${r.pending_requests}`,\n"
    "  `Payments to verify: ${r.payments_awaiting_verification}`,\n"
    "  `Outstanding balances: ${r.outstanding_balances}`,\n"
    "  `New enquiries today: ${r.enquiries_today}, unanswered: ${r.unanswered_enquiries}`,\n"
    "  `Collected today: Rs ${r.collected_today}`,\n"
    "  `Revenue this month: Rs ${r.revenue_this_month}`,\n"
    "];\n"
    "if (r.beds.full_rooms && r.beds.full_rooms.length) {\n"
    "  lines.push(`Full rooms: ${r.beds.full_rooms.join(', ')}`);\n"
    "}\n"
    "return [{ json: { text: lines.join('\\n') } }];\n"
)


def _build() -> list[dict]:
    return [
        INBOUND,
        DAILY_REPORT,
        _schedule("Sagar PG - unanswered enquiries", "*/30 * * * *",
                  "/jobs/unanswered-enquiries",
                  "Unanswered enquiries, at most every 30 minutes."),
        _schedule("Sagar PG - upcoming arrivals and departures", "0 20 * * *",
                  "/jobs/upcoming",
                  "Tomorrow's arrivals and today's departures, each evening."),
        _schedule("Sagar PG - pending booking requests", "*/15 * * * *",
                  "/jobs/pending-requests",
                  "Booking requests still waiting for the owner."),
        _schedule("Sagar PG - health check", "*/10 * * * *",
                  "/jobs/health-check",
                  "Reports only when something is wrong."),
    ]


def _slug(name: str) -> str:
    return (name.lower().replace("sagar pg - ", "")
            .replace(" ", "-").replace(",", "").replace("---", "-"))


def main() -> None:
    for workflow in _build():
        path = OUT / f"{_slug(workflow['name'])}.json"
        path.write_text(json.dumps(workflow, indent=2) + "\n")
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()

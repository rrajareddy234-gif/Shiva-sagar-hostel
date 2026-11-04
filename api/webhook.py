"""WhatsApp webhook endpoint.

Responsibilities, in order:
  1. Answer the GET verification handshake.
  2. Verify the POST signature. Reject anything unsigned or tampered.
  3. Store each inbound message exactly once (dedupe on the provider id).
  4. Return 200 quickly. Meta retries anything that is slow or fails, and a
     retry must never produce a second reply or a second booking.

Processing happens in a background task after the response is sent, so a slow
AI call can never make Meta retry the delivery.
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from api import dashboard, jobs
from core.channel.registry import build_channel
from core.config import Settings
from core.db import Database
from core.dispatcher import build_ai, dispatch
from core.inbox import store_inbound

log = logging.getLogger(__name__)


def create_app(settings: Settings, *, channel=None, ai=None) -> FastAPI:
    """Build the app. `channel` and `ai` may be injected for tests."""
    app = FastAPI(title="Sagar PG AI Manager", docs_url=None, redoc_url=None)

    db = Database(settings.database_url)
    if channel is None:
        channel = build_channel(settings.channel_provider, **settings.channel_credentials())
    if ai is None:
        ai = build_ai(settings)

    app.state.settings = settings
    app.state.db = db
    app.state.channel = channel
    app.state.ai = ai

    app.include_router(jobs.router)
    app.include_router(dashboard.router)

    @app.get("/health")
    def health() -> dict:
        try:
            db.query_one("SELECT 1 AS ok")
            return {"status": "ok", "database": "ok", "channel": channel.name}
        except Exception as exc:  # pragma: no cover - depends on live database
            log.error("health check failed: %s", exc)
            return JSONResponse({"status": "degraded", "database": "error"}, status_code=503)

    @app.get("/webhook")
    def verify(request: Request) -> Response:
        """Meta's subscription handshake."""
        challenge = channel.verify_challenge(dict(request.query_params))
        if challenge is None:
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(challenge)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks) -> Response:
        raw = await request.body()

        if not channel.verify_inbound(raw, dict(request.headers)):
            log.warning("rejected an unverified webhook request")
            return PlainTextResponse("invalid signature", status_code=403)

        try:
            messages = channel.parse_inbound(raw)
        except Exception as exc:
            # A parse failure must still return 200 so Meta does not retry forever.
            log.error("failed to parse a verified webhook payload: %s", exc)
            return JSONResponse({"status": "ignored"}, status_code=200)

        for message in messages:
            stored = store_inbound(db, message)
            if stored is None:
                log.info("ignored a duplicate delivery of %s", message.provider_message_id)
                continue
            background.add_task(dispatch, db, channel, settings, message, ai,
                                stored.message_id)

        return JSONResponse({"status": "received"}, status_code=200)

    return app

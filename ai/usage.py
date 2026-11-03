"""Record token usage and estimated AI cost, so spend is never a surprise."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ai.adapter import AIResponse
from ai.registry import estimate_cost
from core.db import Database

# AI costs are fractions of a rupee, so they are kept to six decimal places.
# Rounding to two places would report every small call as zero.
SIX_PLACES = Decimal("0.000001")


def record_usage(db: Database, response: AIResponse, *, purpose: str,
                 conversation_id: str | None = None,
                 provider: str | None = None) -> Decimal:
    provider_name = provider or response.provider or "unknown"
    cost = estimate_cost(provider_name, response.input_tokens, response.output_tokens)
    db.execute(
        """
        INSERT INTO ai_usage (provider, model, purpose, input_tokens, output_tokens,
                              cost_estimate, conversation_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (provider_name, response.model or "unknown", purpose,
         response.input_tokens, response.output_tokens, cost, conversation_id),
    )
    return cost


def cost_between(db: Database, start: datetime, end: datetime) -> dict:
    row = db.query_one(
        """
        SELECT COALESCE(SUM(cost_estimate), 0) AS cost,
               COALESCE(SUM(input_tokens), 0)  AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               count(*)                        AS calls
        FROM ai_usage WHERE at >= %s AND at < %s
        """,
        (start, end),
    )
    return {
        "cost_usd": (row["cost"] or Decimal("0")).quantize(SIX_PLACES),
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "calls": row["calls"],
    }

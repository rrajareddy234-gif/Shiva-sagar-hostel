"""AI provider selection and published price table.

Switching provider or model is one config change. Prices below are per million
tokens in USD, used only to estimate spend for the owner's cost report. Verify
them against the provider's current pricing before relying on a figure.
"""

from __future__ import annotations

from decimal import Decimal

from ai.adapter import AIClient
from ai.gemini import GeminiClient
from ai.openai_compatible import OpenAICompatibleClient

# provider -> (input price, output price) per million tokens, USD.
# Estimates for the owner's cost report, not billing records.
#
# These are list prices for the cheap model each provider is normally used with,
# checked in November 2026. Verify before relying on a figure; providers change
# prices. The model name is stored alongside every call, so a later report can be
# recalculated with corrected prices.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "gemini": (Decimal("0.30"), Decimal("2.50")),          # gemini-2.5-flash, paid tier
    "gemini_flash_lite": (Decimal("0.10"), Decimal("0.40")),  # gemini-2.5-flash-lite
    "gemini_free": (Decimal("0"), Decimal("0")),           # free tier, rate limited
    "openai": (Decimal("0.15"), Decimal("0.60")),          # gpt-4o-mini class
    "openai_compatible": (Decimal("0.15"), Decimal("0.60")),
    "groq": (Decimal("0.05"), Decimal("0.08")),            # llama-3.x on Groq
    "openrouter": (Decimal("0.10"), Decimal("0.40")),
    "ollama": (Decimal("0"), Decimal("0")),                # local, electricity only
    "anthropic": (Decimal("1.00"), Decimal("5.00")),       # claude haiku class
}

# Providers reached through the OpenAI-compatible chat-completions shape.
_OPENAI_STYLE = {"openai", "openrouter", "groq", "together", "ollama", "local"}


def build_ai_client(provider: str, *, model: str, api_key: str | None = None,
                    base_url: str | None = None) -> AIClient:
    if provider == "gemini":
        return GeminiClient(api_key=api_key or "", model=model)
    if provider in _OPENAI_STYLE:
        return OpenAICompatibleClient(api_key=api_key or "", model=model,
                                      base_url=base_url, provider_name=provider)
    raise ValueError(
        f"unknown AI_PROVIDER {provider!r}; add it to ai/registry.py "
        f"(known: gemini, {', '.join(sorted(_OPENAI_STYLE))})"
    )


def estimate_cost(provider: str, input_tokens: int, output_tokens: int) -> Decimal:
    input_price, output_price = PRICES.get(provider, (Decimal("0"), Decimal("0")))
    million = Decimal("1000000")
    return ((Decimal(input_tokens) * input_price
             + Decimal(output_tokens) * output_price) / million)

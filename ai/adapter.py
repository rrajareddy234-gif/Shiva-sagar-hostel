"""AI provider interface.

Business logic never imports a concrete AI provider. It asks for `AIClient` and
gets whichever provider `AI_PROVIDER` selects. Changing model or provider is a
config change, not a rewrite.

Prompts live in `ai/prompts.py`, never in this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AIResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""

    def cost_estimate(self, input_price: Decimal, output_price: Decimal) -> Decimal:
        """Cost in USD per million tokens, given the provider's published prices."""
        million = Decimal("1000000")
        return ((Decimal(self.input_tokens) * input_price
                 + Decimal(self.output_tokens) * output_price) / million)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


class AIClient(ABC):
    """A chat model that can optionally call tools."""

    provider: str = "unknown"
    model: str = ""

    @abstractmethod
    def complete(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[ToolSpec] | None = None,
                 max_tokens: int = 1024) -> AIResponse:
        """Send a conversation and return the model's reply."""


class AIUnavailable(Exception):
    """The provider could not be reached or refused the request."""

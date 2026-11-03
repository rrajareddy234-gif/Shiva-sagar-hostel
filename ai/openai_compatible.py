"""OpenAI-compatible provider.

One implementation covers OpenAI, OpenRouter, Groq, Together and a local
Ollama or vLLM server, because they all speak the same chat-completions shape.
Point `AI_BASE_URL` at whichever you choose.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ai.adapter import AIClient, AIResponse, AIUnavailable, ToolCall, ToolSpec

log = logging.getLogger(__name__)

DEFAULT_BASE = "https://api.openai.com/v1"


class OpenAICompatibleClient(AIClient):
    provider = "openai_compatible"

    def __init__(self, *, api_key: str, model: str,
                 base_url: str | None = None, timeout: int = 30,
                 provider_name: str | None = None):
        if not api_key:
            raise ValueError("this provider requires an API key")
        self._api_key = api_key
        self.model = model
        self._base = (base_url or DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        if provider_name:
            self.provider = provider_name

    def complete(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[ToolSpec] | None = None,
                 max_tokens: int = 1024) -> AIResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "messages": [{"role": "system", "content": system}] + [
                _to_openai(m) for m in messages
            ],
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }} for t in tools]

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                parsed = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            log.error("%s error %s: %s", self.provider, exc.code, detail)
            raise AIUnavailable(f"{self.provider} returned HTTP {exc.code}") from exc
        except Exception as exc:
            log.error("%s request failed: %s", self.provider, exc)
            raise AIUnavailable(str(exc)) from exc

        return _parse(parsed, self.model, self.provider)


def _to_openai(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("tool_result") is not None:
        return {
            "role": "tool",
            "tool_call_id": message.get("tool_call_id", ""),
            "content": json.dumps(message["tool_result"], default=str),
        }
    out: dict[str, Any] = {"role": message.get("role", "user"),
                           "content": message.get("content") or ""}
    if message.get("tool_calls"):
        out["tool_calls"] = [
            {"id": c.get("id", f"call_{i}"), "type": "function",
             "function": {"name": c["name"],
                          "arguments": json.dumps(c.get("arguments", {}))}}
            for i, c in enumerate(message["tool_calls"])
        ]
    return out


def _parse(parsed: dict[str, Any], model: str, provider: str) -> AIResponse:
    choices = parsed.get("choices") or []
    if not choices:
        return AIResponse(text="", model=model, provider=provider)

    message = choices[0].get("message") or {}
    calls = [
        ToolCall(name=(c.get("function") or {}).get("name", ""),
                 arguments=_loads((c.get("function") or {}).get("arguments")))
        for c in message.get("tool_calls") or []
    ]
    usage = parsed.get("usage") or {}
    return AIResponse(
        text=(message.get("content") or "").strip(),
        tool_calls=calls,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        model=model,
        provider=provider,
    )


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}

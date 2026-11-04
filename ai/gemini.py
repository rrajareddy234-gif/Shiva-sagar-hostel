"""Google Gemini provider, over the plain REST API.

No SDK dependency, so there is nothing to keep in step with a vendor package.
Gemini has a free tier that suits low volume, which is why it is the default.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ai.adapter import AIClient, AIResponse, AIUnavailable, ToolCall, ToolSpec

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiClient(AIClient):
    provider = "gemini"

    def __init__(self, *, api_key: str, model: str = "gemini-3.5-flash",
                 timeout: int = 30):
        if not api_key:
            raise ValueError("Gemini requires an API key")
        self._api_key = api_key
        self.model = model
        self._timeout = timeout

    def complete(self, *, system: str, messages: list[dict[str, Any]],
                 tools: list[ToolSpec] | None = None,
                 max_tokens: int = 1024) -> AIResponse:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [_to_gemini(m) for m in messages],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3},
        }
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ]}]

        url = f"{BASE}/models/{self.model}:generateContent?key={self._api_key}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                parsed = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            log.error("gemini error %s: %s", exc.code, detail)
            raise AIUnavailable(f"Gemini returned HTTP {exc.code}") from exc
        except Exception as exc:
            log.error("gemini request failed: %s", exc)
            raise AIUnavailable(str(exc)) from exc

        return _parse(parsed, self.model)


def _to_gemini(message: dict[str, Any]) -> dict[str, Any]:
    role = "model" if message.get("role") == "assistant" else "user"
    parts: list[dict[str, Any]] = []
    if message.get("content"):
        parts.append({"text": message["content"]})
    for call in message.get("tool_calls") or []:
        parts.append({"functionCall": {"name": call["name"], "args": call.get("arguments", {})}})
    if message.get("tool_result") is not None:
        parts.append({"functionResponse": {
            "name": message["tool_name"],
            "response": {"result": message["tool_result"]},
        }})
    return {"role": role, "parts": parts or [{"text": ""}]}


def _parse(parsed: dict[str, Any], model: str) -> AIResponse:
    candidates = parsed.get("candidates") or []
    if not candidates:
        blocked = (parsed.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise AIUnavailable(f"Gemini blocked the request: {blocked}")
        return AIResponse(text="", model=model, provider="gemini")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in parts:
        if "text" in part:
            texts.append(part["text"])
        elif "functionCall" in part:
            call = part["functionCall"]
            calls.append(ToolCall(name=call.get("name", ""), arguments=call.get("args") or {}))

    usage = parsed.get("usageMetadata") or {}
    return AIResponse(
        text="\n".join(t for t in texts if t).strip(),
        tool_calls=calls,
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
        model=model,
        provider="gemini",
    )

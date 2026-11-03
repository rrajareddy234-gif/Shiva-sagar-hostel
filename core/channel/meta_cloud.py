"""Official Meta WhatsApp Cloud API channel adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from core.channel.adapter import ChannelAdapter, InboundMessage, SendResult

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"


class MetaCloudAdapter(ChannelAdapter):
    name = "meta_cloud"

    def __init__(self, *, app_secret: str, access_token: str,
                 phone_number_id: str, verify_token: str):
        if not app_secret or not access_token or not phone_number_id:
            raise ValueError("Meta adapter requires app_secret, access_token and phone_number_id")
        self._app_secret = app_secret
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._verify_token = verify_token

    # -- inbound ----------------------------------------------------------
    def verify_inbound(self, raw_body: bytes, headers: dict[str, str]) -> bool:
        """Verify the X-Hub-Signature-256 HMAC. Returns False on any problem."""
        sig_header = None
        for key, value in headers.items():
            if key.lower() == "x-hub-signature-256":
                sig_header = value
                break
        if not sig_header or not sig_header.startswith("sha256="):
            return False
        expected = hmac.new(self._app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        provided = sig_header.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    def verify_challenge(self, query: dict[str, str]) -> str | None:
        if query.get("hub.mode") == "subscribe" and query.get("hub.verify_token") == self._verify_token:
            return query.get("hub.challenge")
        return None

    def parse_inbound(self, raw_body: bytes) -> list[InboundMessage]:
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.warning("meta: unparseable webhook body")
            return []

        out: list[InboundMessage] = []
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    msg_type = msg.get("type", "unknown")
                    text = None
                    if msg_type == "text":
                        text = (msg.get("text") or {}).get("body")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive") or {}
                        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                        text = reply.get("title") or reply.get("id")
                    out.append(InboundMessage(
                        provider_message_id=msg.get("id", ""),
                        from_phone=_normalise_phone(msg.get("from", "")),
                        text=text,
                        message_type=msg_type,
                        timestamp=int(msg["timestamp"]) if msg.get("timestamp") else None,
                        raw=msg,
                    ))
        return out

    # -- outbound ---------------------------------------------------------
    def send_text(self, to_phone: str, text: str) -> SendResult:
        body = {
            "messaging_product": "whatsapp",
            "to": _digits(to_phone),
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        return self._post(body)

    def send_template(self, to_phone: str, template_name: str,
                      params: list[str] | None = None,
                      language: str = "en") -> SendResult:
        components = []
        if params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            })
        body = {
            "messaging_product": "whatsapp",
            "to": _digits(to_phone),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        return self._post(body)

    def _post(self, body: dict) -> SendResult:
        url = f"{GRAPH_BASE}/{self._phone_number_id}/messages"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            msg_id = (parsed.get("messages") or [{}])[0].get("id")
            return SendResult(ok=True, provider_message_id=msg_id)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            log.error("meta send failed: %s %s", exc.code, detail)
            return SendResult(ok=False, error=f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # network, timeout, DNS
            log.error("meta send error: %s", exc)
            return SendResult(ok=False, error=str(exc))


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _normalise_phone(phone: str) -> str:
    digits = _digits(phone)
    return f"+{digits}" if digits else ""

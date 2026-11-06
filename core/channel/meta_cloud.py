"""Official Meta WhatsApp Cloud API channel adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Any

from core.channel.adapter import (
    ChannelAdapter,
    InboundMessage,
    Prompt,
    SendResult,
)

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
                # Meta sends the sender's display name in `contacts`, keyed by
                # wa_id, separately from the messages. Build a lookup so each
                # message can carry the name of the person who sent it.
                names: dict[str, str] = {}
                for contact in value.get("contacts", []) or []:
                    wa_id = _digits(contact.get("wa_id") or "")
                    name = ((contact.get("profile") or {}).get("name") or "").strip()
                    if wa_id and name:
                        names[wa_id] = name
                for msg in value.get("messages", []) or []:
                    msg_type = msg.get("type", "unknown")
                    text = None
                    button_id = None
                    media_id = None
                    media_type = None
                    if msg_type == "text":
                        text = (msg.get("text") or {}).get("body")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive") or {}
                        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                        # Keep the id as well as the label: the router answers on
                        # the id, which cannot be mistyped or translated away.
                        button_id = reply.get("id")
                        text = reply.get("title") or button_id
                    elif msg_type in ("image", "document"):
                        # A payment screenshot usually arrives as an image, but
                        # some phones send it as a document. Both carry an id to
                        # download; keep the caption as text when there is one.
                        attachment = msg.get(msg_type) or {}
                        media_id = attachment.get("id")
                        media_type = attachment.get("mime_type")
                        text = attachment.get("caption")
                    out.append(InboundMessage(
                        provider_message_id=msg.get("id", ""),
                        from_phone=_normalise_phone(msg.get("from", "")),
                        text=text,
                        message_type=msg_type,
                        timestamp=int(msg["timestamp"]) if msg.get("timestamp") else None,
                        raw=msg,
                        button_id=button_id,
                        profile_name=names.get(_digits(msg.get("from", ""))),
                        media_id=media_id,
                        media_type=media_type,
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

    def send_interactive(self, to_phone: str, prompt: Prompt) -> SendResult:
        if prompt.rows:
            body = {
                "messaging_product": "whatsapp",
                "to": _digits(to_phone),
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": prompt.body},
                    "action": {
                        "button": prompt.button_text,
                        "sections": [{
                            "title": prompt.button_text,
                            "rows": [{"id": row.id, "title": row.title[:24]}
                                     for row in prompt.rows],
                        }],
                    },
                },
            }
        else:
            body = {
                "messaging_product": "whatsapp",
                "to": _digits(to_phone),
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": prompt.body},
                    "action": {
                        "buttons": [
                            {"type": "reply",
                             "reply": {"id": button.id, "title": button.title[:20]}}
                            for button in prompt.buttons
                        ],
                    },
                },
            }
        return self._post(body)

    def send_document(self, to_phone: str, filename: str, content: bytes,
                      media_type: str = "text/csv",
                      caption: str | None = None) -> SendResult:
        """Upload the file, then send it. Meta needs two calls.

        The upload returns a media id that the send call references. A failed
        upload stops here rather than sending a message with no file attached.
        """
        media_id = self._upload(filename, content, media_type)
        if not media_id:
            return SendResult(ok=False, error="document upload failed")
        document: dict[str, Any] = {"id": media_id, "filename": filename}
        if caption:
            document["caption"] = caption
        return self._post({
            "messaging_product": "whatsapp",
            "to": _digits(to_phone),
            "type": "document",
            "document": document,
        })

    def fetch_media(self, media_id: str) -> tuple[bytes, str] | None:
        """Download an inbound image. Returns (bytes, media_type), or None.

        Two calls: the media id resolves to a temporary URL, and the URL is then
        fetched with the same bearer token. Both are needed -- the URL alone is
        rejected. Used to pull the guest's payment screenshot out of the chat.
        """
        if not media_id:
            return None
        try:
            meta_req = urllib.request.Request(
                f"{GRAPH_BASE}/{media_id}",
                headers={"Authorization": f"Bearer {self._access_token}"})
            with urllib.request.urlopen(meta_req, timeout=30) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            url = info.get("url")
            media_type = info.get("mime_type") or "application/octet-stream"
            if not url:
                return None
            fetch = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._access_token}"})
            with urllib.request.urlopen(fetch, timeout=30) as resp:
                return resp.read(), media_type
        except Exception as exc:  # network, timeout, bad id
            log.error("meta media fetch failed: %s", exc)
            return None

    def _upload(self, filename: str, content: bytes, media_type: str) -> str | None:
        """Upload media and return its id, or None on any failure."""
        boundary = "----sagar" + uuid.uuid4().hex
        parts = []
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="messaging_product"\r\n\r\n'
            f"whatsapp\r\n".encode())
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="type"\r\n\r\n'
            f"{media_type}\r\n".encode())
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n".encode())
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        url = f"{GRAPH_BASE}/{self._phone_number_id}/media"
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            return parsed.get("id")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            log.error("meta upload failed: %s %s", exc.code, detail)
            return None
        except Exception as exc:  # network, timeout, DNS
            log.error("meta upload error: %s", exc)
            return None

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
            return SendResult(ok=False, error=f"HTTP {exc.code}: {detail}",
                              retryable=not _is_rate_limited(exc.code, detail))
        except Exception as exc:  # network, timeout, DNS
            log.error("meta send error: %s", exc)
            return SendResult(ok=False, error=str(exc))


def _is_rate_limited(code: int, detail: str) -> bool:
    """True when Meta refused the send because the pair is throttled.

    Code 131056 is "(Business Account, Consumer Account) pair rate limit hit":
    too many messages from this number to the same guest in a short window.
    Sending again is counted against the same limit, so it must not be retried.
    """
    return code == 429 or "131056" in detail


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _normalise_phone(phone: str) -> str:
    digits = _digits(phone)
    return f"+{digits}" if digits else ""

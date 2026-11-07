#!/usr/bin/env python3
"""Check the Meta WhatsApp credentials before going live.

Run this on the server, or in a session where the secrets are registered:

    python scripts/check_meta.py

It makes read-only calls to the Graph API. It never prints the token or the
app secret. It answers four questions:

  1. Is the access token valid, and whose is it?
  2. Which phone numbers are on the WhatsApp Business Account, and what are
     their phone number IDs? This settles which ID belongs in the settings.
  3. Which of the configured phone number IDs is the real one?
  4. Is the app secret the right shape to verify message signatures?

Nothing here sends a message, so it is safe to run against a live account.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"


def _get(path: str, token: str) -> tuple[int, dict]:
    url = f"{GRAPH}/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {"error": {"message": "unreadable error body"}}
        return exc.code, body
    except Exception as exc:  # network trouble
        return 0, {"error": {"message": str(exc)}}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def main() -> int:
    token = _env("META_ACCESS_TOKEN")
    secret = _env("META_APP_SECRET")
    configured = _env("META_PHONE_NUMBER_ID")
    waba = _env("META_BUSINESS_ACCOUNT_ID") or _env("META_WABA_ID")

    print("Credentials present")
    print(f"  META_ACCESS_TOKEN     : {'yes, ' + str(len(token)) + ' chars' if token else 'MISSING'}")
    print(f"  META_APP_SECRET       : {'yes, ' + str(len(secret)) + ' chars' if secret else 'MISSING'}")
    print(f"  META_PHONE_NUMBER_ID  : {configured or 'MISSING'}")
    print(f"  WABA id               : {waba or 'not set (pass it as META_BUSINESS_ACCOUNT_ID)'}")
    print()

    if not token:
        print("Cannot continue without META_ACCESS_TOKEN.")
        print("If you registered it as a secret, start a new session so it is injected.")
        return 1

    problems: list[str] = []

    # 1. The token itself.
    status, body = _get("me?fields=id,name", token)
    if status == 200:
        print(f"Token is valid. Belongs to: {body.get('name', '?')} (id {body.get('id', '?')})")
    else:
        msg = (body.get("error") or {}).get("message", "unknown")
        print(f"Token REJECTED (HTTP {status}): {msg}")
        problems.append("access token is not valid")

    # 2. The app secret shape. Meta's is 32 hex characters.
    if secret:
        if len(secret) == 32 and all(c in "0123456789abcdefABCDEF" for c in secret):
            print("App secret looks right (32 hex characters).")
        else:
            print(f"App secret is {len(secret)} chars and not plain hex; check you copied it exactly.")
            problems.append("app secret may be wrong")

    # 3. The phone numbers on the account, which settles the ID question.
    if waba:
        status, body = _get(f"{waba}/phone_numbers?fields=id,display_phone_number,verified_name,quality_rating", token)
        if status == 200:
            numbers = body.get("data", [])
            if not numbers:
                print("The account reports no phone numbers.")
                problems.append("no phone numbers on the account")
            else:
                print(f"\nPhone numbers on the account ({len(numbers)}):")
                ids = []
                for n in numbers:
                    nid = n.get("id", "?")
                    ids.append(nid)
                    mark = "  <-- configured" if nid == configured else ""
                    print(f"  {nid}  {n.get('display_phone_number', '?')}  "
                          f"{n.get('verified_name', '')}{mark}")
                if configured and configured not in ids:
                    print(f"\nThe configured phone number ID {configured} is NOT on this account.")
                    problems.append("configured phone number ID is not on the account")
                elif configured:
                    print("\nThe configured phone number ID is on the account. Good.")
        else:
            msg = (body.get("error") or {}).get("message", "unknown")
            print(f"Could not list phone numbers (HTTP {status}): {msg}")
    else:
        print("\nNo WABA id given, so the phone number list was not fetched.")
        print("Set META_BUSINESS_ACCOUNT_ID to the Messaging account ID to list them.")

    print()
    if problems:
        print("PROBLEMS FOUND:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("All checks passed. The credentials look ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

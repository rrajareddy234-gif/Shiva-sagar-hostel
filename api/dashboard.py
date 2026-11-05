"""The owner's dashboard routes.

Password-protected, read-only. The browser keeps the password in a cookie and
sends it back with each request, so the owner logs in once per device.
"""

from __future__ import annotations

from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from api import dashboard_page
from core import dashboard
from core.errors import DomainError

router = APIRouter()

_COOKIE = "sagar_dash"
_YEAR = 60 * 60 * 24 * 365


def _authorised(request: Request) -> bool:
    return dashboard.password_is_ok(request.cookies.get(_COOKIE))


def _login_page(message: str = "") -> str:
    note = f'<p class="muted">{message}</p>' if message else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sagar PG Hostel</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background:#f5f6f8; color:#16181d; display:flex; min-height:100vh;
        align-items:center; justify-content:center; margin:0; padding:16px; }}
.box {{ background:#fff; border-radius:12px; padding:24px; width:100%; max-width:360px;
        box-shadow:0 1px 3px rgba(0,0,0,.08); }}
h1 {{ font-size:18px; margin:0 0 16px; }}
input {{ width:100%; padding:12px; border:1px solid #cfd4dc; border-radius:10px;
         font-size:16px; margin-bottom:12px; }}
button {{ width:100%; padding:12px; border:0; border-radius:10px; background:#1f6feb;
          color:#fff; font-size:16px; font-weight:600; }}
.muted {{ color:#5b6270; font-size:13px; }}
</style></head>
<body><form class="box" method="post" action="/dashboard/login">
<h1>Sagar PG Hostel</h1>
<input type="password" name="password" placeholder="Password" autofocus>
<button type="submit">Open dashboard</button>
{note}
</form></body></html>"""


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_home(request: Request, q: str | None = None,
                   notice: str = "", failed: str = "0") -> Response:
    if not _authorised(request):
        return HTMLResponse(_login_page())
    page = dashboard_page.render(request.app.state.db, search=q,
                                 notice=notice, failed=(failed == "1"))
    return HTMLResponse(page)


@router.post("/dashboard/login")
async def dashboard_login(request: Request) -> Response:
    # The login body is one field, so it is parsed here rather than pulling in
    # the python-multipart dependency for a single form.
    fields = parse_qs((await request.body()).decode())
    supplied = (fields.get("password") or [""])[0]
    if not dashboard.password_is_ok(supplied):
        return HTMLResponse(_login_page("That password is not right."), status_code=401)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(_COOKIE, supplied, max_age=_YEAR, httponly=True,
                        samesite="lax")
    return response


@router.post("/dashboard/decide")
async def dashboard_decide(request: Request) -> Response:
    """Approve or reject one waiting booking, from a tap on the page.

    The same password gate, the same booking rules as the WhatsApp command, and
    the same audit trail. A refusal (a bed taken in the meantime, for example)
    comes back as a clear message rather than a broken page.
    """
    if not _authorised(request):
        return RedirectResponse("/dashboard", status_code=303)

    fields = parse_qs((await request.body()).decode())
    reference = (fields.get("reference") or [""])[0]
    decision = (fields.get("decision") or [""])[0]
    actor = "owner-dashboard"

    try:
        if decision == "approve":
            result = dashboard.approve_booking(request.app.state.db, reference,
                                               actor=actor)
            notice = f"Booking {result['reference']} is confirmed. The bed is held."
        elif decision == "reject":
            result = dashboard.reject_booking(request.app.state.db, reference,
                                              actor=actor)
            notice = f"Booking {result['reference']} is rejected. The bed is free."
        else:
            notice, failed = "Unknown decision.", True
            return _back(notice, failed)
    except DomainError as exc:
        return _back(exc.message, True)

    return _back(notice, False)


def _back(notice: str, failed: bool) -> Response:
    """Send the owner back to the page, with a line saying what happened."""
    flag = "1" if failed else "0"
    url = f"/dashboard?notice={quote(notice)}&failed={flag}"
    return RedirectResponse(url, status_code=303)


@router.post("/dashboard/auto-reply")
async def dashboard_auto_reply(request: Request) -> Response:
    """Pause or resume automatic replies to guests, from a tap on the page.

    The same password gate as the rest of the page. The switch is stored in the
    database, so it survives a restart and takes effect at once, with no `.env`
    edit and no SSH. The webhook and the Meta connection are untouched: while
    paused the system still receives, saves and acts on every message.
    """
    if not _authorised(request):
        return RedirectResponse("/dashboard", status_code=303)

    from core import auto_reply

    fields = parse_qs((await request.body()).decode())
    action = (fields.get("action") or [""])[0]
    if action == "pause":
        auto_reply.set_paused(request.app.state.db, True, actor="owner-dashboard")
        return _back("Automatic replies are paused. Guests get no reply until you "
                     "resume. Messages, bookings and payments keep working.", False)
    if action == "resume":
        auto_reply.set_paused(request.app.state.db, False, actor="owner-dashboard")
        return _back("Automatic replies are on again.", False)
    return _back("Unknown action.", True)


@router.get("/dashboard/logout")
def dashboard_logout() -> Response:
    response = RedirectResponse("/dashboard", status_code=303)
    response.delete_cookie(_COOKIE)
    return response

"""The owner's dashboard, as plain HTML.

One page, built for a phone screen, served by the same app that talks to
WhatsApp. Read-only: it shows what is in the database and changes nothing.

The HTML is built here in Python rather than in a template file, so there is no
second language to install and nothing to configure at deploy time.
"""

from __future__ import annotations

import html
from datetime import datetime

from core import dashboard

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f5f6f8; color: #16181d; font-size: 16px;
}
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: #5b6270; font-size: 13px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.card {
  background: #fff; border-radius: 12px; padding: 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,.06);
}
.card .n { font-size: 26px; font-weight: 700; line-height: 1.1; }
.card .l { font-size: 13px; color: #5b6270; margin-top: 2px; }
.alert { border-left: 4px solid #d98324; }
form { display: flex; gap: 8px; margin: 20px 0 10px; }
input[type=search] {
  flex: 1; padding: 12px; border: 1px solid #cfd4dc; border-radius: 10px;
  font-size: 16px; background: #fff; color: inherit;
}
button {
  padding: 12px 16px; border: 0; border-radius: 10px;
  background: #1f6feb; color: #fff; font-size: 16px; font-weight: 600;
}
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 12px; overflow: hidden; font-size: 14px; }
th, td { text-align: left; padding: 10px; border-bottom: 1px solid #eceef1;
         white-space: nowrap; }
th { background: #eef1f5; font-size: 12px; text-transform: uppercase;
     letter-spacing: .03em; color: #5b6270; }
tr:last-child td { border-bottom: 0; }
.pill { padding: 2px 8px; border-radius: 999px; font-size: 12px;
        background: #eef1f5; }
.scroll { overflow-x: auto; }
.muted { color: #5b6270; }
.actions { display: flex; gap: 6px; }
.actions form { margin: 0; }
.actions button { padding: 8px 12px; font-size: 14px; }
.yes { background: #1a7f37; }
.no { background: #c0392b; }
.notice { background: #e7f6ec; border-left: 4px solid #1a7f37; padding: 12px;
          border-radius: 10px; margin-bottom: 16px; font-size: 14px; }
.notice.bad { background: #fdecea; border-left-color: #c0392b; }
a.logout { float: right; font-size: 13px; color: #5b6270; }
.control { background:#fff; border-radius:12px; padding:14px 16px; margin-bottom:16px;
           display:flex; align-items:center; justify-content:space-between; gap:12px;
           flex-wrap:wrap; box-shadow:0 1px 3px rgba(0,0,0,.08); }
.control .state { font-size:15px; font-weight:600; }
.control .state.on { color:#1a7f37; }
.control .state.paused { color:#c0392b; }
.control .hint { font-size:13px; color:#5b6270; margin-top:2px; }
.control form { margin:0; }
.control button { width:auto; padding:10px 16px; border:0; border-radius:10px;
                  font-size:14px; font-weight:600; color:#fff; cursor:pointer; }
.control button.pause { background:#c0392b; }
.control button.resume { background:#1a7f37; }
@media (min-width: 700px) { .grid { grid-template-columns: repeat(4, 1fr); } }
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sagar PG Hostel</title>
<style>{style}</style>
</head>
<body>
<h1>Sagar PG Hostel <a class="logout" href="/dashboard/logout">Log out</a></h1>
<div class="sub">{updated}</div>

{notice}

{control}

<div class="grid">
  <div class="card"><div class="n">{beds_free} / {beds_total}</div>
    <div class="l">Beds free</div></div>
  <div class="card"><div class="n">{in_house}</div>
    <div class="l">Guests in house</div></div>
  <div class="card"><div class="n">{arrivals_today}</div>
    <div class="l">Arrivals today</div></div>
  <div class="card"><div class="n">{departures_today}</div>
    <div class="l">Checkouts due</div></div>
  <div class="card alert"><div class="n">{pending}</div>
    <div class="l">Waiting for your approval</div></div>
  <div class="card alert"><div class="n">{to_verify}</div>
    <div class="l">Payments to verify</div></div>
  <div class="card"><div class="n">{outstanding}</div>
    <div class="l">Bookings with a balance</div></div>
  <div class="card"><div class="n">Rs {revenue_month}</div>
    <div class="l">Verified this month</div></div>
</div>

{pending_block}

<form method="get" action="/dashboard">
  <input type="search" name="q" value="{query}"
         placeholder="Search a name or phone number">
  <button type="submit">Search</button>
</form>

<div class="scroll">
<table>
<thead><tr>
  <th>Name</th><th>Phone</th><th>Booking</th><th>Status</th><th>Bed</th>
  <th>Room</th><th>Check-in</th><th>Check-out</th><th>Bill</th><th>Paid</th>
  <th>Due</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</div>
<p class="muted">{count}</p>
</body>
</html>
"""

_ROW = """<tr>
  <td>{name}</td><td>{phone}</td><td>{booking}</td><td>{status}</td>
  <td>{bed}</td><td>{room}</td><td>{check_in}</td><td>{check_out}</td>
  <td>{bill}</td><td>{paid}</td><td>{due}</td>
</tr>"""

_PENDING = """<h1 style="font-size:16px;margin:22px 0 8px">Waiting for you</h1>
<div class="scroll"><table>
<thead><tr><th>Reference</th><th>Guest</th><th>Bed</th><th>Check-in</th>
<th>Check-out</th><th>Amount</th><th>Decision</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""

_PENDING_ROW = """<tr>
  <td>{reference}</td><td>{name}</td><td>{bed}</td>
  <td>{check_in}</td><td>{check_out}</td><td>Rs {amount}</td>
  <td><div class="actions">
    <form method="post" action="/dashboard/decide">
      <input type="hidden" name="reference" value="{reference}">
      <input type="hidden" name="decision" value="approve">
      <button class="yes" type="submit">Approve</button>
    </form>
    <form method="post" action="/dashboard/decide">
      <input type="hidden" name="reference" value="{reference}">
      <input type="hidden" name="decision" value="reject">
      <button class="no" type="submit">Reject</button>
    </form>
  </div></td>
</tr>"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


_CONTROL_ON = """<div class="control">
  <div>
    <div class="state on">Automatic replies: ON</div>
    <div class="hint">Guests are answered automatically. Pausing stops replies
    to guests only. Messages are still saved, bookings and payments still work,
    and you are still told about new booking requests.</div>
  </div>
  <form method="post" action="/dashboard/auto-reply">
    <input type="hidden" name="action" value="pause">
    <button class="pause" type="submit">Pause replies</button>
  </form>
</div>"""

_CONTROL_PAUSED = """<div class="control">
  <div>
    <div class="state paused">Automatic replies: PAUSED</div>
    <div class="hint">Guests are not being answered automatically. Messages are
    still saved, bookings and payments still work, and you are still told about
    new booking requests.</div>
  </div>
  <form method="post" action="/dashboard/auto-reply">
    <input type="hidden" name="action" value="resume">
    <button class="resume" type="submit">Resume replies</button>
  </form>
</div>"""


def render(db, *, now: datetime | None = None, search: str | None = None,
           notice: str = "", failed: bool = False) -> str:
    """The whole page as one HTML string."""
    now = now or datetime.now().astimezone()
    numbers = dashboard.overview(db, now)
    guests = dashboard.guests(db, search=search)
    waiting = dashboard.pending_requests(db)

    notice_block = ""
    if notice:
        css = "notice bad" if failed else "notice"
        notice_block = f'<div class="{css}">{_esc(notice)}</div>'

    pending_block = ""
    if waiting:
        rows = "".join(_PENDING_ROW.format(**{k: _esc(v) for k, v in w.items()})
                       for w in waiting)
        pending_block = _PENDING.format(rows=rows)

    count = f"{len(guests)} guest(s) shown"
    if search and not guests:
        count = f"No guest matches {search!r}"

    from core import auto_reply
    paused = auto_reply.is_paused(db)
    control = _CONTROL_PAUSED if paused else _CONTROL_ON

    rows = "".join(
        _ROW.format(name=_esc(g.name), phone=_esc(g.phone), booking=_esc(g.booking),
                    status=_esc(g.status), bed=_esc(g.bed), room=_esc(g.room),
                    check_in=_esc(g.check_in), check_out=_esc(g.check_out),
                    bill=_esc(g.bill), paid=_esc(g.paid), due=_esc(g.due))
        for g in guests) or '<tr><td colspan="11" class="muted">No guests yet.</td></tr>'

    return _PAGE.format(
        style=_STYLE,
        updated=f"Updated {now:%d %b %Y, %I:%M %p}",
        query=_esc(search or ""),
        rows=rows,
        pending_block=pending_block,
        notice=notice_block,
        control=control,
        count=count,
        **{k: _esc(v) for k, v in numbers.items()},
    )

"""Build a permanent, self-contained demo page for GitHub Pages.

Runs two real conversations — one as a guest, one as the owner — against a
throwaway database seeded from `db/seed.sql`, captures exactly what the engine
replies, and writes the whole thing into one static HTML file with no external
dependencies. Nothing on the page is hand-written: every bubble is the real
output of the real code.

    python scripts/build_demo_page.py            # writes docs/demo/index.html
    python scripts/build_demo_page.py --out DIR
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.channel.adapter import ChannelAdapter, InboundMessage, Prompt, SendResult  # noqa: E402
from core.config import Settings, load_dotenv  # noqa: E402
from core.db import Database  # noqa: E402
from core.dispatcher import dispatch  # noqa: E402
from core.inbox import store_inbound  # noqa: E402
from scripts.chat import DEMO_DB, _admin_dsn, build_scratch_database  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GUEST_PHONE = "+919000000001"
OWNER_PHONE = "+918688397996"

# The demo is one interleaved timeline, not two scripts. A guest books, the
# owner makes the first decision, the guest pays and sends the screenshot, and
# the owner makes the second decision -- in that order, because that is the
# order the two people actually act in. Splitting it into a "guest script" and
# an "owner script" would show the steps out of order and hide the whole point
# of the two-step approval.
#
# A step wrapped in [ ] is a tap on the button with that label, exactly as
# WhatsApp delivers it. `[screenshot]` is the guest's payment image.
TIMELINE = [
    ("guest", "hi"),
    ("guest", "[Rooms & Prices]"),
    ("guest", "[AC]"),
    ("guest", "[With food]"),
    ("guest", "[Check Availability]"),
    ("guest", "[Food & Amenities]"),
    ("guest", "[Location & Directions]"),
    ("guest", "[Guest Reviews]"),
    ("guest", "[My Bookings]"),
    ("guest", "[Book a Room]"),
    ("guest", "[Tomorrow]"),
    ("guest", "[2 nights]"),
    ("guest", "[2 guests]"),
    ("guest", "[AC]"),
    ("guest", "[With food]"),
    ("guest", "[Reservation]"),
    # The name is not asked for here: the guest's WhatsApp profile name is
    # already known, so the flow goes straight to the review. That is the real
    # behaviour, and it keeps this page free of any AI call.
    ("guest", "[Submit]"),
    # The owner is now asked to make the first decision.
    ("owner", "pending bookings"),
    ("owner", "[Approve]"),
    # Accepting tells the guest to pay. The guest pays and sends the proof.
    ("guest", "[screenshot]"),
    # The owner checks the money and makes the second decision.
    ("owner", "[Confirm]"),
    ("guest", "[My Bookings]"),
    ("guest", "is there wifi?"),
    ("guest", "where are you located"),
    ("guest", "[Talk to Staff]"),
    ("owner", "how many vacancies today"),
    ("owner", "revenue this month"),
]


class CaptureChannel(ChannelAdapter):
    """Records replies instead of sending them, and serves a fake screenshot.

    Every capture is tagged with the phone it was addressed to, so one timeline
    can hold both people's phones: a message sent to the owner belongs in the
    owner's window and a message sent to the guest in the guest's. Without that
    the two conversations could not be interleaved in the right order.
    """

    name = "capture"

    def __init__(self) -> None:
        self.out_seq = 0
        self.last_options: dict[str, str] = {}

    def _next(self, kind: str) -> str:
        self.out_seq += 1
        return f"{kind}.{self.out_seq}"

    def verify_inbound(self, raw_body: bytes, headers: dict) -> bool:
        return True

    def parse_inbound(self, raw_body: bytes) -> list[InboundMessage]:
        return []

    def fetch_media(self, media_id: str) -> tuple[bytes, str] | None:
        """Stand in for the download of a guest's payment screenshot.

        The real channel downloads the image from WhatsApp. Here a small, valid
        1x1 JPEG is returned so the proof path -- file it, forward it to the
        owner, email a copy -- runs exactly as it does in production.
        """
        return _SAMPLE_JPEG, "image/jpeg"

    def send_text(self, to_phone: str, text: str) -> SendResult:
        self._capture(to_phone, {"type": "text", "text": text})
        return SendResult(ok=True, provider_message_id=self._next("out"))

    def send_interactive(self, to_phone: str, prompt: Prompt) -> SendResult:
        options = prompt.rows or prompt.buttons
        self.last_options.update({o.title: o.id for o in options})
        self._capture(to_phone, {
            "type": "list" if prompt.rows else "buttons",
            "text": prompt.body,
            "button_text": prompt.button_text,
            "options": [{"id": o.id, "title": o.title} for o in options],
        })
        return SendResult(ok=True, provider_message_id=self._next("btn"))

    def send_template(self, to_phone, template_name, params=None, language="en"):
        self._capture(to_phone, {"type": "text", "text": f"(template: {template_name})"})
        return SendResult(ok=True, provider_message_id=self._next("tpl"))

    def send_document(self, to_phone, filename, content, media_type="text/csv",
                      caption=None):
        self._capture(to_phone, {"type": "document", "text": caption or "",
                                 "filename": filename, "bytes": len(content or b"")})
        return SendResult(ok=True, provider_message_id=self._next("doc"))

    def _capture(self, to_phone: str, payload: dict) -> None:
        payload = {"to": to_phone, **payload}
        CURRENT.setdefault("replies", []).append(payload)


# A valid 1x1 JPEG, small enough to inline. It stands in for the guest's
# payment screenshot so the demo exercises the real media path.
_SAMPLE_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "07090908"
    "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837"
    "292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc400"
    "1f0000010501010101010100000000000000000102030405060708090a0bffc400b5"
    "100002010303020403050504040000017d0102030004110512213141061351610722"
    "7114328191082342b1c11552d1f02433627282090a161718191a25262728292a343536"
    "3738393a434445464748494a535455565758595a636465666768696a73747576777879"
    "7a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8"
    "b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3"
    "f4f5f6f7f8f9faffda0008010100003f00fefeffd9")

CURRENT: dict = {}


def _run_timeline(db, channel, settings, ai) -> dict:
    """Play the timeline and split the captured traffic into two windows.

    Every reply the engine sends is addressed to one phone, so it is filed in
    that person's window. That is what lets the guest's and the owner's sides of
    one booking be shown as they really happen, in order, rather than as two
    unrelated scripts.
    """
    windows: dict[str, list[dict]] = {"guest": [], "owner": []}
    people = {"guest": GUEST_PHONE, "owner": OWNER_PHONE}
    global CURRENT

    for i, (who, raw) in enumerate(TIMELINE, start=1):
        phone = people[who]
        tap = None
        text: str | None = raw
        media_id = None
        message_type = "text"

        if raw == "[screenshot]":
            media_id = "demo-media-1"
            message_type = "image"
            text = None
        elif raw.startswith("[") and raw.endswith("]"):
            tap = raw[1:-1]
            text = tap

        button_id = channel.last_options.get(tap, tap) if tap else None
        message = InboundMessage(
            provider_message_id=f"{who}.{i}",
            from_phone=phone,
            text=text,
            message_type="interactive" if button_id else message_type,
            button_id=button_id,
            media_id=media_id,
            media_type="image/jpeg" if media_id else None,
            profile_name="Ravi Kumar",
        )

        CURRENT = {"replies": []}
        windows[who].append({"from": "them", "text": raw, "tap": bool(tap),
                             "media": bool(media_id)})
        try:
            stored = store_inbound(db, message)
            dispatch(db, channel, settings, message, ai,
                     stored.message_id if stored else None)
        except Exception as exc:  # keep the page buildable
            CURRENT["replies"].append(
                {"type": "text", "text": f"[engine error: {type(exc).__name__}: {exc}]"})

        for r in CURRENT["replies"]:
            target = "owner" if r.get("to") == OWNER_PHONE else "guest"
            windows[target].append({"from": "bot", **r})
    return windows


def _seed_sample_reviews(db) -> None:
    """Two clearly-labelled sample reviews, so the demo's Guest Reviews tap has
    something to show. They live only in the throwaway demo database; the real
    hostel starts with none, and the page says so plainly until the owner adds
    their own."""
    from core import reviews
    for name, stars, body in (
        ("Sample guest 1", 5, "Rooms are clean and the food is homely. [sample]"),
        ("Sample guest 2", 4, "Quiet at night and the owner is helpful. [sample]"),
    ):
        reviews.add(db, guest_name=name, body=body, actor="demo", rating=stars,
                    source="owner")


def build_page(out_path: Path) -> Path:
    load_dotenv()
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is not set; copy .env.example to .env first.")

    dsn = build_scratch_database()
    settings = Settings.from_env()
    settings = Settings(
        database_url=dsn, channel_provider="capture",
        owner_phone=OWNER_PHONE, timezone=settings.timezone,
        meta_app_secret=None, meta_access_token=None,
        meta_phone_number_id=None, meta_verify_token=None,
        ai_provider=settings.ai_provider, ai_model=settings.ai_model,
        ai_api_key=settings.ai_api_key, ai_base_url=settings.ai_base_url,
        ai_timeout=settings.ai_timeout, staff_phones=settings.staff_phones,
    )
    # No AI on this page: every answer comes from the router and the database,
    # so the page shows exactly the deterministic behaviour, not a model's mood.
    ai = None
    db = Database(dsn)
    channel = CaptureChannel()

    _seed_sample_reviews(db)

    windows = _run_timeline(db, channel, settings, ai)

    page = _render(windows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page)

    with psycopg_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (DEMO_DB,))
            cur.execute(f'DROP DATABASE IF EXISTS "{DEMO_DB}"')
    return out_path


def psycopg_connect():
    import psycopg
    return psycopg.connect(_admin_dsn(), autocommit=True)


def _esc(t: str) -> str:
    return html.escape(t)


def _bubble(turn: dict) -> str:
    if turn["from"] == "them":
        if turn.get("media"):
            # The guest's payment screenshot, as it appears in the chat.
            return ('<div class="msg out shot"><div class="thumb">🧾</div>'
                    'payment-screenshot.jpg<span class="tick">✓✓</span></div>')
        body = _esc(turn["text"])
        cls = "out"
        if turn.get("tap"):
            body = f'<span class="tapmark">tapped</span> {body}'
        return f'<div class="msg {cls}">{body}<span class="tick">✓✓</span></div>'

    kind = turn.get("type", "text")
    if kind == "document":
        name = _esc(turn.get("filename", "file"))
        caption = _esc(turn.get("text", "")).replace("\n", "<br>")
        return (f'<div class="msg in doc"><div class="dhead">📎 {name}</div>'
                f'{caption}</div>')

    body = _esc(turn.get("text", "")).replace("\n", "<br>")
    extra = ""
    if kind in ("list", "buttons") and turn.get("options"):
        items = "".join(
            f'<div class="opt"><span class="oic">▸</span>{_esc(o["title"])}</div>'
            for o in turn["options"]
        )
        label = _esc(turn.get("button_text") or "Choose")
        extra = (f'<div class="opts"><div class="ohdr">{label}</div>{items}</div>')
    return f'<div class="msg in">{body}{extra}</div>'


def _conversation(turns: list[dict]) -> str:
    return "".join(_bubble(t) for t in turns)


def _render(conv: dict) -> str:
    guest_html = _conversation(conv["guest"])
    owner_html = _conversation(conv["owner"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sagar PG Hostel — Live Demo</title>
<style>
  :root{{
    --wa-bg:#0b141a; --bubble-in:#202c33; --bubble-out:#005c4b;
    --txt:#e9edef; --muted:#8696a0; --green:#00a884; --tick:#53bdeb;
    --saffron:#f2a33c; --paper:#f5efe6;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:#14100d;color:var(--paper);padding:28px 16px 80px;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:760px;margin:0 auto}}
  header{{margin-bottom:22px}}
  .eyebrow{{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--saffron);margin-bottom:12px}}
  h1{{font-size:clamp(1.6rem,5vw,2.4rem);line-height:1.1;letter-spacing:-.02em;margin-bottom:12px}}
  h1 em{{font-style:normal;color:var(--saffron)}}
  .lede{{color:#cbbfb2;font-size:.95rem;line-height:1.6;max-width:60ch}}
  .note{{border:1px solid rgba(242,163,60,.34);border-left:3px solid var(--saffron);
    background:rgba(242,163,60,.07);border-radius:10px;padding:13px 16px;margin:20px 0;
    font-size:.86rem;line-height:1.55;color:#e3d6c6}}
  .note b{{color:var(--saffron)}}
  .tabs{{display:flex;gap:8px;margin:22px 0 14px}}
  .tabs button{{flex:1;border:1px solid rgba(245,239,230,.16);background:transparent;color:var(--muted);
    padding:11px 12px;border-radius:10px;font:inherit;font-size:.9rem;font-weight:600;cursor:pointer}}
  .tabs button.on{{background:var(--green);border-color:var(--green);color:#04231c}}
  .phone{{background:#0d0f10;border-radius:26px;padding:9px;box-shadow:0 24px 50px -18px rgba(0,0,0,.85),0 0 0 1px rgba(245,239,230,.09)}}
  .screen{{background:var(--wa-bg);border-radius:20px;overflow:hidden;display:flex;flex-direction:column}}
  .bar{{background:#1f2c33;padding:11px 14px;display:flex;align-items:center;gap:11px;border-bottom:1px solid rgba(0,0,0,.3)}}
  .avatar{{width:34px;height:34px;border-radius:50%;background:linear-gradient(140deg,var(--saffron),#c65d3b);
    display:grid;place-items:center;font-weight:700;color:#231a10;flex:0 0 auto}}
  .who b{{display:block;font-size:.9rem;font-weight:600}}
  .who span{{font-size:.7rem;color:var(--muted)}}
  .chat{{padding:16px 13px 20px;display:flex;flex-direction:column;gap:9px;min-height:420px}}
  .msg{{max-width:88%;padding:9px 12px;border-radius:12px;font-size:.85rem;line-height:1.5;
    word-wrap:break-word;animation:pop .35s ease backwards}}
  .msg.in{{background:var(--bubble-in);align-self:flex-start;border-top-left-radius:3px}}
  .msg.out{{background:var(--bubble-out);align-self:flex-end;border-top-right-radius:3px}}
  .msg .tick{{font-size:.65rem;color:var(--tick);margin-left:6px}}
  .tapmark{{font-size:.6rem;text-transform:uppercase;letter-spacing:.08em;background:rgba(255,255,255,.18);
    padding:2px 6px;border-radius:5px;margin-right:6px}}
  .opts{{margin:9px -12px -9px;border-top:1px solid rgba(245,239,230,.12)}}
  .ohdr{{font-size:.66rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:8px 12px 5px}}
  .opt{{padding:9px 12px;border-top:1px solid rgba(245,239,230,.07);font-size:.82rem;display:flex;gap:9px}}
  .oic{{color:var(--green)}}
  .shot{{display:flex;align-items:center;gap:9px}}
  .shot .thumb{{width:38px;height:38px;border-radius:8px;background:rgba(255,255,255,.14);
    display:grid;place-items:center;font-size:1.1rem;flex:0 0 auto}}
  .doc{{border-left:3px solid var(--green)}}
  .doc .dhead{{font-size:.72rem;color:#7ee2a8;font-weight:700;margin-bottom:5px;letter-spacing:.02em}}
  .steps{{margin:22px 0 4px;display:grid;gap:9px}}
  .step{{display:flex;gap:11px;align-items:flex-start;font-size:.86rem;line-height:1.5;color:#e3d6c6}}
  .step .n{{flex:0 0 auto;width:22px;height:22px;border-radius:50%;background:var(--saffron);
    color:#231a10;font-weight:700;font-size:.74rem;display:grid;place-items:center;margin-top:1px}}
  .step b{{color:var(--paper)}}
  @keyframes pop{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
  .hide{{display:none}}
  footer{{margin-top:30px;color:var(--muted);font-size:.82rem;line-height:1.7}}
  footer b{{color:var(--paper)}}
  .replay{{margin:12px 0 0;border:1px solid rgba(0,168,132,.5);background:rgba(0,168,132,.12);color:#7ee2a8;
    padding:9px 16px;border-radius:9px;font:inherit;font-size:.85rem;font-weight:600;cursor:pointer}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Sagar PG · Macherla</div>
    <h1>Book a bed, and watch<br><em>both sides of it</em>.</h1>
    <p class="lede">One guest, one booking, two decisions by the owner. Every bubble
    below was produced by the real booking engine on a sample database. Nothing here
    is typed by hand.</p>
  </header>

  <div class="note">
    <b>These are real outputs, not a mockup.</b> Each reply was generated by the same
    code your WhatsApp number runs, on a sample hostel seeded from your own rooms and
    rate card. The database is a throwaway copy, so nothing here can affect real
    guests. The AI is switched off, so you see the fixed answers and the exact prices
    from your rate card.
  </div>

  <div class="steps">
    <div class="step"><span class="n">1</span><span>The guest books and <b>sends the payment screenshot</b>.</span></div>
    <div class="step"><span class="n">2</span><span>You tap <b>Approve</b> — the guest is told to pay.</span></div>
    <div class="step"><span class="n">3</span><span>You check the money, then tap <b>Confirm</b>. <b>A bed is only ever confirmed once you have seen the payment.</b></span></div>
  </div>

  <div class="tabs" id="tabs">
    <button class="on" data-t="guest">👤 Guest's phone</button>
    <button data-t="owner">🏠 Your phone</button>
  </div>

  <div id="guest">
    <div class="phone"><div class="screen">
      <div class="bar"><span class="avatar">SP</span>
        <span class="who"><b>Sagar PG Hostel</b><span>online</span></span></div>
      <div class="chat" id="chat-guest">{guest_html}</div>
    </div></div>
    <button class="replay" data-r="guest">▶ Replay this conversation</button>
  </div>

  <div id="owner" class="hide">
    <div class="phone"><div class="screen">
      <div class="bar"><span class="avatar">SP</span>
        <span class="who"><b>Sagar PG Hostel — Owner</b><span>you</span></span></div>
      <div class="chat" id="chat-owner">{owner_html}</div>
    </div></div>
    <button class="replay" data-r="owner">▶ Replay this conversation</button>
  </div>

  <footer>
    <b>What you are looking at:</b> the guest tab shows the menu, prices chosen by
    tapping, live availability, the booking, the payment screenshot, and your address.
    The owner tab shows your two decisions — <b>Approve</b>, then <b>Confirm</b> — plus
    your own commands. Switch tabs and read the two sides of the same booking.<br><br>
    <b>Prices never come from the AI.</b> Every figure is read from your rate card by
    <code>core/pricing.py</code>. The payment screenshot is only ever moved to you;
    the bot never reads it, and it cannot.
  </footer>
</div>

<script>
  document.querySelectorAll('#tabs button').forEach(function (b) {{
    b.addEventListener('click', function () {{
      document.querySelectorAll('#tabs button').forEach(function (x) {{ x.classList.remove('on'); }});
      b.classList.add('on');
      ['guest', 'owner'].forEach(function (k) {{
        document.getElementById(k).classList.toggle('hide', k !== b.dataset.t);
      }});
    }});
  }});
  function replay(which) {{
    var msgs = document.querySelectorAll('#chat-' + which + ' .msg');
    msgs.forEach(function (m) {{ m.style.animation = 'none'; }});
    void document.body.offsetWidth;
    msgs.forEach(function (m, i) {{
      m.style.animation = 'pop .35s ease backwards';
      m.style.animationDelay = (i * 0.28) + 's';
    }});
  }}
  document.querySelectorAll('.replay').forEach(function (b) {{
    b.addEventListener('click', function () {{ replay(b.dataset.r); }});
  }});
  replay('guest');
</script>
</body>
</html>
"""


LANDING = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sagar PG Hostel — Previews</title>
<style>
  body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:#14100d;color:#f5efe6;margin:0;padding:44px 18px;
    display:flex;flex-direction:column;align-items:center;-webkit-font-smoothing:antialiased}
  .card{width:100%;max-width:520px}
  .eyebrow{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#f2a33c;margin-bottom:14px}
  h1{font-size:clamp(1.7rem,6vw,2.3rem);line-height:1.1;letter-spacing:-.02em;margin:0 0 12px}
  h1 em{font-style:normal;color:#f2a33c}
  p{color:#cbbfb2;line-height:1.6;font-size:.95rem;margin:0 0 26px}
  a.big{display:block;text-decoration:none;color:inherit;border:1px solid rgba(245,239,230,.14);
    border-radius:14px;padding:20px 22px;margin-bottom:14px;background:rgba(245,239,230,.035);transition:.15s}
  a.big:hover{border-color:#00a884;background:rgba(0,168,132,.10)}
  a.big .t{font-weight:700;font-size:1.05rem;display:flex;align-items:center;gap:9px}
  a.big .d{color:#8696a0;font-size:.85rem;margin-top:7px;line-height:1.5}
  .foot{color:#8696a0;font-size:.8rem;line-height:1.7;margin-top:22px}
  .foot b{color:#f5efe6}
</style>
</head>
<body>
<div class="card">
  <div class="eyebrow">Sagar PG · Macherla</div>
  <h1>Two things to look at,<br><em>any time you like</em>.</h1>
  <p>This link is permanent. It does not depend on any server being switched on, so it
  will still open tomorrow and next month.</p>

  <a class="big" href="demo.html">
    <span class="t">💬 Try the bot — guest &amp; owner</span>
    <span class="d">Two real conversations produced by the actual booking engine.
    Tap the guest tab or the owner tab, then press replay.</span>
  </a>

  <a class="big" href="menu.html">
    <span class="t">📱 The menu design</span>
    <span class="d">The full WhatsApp booking picture — your menu choices,
    the bill with the working shown, and the two payment paths.</span>
  </a>

  <div class="foot">
    <b>Nothing here is a live system.</b> These pages are generated from the project's
    own code and data. Prices come from the rate card, never from the AI.
  </div>
</div>
</body>
</html>
"""


def build_site(out_dir: Path) -> list[Path]:
    """Write the permanent site: landing page, live demo, and the menu mockup."""
    out_dir.mkdir(parents=True, exist_ok=True)
    demo = build_page(out_dir / "demo.html")
    menu_src = ROOT / "mockups" / "final-booking-template.html"
    menu = out_dir / "menu.html"
    menu.write_text(menu_src.read_text())
    index = out_dir / "index.html"
    index.write_text(LANDING)
    return [index, demo, menu]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the single demo page here")
    parser.add_argument("--site", help="write the full site (landing + demo + menu) here")
    args = parser.parse_args()

    if args.site:
        paths = build_site(Path(args.site))
        for p in paths:
            print(f"wrote {p} ({p.stat().st_size} bytes)")
        return 0

    path = build_page(Path(args.out or (ROOT / "docs" / "demo" / "index.html")))
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

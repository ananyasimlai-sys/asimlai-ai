"""CarNote: a QR sticker that lets anyone email a car's owner from their own email app.

One file on purpose. The whole product is three ideas:
  1. An owner types in an email address and gets a QR sticker. No account, no password.
  2. Anyone who scans the sticker picks a ready-made message.
  3. Their own email app opens with that message addressed to the owner.

The site itself never sends an email, so there is no mail server to set up or pay for.
The trade-off: the scanner and the owner see each other's email addresses.
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import segno
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from markupsafe import Markup

# What a scanner can say. Ready-made choices make sending a ten-second job.
REASONS = {
    "blocking": "Your car is blocking me",
    "lights": "Your lights are on",
    "window": "A window or door is open",
    "damage": "Your car has been damaged",
    "alarm": "Your alarm is going off",
    "other": "Something else",
}

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L, easy to read aloud
# Deliberately strict: the address goes inside an email link, so no odd characters allowed.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

MAX_NOTE = 280
MAX_STICKERS_PER_DEVICE_PER_HOUR = 5
MAX_REVEALS_PER_DEVICE_PER_HOUR = 5  # how often one device can be shown a car's email address
MAX_REVEALS_PER_CAR_PER_HOUR = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    manage_hash TEXT NOT NULL UNIQUE,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    kind TEXT NOT NULL,          -- 'create' (sticker made) or 'reveal' (scanner shown the address)
    car_id INTEGER,
    sender TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


# ---------- small helpers ----------

def stamp(moment=None):
    """A UTC timestamp as text. The fixed format means text comparison equals time comparison."""
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def an_hour_ago():
    return stamp(datetime.now(timezone.utc) - timedelta(hours=1))


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def absolute(endpoint, **values):
    base = current_app.config["BASE_URL"] or request.host_url.rstrip("/")
    return base + url_for(endpoint, **values)


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def hashed(token):
    """We keep only a scrambled copy of each manage link, like a password."""
    return hashlib.sha256(token.encode()).hexdigest()


def device_fingerprint():
    """Identifies a device for rate limiting without storing its IP address."""
    raw = f"{current_app.secret_key}|{request.remote_addr}|{request.user_agent.string}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def notice(title, body, status=200):
    return render_template("notice.html", title=title, body=body), status


def mailto(recipient, subject, body):
    """A link that opens the visitor's own email app with a message ready to send."""
    body = body.replace("\n", "\r\n")
    return f"mailto:{quote(recipient, safe='@')}?subject={quote(subject)}&body={quote(body)}"


def local_secret(instance_path):
    """A secret key kept in a file that never goes to GitHub, for running on your own machine."""
    path = Path(instance_path) / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


# ---------- the app ----------

def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    env = os.environ.get
    app.config.update(
        APP_NAME=env("APP_NAME", "CarNote"),
        DATABASE=env("DATABASE", str(Path(app.instance_path) / "carnote.sqlite3")),
        BASE_URL=env("BASE_URL", "").rstrip("/"),
        SECRET_KEY=env("SECRET_KEY", ""),
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.config.update(test_config or {})
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = local_secret(app.instance_path)
    app.config["SESSION_COOKIE_SECURE"] = app.config["BASE_URL"].startswith("https")

    if env("TRUST_PROXY") == "1":  # set this when hosted behind a proxy, so we see real visitor IPs
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    with sqlite3.connect(app.config["DATABASE"]) as db:
        db.executescript(SCHEMA)

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def template_globals():
        return {"app_name": app.config["APP_NAME"], "csrf_token": csrf_token, "REASONS": REASONS}

    @app.before_request
    def check_form_came_from_us():
        """Every form carries a hidden token, so other websites cannot submit forms for a visitor."""
        if request.method != "POST":
            return None
        sent = request.form.get("csrf", "").encode()
        expected = session.get("csrf", "").encode()
        if not expected or not hmac.compare_digest(sent, expected):
            return notice(
                "Please try again",
                "This page had been open for a while. Go back, refresh it, and send again.",
                400,
            )
        return None

    @app.after_request
    def privacy_headers(response):
        response.headers["Referrer-Policy"] = "no-referrer"  # our links contain private tokens
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def count_events(kind, car_id=None, sender=None):
        query, values = "SELECT COUNT(*) FROM events WHERE kind = ? AND created_at > ?", [kind, an_hour_ago()]
        if car_id is not None:
            query, values = query + " AND car_id = ?", values + [car_id]
        if sender is not None:
            query, values = query + " AND sender = ?", values + [sender]
        return get_db().execute(query, values).fetchone()[0]

    def record_event(kind, car_id, sender):
        db = get_db()
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (kind, car_id, sender, stamp()))
        db.commit()

    # ----- owner: make a sticker -----

    @app.get("/")
    def home():
        # Stickers made in this browser, so a lost manage link isn't the end of the world.
        mine = []
        for token in session.get("mine", []):
            car = get_db().execute("SELECT * FROM cars WHERE manage_hash = ?", (hashed(token),)).fetchone()
            if car:
                mine.append({"token": token, "code": car["code"], "email": car["email"]})
        return render_template("home.html", mine=mine)

    @app.post("/start")
    def start():
        email = request.form.get("email", "").strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            flash("That doesn't look like an email address. Please check it.")
            return redirect(url_for("home"))
        device = device_fingerprint()
        if count_events("create", sender=device) >= MAX_STICKERS_PER_DEVICE_PER_HOUR:
            return notice("Too many stickers", "You've made several stickers recently. Please try again later.", 429)

        db = get_db()
        token = secrets.token_urlsafe(24)
        while True:
            try:
                cursor = db.execute(
                    "INSERT INTO cars (code, email, manage_hash, created_at) VALUES (?, ?, ?, ?)",
                    (new_code(), email, hashed(token), stamp()),
                )
                break
            except sqlite3.IntegrityError:
                continue  # code already taken (astronomically unlikely), pick another
        db.commit()
        record_event("create", cursor.lastrowid, device)
        session["mine"] = (session.get("mine", []) + [token])[-10:]
        session.permanent = True
        return redirect(url_for("manage", token=token))

    def car_for(token):
        car = get_db().execute("SELECT * FROM cars WHERE manage_hash = ?", (hashed(token),)).fetchone()
        if car is None:
            abort(404)
        return car

    @app.get("/m/<token>")
    def manage(token):
        """The owner's private page. Whoever has this link controls the sticker."""
        car = car_for(token)
        scan_link = absolute("scan", code=car["code"])
        manage_link = absolute("manage", token=token)
        started = get_db().execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'reveal' AND car_id = ?", (car["id"],)
        ).fetchone()[0]
        return render_template(
            "manage.html",
            car=car,
            token=token,
            scan_link=scan_link,
            started=started,
            qr=Markup(segno.make(scan_link, error="m").svg_inline(border=2, omitsize=True)),
            save_link=mailto(
                car["email"],
                f"My {app.config['APP_NAME']} sticker",
                f"Keep this email. This private link lets you print, pause or delete your sticker:\n\n{manage_link}",
            ),
        )

    @app.post("/m/<token>")
    def update(token):
        car = car_for(token)
        db = get_db()
        action = request.form.get("action")
        if action == "delete":
            db.execute("DELETE FROM events WHERE car_id = ?", (car["id"],))
            db.execute("DELETE FROM cars WHERE id = ?", (car["id"],))
            db.commit()
            return notice("Sticker deleted", "Your email address has been removed. The QR code no longer works, so you can peel the sticker off.")
        if action in ("pause", "resume"):
            db.execute("UPDATE cars SET paused = ? WHERE id = ?", (1 if action == "pause" else 0, car["id"]))
            db.commit()
        return redirect(url_for("manage", token=token))

    # ----- scanner: write a message, then send it from their own email app -----

    def find_car(code):
        car = get_db().execute("SELECT * FROM cars WHERE code = ?", (code.upper(),)).fetchone()
        if car is None:
            abort(404)
        return car

    @app.get("/c/<code>")
    def scan(code):
        # Note: the owner's address is NOT on this page. It is only shown after the form is sent.
        return render_template("scan.html", car=find_car(code), max_note=MAX_NOTE)

    @app.post("/c/<code>")
    def compose(code):
        car = find_car(code)
        if car["paused"]:
            return redirect(url_for("scan", code=car["code"]))
        if request.form.get("website"):  # hidden field that only automated spam fills in
            return notice("Thank you", "Your message is on its way.")
        reason = request.form.get("reason", "")
        note = " ".join(request.form.get("note", "").split())[:MAX_NOTE]
        if reason not in REASONS:
            flash("Please choose what you'd like to tell the owner.")
            return redirect(url_for("scan", code=car["code"]))

        # Limits on how often the address is shown, so it can't be harvested or hammered.
        device = device_fingerprint()
        if (
            count_events("reveal", car["id"], device) >= MAX_REVEALS_PER_DEVICE_PER_HOUR
            or count_events("reveal", car["id"]) >= MAX_REVEALS_PER_CAR_PER_HOUR
        ):
            return notice("Too many messages", "This car has had several messages recently. Please try again later.", 429)
        record_event("reveal", car["id"], device)

        if reason == "other":
            subject, lines = "A message about your car", []
        else:
            subject, lines = REASONS[reason], [REASONS[reason] + "."]
        if note:
            lines.append(note)
        if not lines:
            lines.append("I need to reach you about your car.")
        lines.append(f"(Sent by scanning the {app.config['APP_NAME']} sticker on your car.)")
        body = "\n\n".join(lines)
        return render_template(
            "compose.html",
            email=car["email"],
            subject=subject,
            body=body,
            link=mailto(car["email"], subject, body),
        )

    @app.errorhandler(404)
    def not_found(_error):
        return notice("Page not found", "This link doesn't lead anywhere. If you scanned a sticker, it may have been retired.", 404)

    return app


if __name__ == "__main__":
    create_app().run(debug=True)

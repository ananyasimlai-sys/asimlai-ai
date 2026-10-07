"""CarNote: reach a car's owner by scanning a QR sticker, without learning who they are.

One file on purpose. The whole product is four ideas:
  1. An owner signs up with only an email address (a link is emailed, no password).
  2. Each car gets a random code, printed as a QR sticker. The code says nothing about the owner.
  3. Anyone who scans the sticker can send a short message. We email it to the owner.
  4. The owner taps a ready-made reply, which appears on the scanner's screen.
"""

import hashlib
import hmac
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

import segno
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup

# What a scanner can say. Ready-made choices are quicker to send and harder to abuse.
REASONS = {
    "blocking": "Your car is blocking me",
    "lights": "Your lights are on",
    "window": "A window or door is open",
    "damage": "Your car has been damaged",
    "alarm": "Your alarm is going off",
    "other": "Something else",
}

# What an owner can answer. No free text, so an owner cannot reveal themselves by accident.
REPLIES = {
    "5min": "On my way, about 5 minutes",
    "15min": "On my way, about 15 minutes",
    "thanks": "Thanks for letting me know, I'll sort it",
    "away": "I'm not nearby, but I'll deal with it as soon as I can",
}

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I/L, easy to read aloud
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINK_RE = re.compile(r"https?://|www\.", re.I)

LOGIN_LINK_MINUTES = 30
REPLY_LINK_DAYS = 7
MAX_NOTE = 280
MAX_CARS = 5
MAX_PER_SENDER_PER_HOUR = 3
MAX_PER_CAR_PER_HOUR = 10

VERSION = "4"  # shown at /status, so you can tell which version of the code is live

SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cars (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES owners(id),
    code TEXT NOT NULL UNIQUE,
    nickname TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    car_id INTEGER NOT NULL REFERENCES cars(id),
    thread TEXT NOT NULL UNIQUE,
    sender TEXT NOT NULL,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    reply TEXT,
    replied_at TEXT,
    reported INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_requests (
    email TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


# ---------- small helpers ----------

def stamp(moment=None):
    """A UTC timestamp as text. The fixed format means text comparison equals time comparison."""
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kwargs):
    return stamp(datetime.now(timezone.utc) - timedelta(**kwargs))


_copy_lock = threading.Lock()


class SavedConnection(sqlite3.Connection):
    """A database connection that also backs the data up after every change.

    Hosts like Cloud Run only keep files that live on a mounted storage bucket, and SQLite
    cannot work on such a bucket directly (it needs file locking, which buckets lack). So we
    work on a normal local file and copy the whole thing to the bucket after each save.
    """

    def commit(self):
        super().commit()
        durable = current_app.config.get("DURABLE_COPY")
        if not durable:
            return
        try:
            with _copy_lock:
                snapshot = current_app.config["DATABASE"] + ".snapshot"
                target = sqlite3.connect(snapshot)
                self.backup(target)  # a consistent copy, even if another request is writing
                target.close()
                shutil.copyfile(snapshot, durable)
            current_app.config["LAST_BACKUP"] = "ok"
        except Exception as error:
            current_app.config["LAST_BACKUP"] = f"failed ({type(error).__name__})"
            current_app.logger.exception("Could not back up the database to %s", durable)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], factory=SavedConnection)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def signer(purpose):
    """Makes tamper-proof, expiring tokens for the links we put in emails."""
    return URLSafeTimedSerializer(current_app.secret_key, salt=purpose)


def absolute(endpoint, **values):
    base = current_app.config["BASE_URL"] or request.host_url.rstrip("/")
    return base + url_for(endpoint, **values)


def new_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def sender_fingerprint():
    """Identifies a device for rate limiting without storing its IP address."""
    raw = f"{current_app.secret_key}|{request.remote_addr}|{request.user_agent.string}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def notice(title, body, status=200):
    return render_template("notice.html", title=title, body=body), status


def send_email(recipient, subject, body):
    """Sends an email, or in test mode saves it so it can be read at /dev/outbox."""
    config = current_app.config
    if config["DEV_OUTBOX"]:
        db = get_db()
        db.execute(
            "INSERT INTO outbox (recipient, subject, body, created_at) VALUES (?, ?, ?, ?)",
            (recipient, subject, body, stamp()),
        )
        db.commit()
        return
    message = EmailMessage()
    message["From"] = config["MAIL_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        if config["SMTP_PORT"] == 465:
            server = smtplib.SMTP_SSL(config["SMTP_HOST"], 465, timeout=15)
        else:
            server = smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=15)
            server.starttls()
        with server:
            if config["SMTP_USER"]:
                server.login(config["SMTP_USER"], config["SMTP_PASSWORD"])
            server.send_message(message)
    except Exception:
        # The message is already saved and visible on the owner's dashboard.
        current_app.logger.exception("Could not send email")


def create_car(owner_id, nickname):
    db = get_db()
    while True:
        try:
            db.execute(
                "INSERT INTO cars (owner_id, code, nickname, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, new_code(), nickname, stamp()),
            )
            db.commit()
            return
        except sqlite3.IntegrityError:
            continue  # code already taken (astronomically unlikely), pick another


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("owner_id"):
            return redirect(url_for("home"))
        return view(*args, **kwargs)

    return wrapped


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
        SMTP_HOST=env("SMTP_HOST", ""),
        SMTP_PORT=int(env("SMTP_PORT", "587")),
        SMTP_USER=env("SMTP_USER", ""),
        SMTP_PASSWORD=env("SMTP_PASSWORD", ""),
        MAIL_FROM=env("MAIL_FROM", "CarNote <no-reply@localhost>"),
        SECRET_KEY=env("SECRET_KEY", ""),
        SESSION_COOKIE_SAMESITE="Lax",
    )
    app.config.update(test_config or {})
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = local_secret(app.instance_path)
    # With no mail server configured we run in test mode: emails are shown, not sent.
    app.config["DEV_OUTBOX"] = not app.config["SMTP_HOST"]
    app.config["SESSION_COOKIE_SECURE"] = app.config["BASE_URL"].startswith("https")

    if env("TRUST_PROXY") == "1":  # set this when hosted behind a proxy, so we see real visitor IPs
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # On a host with bucket storage: work on a local file, keep DATABASE as the backup location.
    # This is automatic whenever a host sets DATABASE (set WORK_ON_LOCAL_COPY=0 to turn it off).
    wanted = env("WORK_ON_LOCAL_COPY", "1" if env("DATABASE") else "0") == "1"
    app.config.setdefault("WORK_ON_LOCAL_COPY", wanted)
    app.config["LAST_BACKUP"] = "none yet"
    app.config["DURABLE_COPY"] = ""
    if app.config["WORK_ON_LOCAL_COPY"]:
        durable = app.config["DATABASE"]
        working = app.config.get("WORKING_DATABASE") or str(Path(tempfile.gettempdir()) / "carnote-working.sqlite3")
        try:
            if os.path.getsize(durable) > 0:
                shutil.copyfile(durable, working)  # pick up where the last run left off
        except OSError:
            pass  # no backup yet: this is the first run
        app.config.update(DATABASE=working, DURABLE_COPY=durable)

    try:
        with sqlite3.connect(app.config["DATABASE"]) as db:
            db.executescript(SCHEMA)
            db.execute("SELECT COUNT(*) FROM owners").fetchone()
    except sqlite3.DatabaseError:
        if not app.config["DURABLE_COPY"]:
            raise
        # The backup we restored is damaged. Set it aside and start clean rather than crash.
        app.logger.error("Restored database was unreadable; starting with an empty one")
        os.replace(app.config["DATABASE"], app.config["DATABASE"] + ".damaged")
        with sqlite3.connect(app.config["DATABASE"]) as db:
            db.executescript(SCHEMA)

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def template_globals():
        return {
            "app_name": app.config["APP_NAME"],
            "dev_outbox": app.config["DEV_OUTBOX"],
            "csrf_token": csrf_token,
            "REASONS": REASONS,
            "REPLIES": REPLIES,
        }

    @app.template_filter("when")
    def when(value):
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return f"{moment.day} {moment:%b}, {moment:%H:%M} UTC"

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

    # ----- owner: sign up and sign in -----

    @app.get("/")
    def home():
        if session.get("owner_id"):
            return redirect(url_for("dashboard"))
        return render_template("home.html")

    @app.post("/start")
    def start():
        email = request.form.get("email", "").strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            flash("That doesn't look like an email address. Please check it.")
            return redirect(url_for("home"))
        db = get_db()
        recent = db.execute(
            "SELECT COUNT(*) FROM login_requests WHERE email = ? AND created_at > ?",
            (email, ago(minutes=15)),
        ).fetchone()[0]
        if recent < 3:  # stops someone flooding an inbox with sign-in links
            db.execute("INSERT INTO login_requests VALUES (?, ?)", (email, stamp()))
            db.commit()
            link = absolute("login", token=signer("login").dumps(email))
            send_email(
                email,
                f"Your {app.config['APP_NAME']} sign-in link",
                f"Open this link to get your QR sticker:\n\n{link}\n\n"
                f"It works for {LOGIN_LINK_MINUTES} minutes. "
                "If you didn't ask for it, you can ignore this email.",
            )
        return notice(
            "Check your email",
            f"We've sent a sign-in link to {email}. Open it on this device to get your QR sticker.",
        )

    @app.get("/login/<token>")
    def login(token):
        try:
            email = signer("login").loads(token, max_age=LOGIN_LINK_MINUTES * 60)
        except BadSignature:  # also covers expired links
            return notice("That link has expired", "Go back to the home page and ask for a new one.", 400)
        db = get_db()
        owner = db.execute("SELECT id FROM owners WHERE email = ?", (email,)).fetchone()
        if owner is None:
            # The account is created only now, once we know the address is really theirs.
            db.execute("INSERT INTO owners (email, created_at) VALUES (?, ?)", (email, stamp()))
            db.commit()
            owner = db.execute("SELECT id FROM owners WHERE email = ?", (email,)).fetchone()
            create_car(owner["id"], "My car")  # so the QR code is ready straight away
        session.clear()
        session["owner_id"] = owner["id"]
        session.permanent = True
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    # ----- owner: dashboard, cars, stickers -----

    def own_car(car_id):
        car = get_db().execute(
            "SELECT * FROM cars WHERE id = ? AND owner_id = ?", (car_id, session["owner_id"])
        ).fetchone()
        if car is None:
            abort(404)
        return car

    @app.get("/dashboard")
    @login_required
    def dashboard():
        db = get_db()
        cars = []
        for car in db.execute("SELECT * FROM cars WHERE owner_id = ? ORDER BY id", (session["owner_id"],)):
            messages = [
                {**dict(row), "token": signer("reply").dumps(row["id"])}
                for row in db.execute(
                    "SELECT * FROM messages WHERE car_id = ? ORDER BY id DESC LIMIT 10", (car["id"],)
                )
            ]
            cars.append({**dict(car), "messages": messages})
        return render_template("dashboard.html", cars=cars, max_cars=MAX_CARS)

    @app.post("/cars")
    @login_required
    def add_car():
        count = get_db().execute(
            "SELECT COUNT(*) FROM cars WHERE owner_id = ?", (session["owner_id"],)
        ).fetchone()[0]
        if count >= MAX_CARS:
            flash(f"You can have up to {MAX_CARS} cars.")
        else:
            nickname = " ".join(request.form.get("nickname", "").split())[:40] or "My car"
            create_car(session["owner_id"], nickname)
        return redirect(url_for("dashboard"))

    @app.post("/cars/<int:car_id>/pause")
    @login_required
    def toggle_pause(car_id):
        car = own_car(car_id)
        db = get_db()
        db.execute("UPDATE cars SET paused = ? WHERE id = ?", (0 if car["paused"] else 1, car_id))
        db.commit()
        return redirect(url_for("dashboard"))

    @app.get("/cars/<int:car_id>/sticker")
    @login_required
    def sticker(car_id):
        car = own_car(car_id)
        link = absolute("scan", code=car["code"])
        qr = segno.make(link, error="m").svg_inline(border=2, omitsize=True)
        return render_template("sticker.html", car=car, link=link, qr=Markup(qr))

    # ----- scanner: send a message, wait for a reply -----

    def find_car(code):
        car = get_db().execute("SELECT * FROM cars WHERE code = ?", (code.upper(),)).fetchone()
        if car is None:
            abort(404)
        return car

    @app.get("/c/<code>")
    def scan(code):
        return render_template("scan.html", car=find_car(code), max_note=MAX_NOTE)

    @app.post("/c/<code>")
    def send_message(code):
        car = find_car(code)
        back = redirect(url_for("scan", code=car["code"]))
        if car["paused"]:
            return back
        if request.form.get("website"):  # hidden field that only automated spam fills in
            return notice("Message sent", "Thank you.")
        reason = request.form.get("reason", "")
        note = " ".join(request.form.get("note", "").split())[:MAX_NOTE]
        if reason not in REASONS:
            flash("Please choose what you'd like to tell the owner.")
            return back
        if LINK_RE.search(note):
            flash("Links aren't allowed in the note. Please describe it in words.")
            return back

        db = get_db()
        sender = sender_fingerprint()
        blocked = db.execute(
            "SELECT 1 FROM messages WHERE car_id = ? AND sender = ? AND reported = 1 LIMIT 1",
            (car["id"], sender),
        ).fetchone()
        from_sender = db.execute(
            "SELECT COUNT(*) FROM messages WHERE car_id = ? AND sender = ? AND created_at > ?",
            (car["id"], sender, ago(hours=1)),
        ).fetchone()[0]
        to_car = db.execute(
            "SELECT COUNT(*) FROM messages WHERE car_id = ? AND created_at > ?",
            (car["id"], ago(hours=1)),
        ).fetchone()[0]
        if blocked or from_sender >= MAX_PER_SENDER_PER_HOUR or to_car >= MAX_PER_CAR_PER_HOUR:
            return notice(
                "Too many messages",
                "This car has had several messages recently. Please try again later.",
                429,
            )

        thread = secrets.token_urlsafe(18)
        cursor = db.execute(
            "INSERT INTO messages (car_id, thread, sender, reason, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (car["id"], thread, sender, reason, note, stamp()),
        )
        db.commit()

        owner = db.execute("SELECT email FROM owners WHERE id = ?", (car["owner_id"],)).fetchone()
        reply_link = absolute("reply", token=signer("reply").dumps(cursor.lastrowid))
        quoted_note = f'\n    "{note}"' if note else ""
        send_email(
            owner["email"],
            f"{car['nickname']}: {REASONS[reason]}",
            f"Someone scanned the sticker on {car['nickname']} and sent you this:\n\n"
            f"    {REASONS[reason]}{quoted_note}\n\n"
            f"Reply with one tap. They will not see your name or email address:\n{reply_link}\n\n"
            "The same link lets you report this message or pause your sticker.",
        )
        return redirect(url_for("thread", thread=thread))

    def find_thread(thread):
        message = get_db().execute("SELECT * FROM messages WHERE thread = ?", (thread,)).fetchone()
        if message is None:
            abort(404)
        return message

    @app.get("/t/<thread>")
    def thread(thread):
        return render_template("thread.html", m=find_thread(thread))

    @app.get("/t/<thread>/status")
    def thread_status(thread):
        return jsonify(reply=REPLIES.get(find_thread(thread)["reply"]))

    # ----- owner: reply from the email link (no sign-in needed) -----

    def message_for(token):
        try:
            message_id = signer("reply").loads(token, max_age=REPLY_LINK_DAYS * 86400)
        except BadSignature:
            abort(404)
        row = get_db().execute(
            "SELECT m.*, c.nickname, c.paused FROM messages m JOIN cars c ON c.id = m.car_id WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            abort(404)
        return row

    @app.get("/r/<token>")
    def reply(token):
        return render_template("reply.html", m=message_for(token), token=token)

    @app.post("/r/<token>")
    def save_reply(token):
        message = message_for(token)
        db = get_db()
        choice = request.form.get("reply")
        action = request.form.get("action")
        if choice in REPLIES and not message["reply"]:
            db.execute(
                "UPDATE messages SET reply = ?, replied_at = ? WHERE id = ?",
                (choice, stamp(), message["id"]),
            )
        elif action == "report":  # also blocks that device from messaging this car again
            db.execute("UPDATE messages SET reported = 1 WHERE id = ?", (message["id"],))
        elif action == "pause":
            db.execute("UPDATE cars SET paused = 1 WHERE id = ?", (message["car_id"],))
        db.commit()
        return redirect(url_for("reply", token=token))

    # ----- test mode only -----

    @app.get("/dev/outbox")
    def outbox():
        if not app.config["DEV_OUTBOX"]:
            abort(404)
        emails = get_db().execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 20").fetchall()
        return render_template("outbox.html", emails=emails)

    @app.get("/status")
    def status():
        """A quick health report. It shows no private details."""
        return jsonify(
            version=VERSION,
            storage="local copy, backed up" if app.config["DURABLE_COPY"] else "direct file",
            last_backup=app.config["LAST_BACKUP"] if app.config["DURABLE_COPY"] else "not used",
            email="test mode" if app.config["DEV_OUTBOX"] else "sending",
        )

    @app.errorhandler(404)
    def not_found(_error):
        return notice("Page not found", "This link doesn't lead anywhere. If you scanned a sticker, it may have been retired.", 404)

    return app


if __name__ == "__main__":
    create_app().run(debug=True)

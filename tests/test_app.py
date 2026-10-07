"""Tests that walk through the product the way real people would use it."""

import re
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import MAX_PER_SENDER_PER_HOUR, create_app  # noqa: E402

OWNER_EMAIL = "owner@example.com"


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test-secret",
            "SMTP_HOST": "",
            "BASE_URL": "http://localhost",
        }
    )


def csrf(client):
    client.get("/", follow_redirects=True)
    with client.session_transaction() as current:
        return current["csrf"]


def post(client, path, **form):
    return client.post(path, data={"csrf": csrf(client), **form})


def emails(app):
    with sqlite3.connect(app.config["DATABASE"]) as db:
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM outbox ORDER BY id").fetchall()


def link_in(email):
    return re.search(r"http://localhost(/\S+)", email["body"]).group(1)


def sign_up(app, email=OWNER_EMAIL):
    """Returns a signed-in owner's browser and their car's sticker code."""
    owner = app.test_client()
    post(owner, "/start", email=email)
    owner.get(link_in(emails(app)[-1]))
    code = re.search(r"Sticker code (\w+)", owner.get("/dashboard").text).group(1)
    return owner, code


def send(client, code, reason="blocking", note=""):
    return post(client, f"/c/{code}", reason=reason, note=note)


def test_whole_conversation_from_sticker_to_reply(app):
    owner, code = sign_up(app)
    assert f"/c/{code}" in owner.get("/cars/1/sticker").text

    scanner = app.test_client()
    assert "Message this car" in scanner.get(f"/c/{code}").text
    sent = send(scanner, code, note="Grey van behind you")
    thread_path = sent.headers["Location"]
    assert "Waiting for the owner" in scanner.get(thread_path).text
    assert scanner.get(thread_path + "/status").json == {"reply": None}

    email = emails(app)[-1]
    assert email["recipient"] == OWNER_EMAIL
    assert "Your car is blocking me" in email["body"]
    assert "Grey van behind you" in email["body"]

    # The owner replies from the email on a device where they are not signed in.
    phone = app.test_client()
    reply_path = link_in(email)
    assert "Tap a reply" in phone.get(reply_path).text
    post(phone, reply_path, reply="5min")
    assert scanner.get(thread_path + "/status").json == {"reply": "On my way, about 5 minutes"}
    assert "You replied" in owner.get("/dashboard").text


def test_scanner_never_sees_owner_details(app):
    _owner, code = sign_up(app)
    scanner = app.test_client()
    pages = [scanner.get(f"/c/{code}").text]
    thread_path = send(scanner, code).headers["Location"]
    pages.append(scanner.get(thread_path).text)
    for page in pages:
        assert OWNER_EMAIL not in page
        assert "My car" not in page  # the nickname is private too


def test_owner_pages_need_sign_in(app):
    _owner, _code = sign_up(app)
    stranger = app.test_client()
    assert stranger.get("/dashboard").status_code == 302
    assert stranger.get("/cars/1/sticker").status_code == 302


def test_owner_cannot_touch_someone_elses_car(app):
    sign_up(app)
    other, _ = sign_up(app, "other@example.com")
    assert other.get("/cars/1/sticker").status_code == 404
    assert post(other, "/cars/1/pause").status_code == 404


def test_paused_sticker_accepts_no_messages(app):
    owner, code = sign_up(app)
    post(owner, "/cars/1/pause")
    scanner = app.test_client()
    assert "Messages are paused" in scanner.get(f"/c/{code}").text
    before = len(emails(app))
    send(scanner, code)
    assert len(emails(app)) == before


def test_one_device_cannot_flood_a_car(app):
    _owner, code = sign_up(app)
    scanner = app.test_client()
    for _ in range(MAX_PER_SENDER_PER_HOUR):
        assert send(scanner, code).status_code == 302
    assert send(scanner, code).status_code == 429


def test_reporting_blocks_that_device(app):
    _owner, code = sign_up(app)
    scanner = app.test_client()
    send(scanner, code, reason="other", note="rude words")
    post(app.test_client(), link_in(emails(app)[-1]), action="report")
    assert send(scanner, code).status_code == 429


def test_links_in_notes_are_refused(app):
    _owner, code = sign_up(app)
    before = len(emails(app))
    send(app.test_client(), code, note="click https://bad.example")
    assert len(emails(app)) == before


def test_forms_without_our_token_are_refused(app):
    _owner, code = sign_up(app)
    assert app.test_client().post(f"/c/{code}", data={"reason": "lights"}).status_code == 400


def test_bad_links_lead_nowhere(app):
    client = app.test_client()
    assert client.get("/c/NOSUCHCAR").status_code == 404
    assert client.get("/r/forged-token").status_code == 404
    assert client.get("/login/forged-token").status_code == 400


def test_owner_can_add_a_second_car(app):
    owner, _code = sign_up(app)
    assert post(owner, "/cars", nickname="Blue Golf").status_code == 302
    assert "Blue Golf" in owner.get("/dashboard").text


def hosted_app(tmp_path):
    """The app as it runs on a host: local working file, backed up to a 'bucket' folder."""
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "bucket" / "carnote.sqlite3"),
            "WORKING_DATABASE": str(tmp_path / "working.sqlite3"),
            "WORK_ON_LOCAL_COPY": True,
            "SECRET_KEY": "test-secret",
            "SMTP_HOST": "",
            "BASE_URL": "http://localhost",
        }
    )


def test_data_survives_a_restart_on_a_host(tmp_path):
    (tmp_path / "bucket").mkdir()
    first = hosted_app(tmp_path)
    _owner, code = sign_up(first)

    (tmp_path / "working.sqlite3").unlink()  # the host throws the local file away on restart
    second = hosted_app(tmp_path)
    assert "Message this car" in second.test_client().get(f"/c/{code}").text


def test_a_damaged_backup_does_not_stop_the_site(tmp_path):
    (tmp_path / "bucket").mkdir()
    (tmp_path / "bucket" / "carnote.sqlite3").write_text("this is not a database")
    app = hosted_app(tmp_path)
    _owner, code = sign_up(app)
    assert app.test_client().get(f"/c/{code}").status_code == 200


def test_site_keeps_working_if_the_backup_location_fails(tmp_path):
    app = hosted_app(tmp_path)  # the 'bucket' folder does not exist, so every backup fails
    _owner, code = sign_up(app)
    assert app.test_client().get(f"/c/{code}").status_code == 200

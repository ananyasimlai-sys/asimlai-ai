"""Tests that walk through the product the way real people would use it."""

import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import MAX_REVEALS_PER_DEVICE_PER_HOUR, MAX_STICKERS_PER_DEVICE_PER_HOUR, create_app  # noqa: E402

OWNER_EMAIL = "owner@example.com"


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test-secret",
            "BASE_URL": "http://localhost",
        }
    )


def csrf(client):
    client.get("/")
    with client.session_transaction() as current:
        return current["csrf"]


def post(client, path, **form):
    return client.post(path, data={"csrf": csrf(client), **form})


def make_sticker(app, email=OWNER_EMAIL):
    """Returns the owner's browser, their private manage path, and the sticker code."""
    owner = app.test_client()
    manage_path = post(owner, "/start", email=email).headers["Location"]
    code = re.search(r"/c/(\w+)", owner.get(manage_path).text).group(1)
    return owner, manage_path, code


def test_owner_gets_a_sticker_from_just_an_email(app):
    owner, manage_path, code = make_sticker(app)
    page = owner.get(manage_path).text
    assert "<svg" in page  # the QR code
    assert f"http://localhost/c/{code}" in page
    assert f"Sticker {code}" in owner.get("/").text  # remembered on this device


def test_scanner_gets_a_ready_to_send_email(app):
    _owner, _manage, code = make_sticker(app)
    scanner = app.test_client()
    assert "Message this car" in scanner.get(f"/c/{code}").text

    page = post(scanner, f"/c/{code}", reason="blocking", note="Grey van behind you").text
    link = unquote(re.search(r'href="(mailto:[^"]+)"', page).group(1))
    assert link.startswith(f"mailto:{OWNER_EMAIL}?subject=Your car is blocking me&amp;body=")
    assert "Grey van behind you" in link


def test_address_is_hidden_until_a_message_is_written(app):
    _owner, _manage, code = make_sticker(app)
    assert OWNER_EMAIL not in app.test_client().get(f"/c/{code}").text


def test_paused_sticker_reveals_nothing(app):
    owner, manage_path, code = make_sticker(app)
    post(owner, manage_path, action="pause")
    scanner = app.test_client()
    assert "Messages are paused" in scanner.get(f"/c/{code}").text
    assert OWNER_EMAIL not in post(scanner, f"/c/{code}", reason="lights").text

    post(owner, manage_path, action="resume")
    assert OWNER_EMAIL in post(scanner, f"/c/{code}", reason="lights").text


def test_deleting_removes_the_sticker_and_the_address(app):
    owner, manage_path, code = make_sticker(app)
    assert "Sticker deleted" in post(owner, manage_path, action="delete").text
    assert owner.get(manage_path).status_code == 404
    assert app.test_client().get(f"/c/{code}").status_code == 404


def test_one_device_cannot_harvest_or_hammer_an_address(app):
    _owner, _manage, code = make_sticker(app)
    scanner = app.test_client()
    for _ in range(MAX_REVEALS_PER_DEVICE_PER_HOUR):
        assert post(scanner, f"/c/{code}", reason="lights").status_code == 200
    blocked = post(scanner, f"/c/{code}", reason="lights")
    assert blocked.status_code == 429
    assert OWNER_EMAIL not in blocked.text


def test_one_device_cannot_mass_produce_stickers(app):
    client = app.test_client()
    for number in range(MAX_STICKERS_PER_DEVICE_PER_HOUR):
        assert post(client, "/start", email=f"car{number}@example.com").status_code == 302
    assert post(client, "/start", email="onemore@example.com").status_code == 429


@pytest.mark.parametrize("bad", ["", "not-an-email", "a@b", "a@b.com?bcc=evil@x.com", "a b@c.com", "a@b.com\nBcc: x@y.com"])
def test_bad_or_sneaky_email_addresses_are_refused(app, bad):
    client = app.test_client()
    post(client, "/start", email=bad)
    assert "Stickers made on this device" not in client.get("/").text


def test_forms_without_our_token_are_refused(app):
    _owner, manage_path, code = make_sticker(app)
    stranger = app.test_client()
    assert stranger.post(f"/c/{code}", data={"reason": "lights"}).status_code == 400
    assert stranger.post(manage_path, data={"action": "delete"}).status_code == 400


def test_bad_links_lead_nowhere(app):
    client = app.test_client()
    assert client.get("/c/NOSUCHCAR").status_code == 404
    assert client.get("/m/guessed-token").status_code == 404

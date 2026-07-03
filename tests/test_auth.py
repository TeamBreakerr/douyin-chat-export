"""Characterization tests for the auth middleware + login flow.

TestClient is used WITHOUT the context manager on purpose, so the app's
startup event (which would restore the real cron schedule from the live
panel_config.json) never fires.
"""
import pytest
from fastapi.testclient import TestClient

import backend.main as main


@pytest.fixture
def client():
    return TestClient(main.app)


def _set_password(monkeypatch, plaintext):
    monkeypatch.setattr(main, "_get_password_hash",
                        lambda: main._hash_password(plaintext))


def test_no_password_allows_all(client, monkeypatch, temp_db):
    monkeypatch.setattr(main, "_get_password_hash", lambda: None)
    assert client.get("/api/auth/check").json() == {
        "need_password": False, "authenticated": True}
    assert client.get("/api/stats").status_code == 200


def test_protected_route_blocked_without_token(client, monkeypatch, temp_db):
    _set_password(monkeypatch, "secret")
    r = client.get("/api/stats")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_public_paths_bypass_auth(client, monkeypatch, temp_db):
    _set_password(monkeypatch, "secret")
    # /media, /assets, /favicon.svg, /panel HTML, /api/auth/* are public → not 401
    for path in ("/api/auth/check", "/panel", "/panel/"):
        assert client.get(path).status_code != 401
    # /media/<missing> is public: 404 (from StaticFiles), never 401
    assert client.get("/media/nope.png").status_code != 401


def test_login_and_token_flow(client, monkeypatch, temp_db):
    _set_password(monkeypatch, "secret")
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 403
    r = client.post("/api/auth/login", json={"password": "secret"})
    assert r.status_code == 200
    token = r.json()["token"]
    # token grants access via Authorization header
    ok = client.get("/api/stats", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200
    # and auth/check reports authenticated
    chk = client.get("/api/auth/check", headers={"Authorization": f"Bearer {token}"})
    assert chk.json() == {"need_password": True, "authenticated": True}


def test_token_accepted_without_bearer_prefix(client, monkeypatch, temp_db):
    _set_password(monkeypatch, "secret")
    token = client.post("/api/auth/login", json={"password": "secret"}).json()["token"]
    # removeprefix('Bearer ') is a no-op when there's no prefix → raw token works
    ok = client.get("/api/stats", headers={"Authorization": token})
    assert ok.status_code == 200


def test_login_when_no_password_returns_400(client, monkeypatch):
    monkeypatch.setattr(main, "_get_password_hash", lambda: None)
    r = client.post("/api/auth/login", json={"password": "x"})
    assert r.status_code == 400
    assert r.json() == {"error": "no password set"}

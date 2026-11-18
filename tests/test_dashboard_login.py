"""Signing in to the dashboard (POST /api/auth/login and friends).

The deployed dashboard is reached over the network, so a username and
password is how a person gets in. What a sign-in hands back used to be the
dashboard's own token — the same one for everybody, with no expiry and no way
to end it. These cover what it hands back now, what it refuses, and that the
master token is never one of the things it can give away.
"""

import os

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird import sessions

ORIGIN = "http://127.0.0.1:5555"
PASSWORD = "a-long-enough-test-password"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "sellbird.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    dash.session_store.configure(tmp_path / "data" / "sessions.json")
    # A fresh throttle per test, so one test's failures never lock another out.
    monkeypatch.setattr(dash, "login_throttle", sessions.LoginThrottle())
    monkeypatch.setenv("DASHBOARD_USER", "operator")
    monkeypatch.setenv("DASHBOARD_PASS", PASSWORD)
    monkeypatch.delenv("DASHBOARD_PASS_HASH", raising=False)
    return {"dir": tmp_path, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN) as c:
        yield c


def sign_in(client, user="operator", password=PASSWORD):
    return client.post("/api/auth/login", json={"username": user, "password": password})


# ── What a sign-in gives you ──


def test_the_right_credentials_give_a_session(client):
    r = sign_in(client)
    assert r.status_code == 200
    body = r.json()
    assert len(body["token"]) >= 43
    assert body["expires_in"] == sessions.MAX_SECONDS


def test_a_sign_in_never_hands_out_the_master_token(client, env):
    """The whole point. The master token is a file on the server, used by the
    CLI and printed in a terminal; a password must not be an alias for it."""
    assert sign_in(client).json()["token"] != env["token"]


def test_two_sign_ins_are_two_different_sessions(client):
    assert sign_in(client).json()["token"] != sign_in(client).json()["token"]


def test_a_session_token_opens_the_api(client):
    token = sign_in(client).json()["token"]
    r = client.get("/api/sending-mode", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_the_api_stays_shut_without_one(client):
    assert client.get("/api/sending-mode").status_code == 401


def test_a_made_up_session_token_is_refused(client):
    r = client.get("/api/sending-mode", headers={"Authorization": "Bearer " + "x" * 43})
    assert r.status_code == 401


# ── What is refused ──


@pytest.mark.parametrize("user, password", [
    ("operator", "wrong"),
    ("wrong", PASSWORD),
    ("", ""),
    ("operator", ""),
])
def test_wrong_credentials_are_refused(client, user, password):
    r = sign_in(client, user, password)
    assert r.status_code == 401
    assert "token" not in r.json()


def test_the_reply_does_not_say_which_half_was_wrong(client):
    """A different message for a bad name would turn the login into a
    directory of valid usernames."""
    bad_name = sign_in(client, "nobody", PASSWORD).json()
    bad_pass = sign_in(client, "operator", "nope").json()
    assert bad_name == bad_pass


def test_a_body_that_is_not_an_object_is_refused(client):
    r = client.post("/api/auth/login", json=["operator", PASSWORD])
    assert r.status_code == 400


def test_login_says_so_when_it_is_not_configured(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    r = sign_in(client)
    assert r.status_code == 503
    assert "DASHBOARD_USER" in r.json()["message"]


def test_guessing_is_throttled(client):
    for _ in range(sessions.LoginThrottle.PER_SOURCE):
        assert sign_in(client, password="wrong").status_code == 401
    r = sign_in(client, password="wrong")
    assert r.status_code == 429
    assert r.headers["Retry-After"].isdigit()
    # And the right password is refused too while it is locked: a throttle
    # that let the correct guess through would not be a throttle.
    assert sign_in(client).status_code == 429


def test_signing_in_clears_the_count(client):
    for _ in range(sessions.LoginThrottle.PER_SOURCE - 1):
        sign_in(client, password="wrong")
    assert sign_in(client).status_code == 200
    for _ in range(sessions.LoginThrottle.PER_SOURCE - 1):
        assert sign_in(client, password="wrong").status_code == 401


# ── A hashed password ──


def test_a_hashed_password_works(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASS_HASH", sessions.hash_password("hashed-" + PASSWORD))
    monkeypatch.setenv("DASHBOARD_PASS", "something-else-entirely")
    assert sign_in(client, password="hashed-" + PASSWORD).status_code == 200


def test_the_plain_password_is_ignored_once_a_hash_is_set(client, monkeypatch):
    """Otherwise setting the hash and forgetting to remove the plain value
    would leave the weaker of the two still working."""
    monkeypatch.setenv("DASHBOARD_PASS_HASH", sessions.hash_password("the-real-one"))
    monkeypatch.setenv("DASHBOARD_PASS", PASSWORD)
    assert sign_in(client, password=PASSWORD).status_code == 401
    assert sign_in(client, password="the-real-one").status_code == 200


def test_a_hash_only_setup_still_counts_as_configured(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASS", raising=False)
    monkeypatch.setenv("DASHBOARD_PASS_HASH", sessions.hash_password("only-the-hash"))
    assert dash.login_configured() is True
    assert sign_in(client, password="only-the-hash").status_code == 200


# ── Signing out ──


def test_signing_out_ends_the_session(client):
    token = sign_in(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=headers).json()["ended"] is True
    assert client.get("/api/sending-mode", headers=headers).status_code == 401


def test_signing_out_leaves_other_sessions_alone(client):
    mine = sign_in(client).json()["token"]
    theirs = sign_in(client).json()["token"]
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {mine}"})
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {theirs}"}).status_code == 200


def test_signing_out_with_the_master_token_ends_nothing(client, env):
    """It is a file on the server. Pretending to end it would be a lie, and
    deleting it is a different decision made somewhere else."""
    r = client.post("/api/auth/logout",
                    headers={"Authorization": f"Bearer {env['token']}"})
    assert r.json()["ended"] is False
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {env['token']}"}).status_code == 200


def test_every_browser_can_be_signed_out_at_once(client, env):
    tokens = [sign_in(client).json()["token"] for _ in range(3)]
    r = client.request("DELETE", "/api/auth/sessions",
                       headers={"Authorization": f"Bearer {env['token']}"})
    assert r.json()["ended"] == 3
    for token in tokens:
        assert client.get("/api/sending-mode",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ── Who am I ──


def test_me_tells_a_session_from_the_master_token(client, env):
    token = sign_in(client).json()["token"]
    as_session = client.get("/api/auth/me",
                            headers={"Authorization": f"Bearer {token}"}).json()
    assert as_session["kind"] == "session" and as_session["principal"] == "user"
    assert as_session["session"]["expires_at"] > as_session["session"]["created_at"]

    as_token = client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {env['token']}"}).json()
    assert as_token["kind"] == "token" and "session" not in as_token


def test_me_needs_a_credential_like_everything_else(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_never_includes_a_token(client):
    token = sign_in(client).json()["token"]
    body = client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {token}"}).text
    assert token not in body


# ── What the locked page is allowed to know ──


def test_the_instance_route_says_how_to_get_in(client, env):
    r = client.get("/api/instance")
    assert r.status_code == 200
    body = r.json()
    assert body["login_enabled"] is True
    assert env["token"] not in r.text


def test_the_instance_route_says_when_there_is_no_sign_in(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    assert client.get("/api/instance").json()["login_enabled"] is False


def test_the_sign_in_page_is_served_without_a_token(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "login-form" in r.text


# ── The agent's token is not a person ──


def test_the_coding_agent_cannot_list_or_revoke_sessions(env, tmp_path):
    dash.agent_token_store.configure(tmp_path / "data" / "agent-token")
    agent = dash.agent_token_store.get()
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {agent}"}) as c:
        assert c.get("/api/auth/sessions").status_code == 403
        assert c.request("DELETE", "/api/auth/sessions").status_code == 403


# ── TLS ──


def test_a_request_over_tls_is_told_to_stay_on_tls(client):
    r = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert "max-age=" in r.headers["strict-transport-security"]


def test_a_plain_local_dashboard_is_not(client):
    """A local http dashboard that sent HSTS would make itself unreachable."""
    assert "strict-transport-security" not in client.get("/").headers

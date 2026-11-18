"""Signing in to the dashboard (POST /api/auth/login and friends).

The deployed dashboard is reached over the network, so a username and
password is how a person gets in. What a sign-in hands back used to be the
dashboard's own token — the same one for everybody, with no expiry and no way
to end it. These cover what it hands back now, what it refuses, and that the
master token is never one of the things it can give away.
"""

import json
import os
from pathlib import Path

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


# ── Changing the password is how access is taken away ──


def test_changing_the_password_ends_every_sign_in(client, monkeypatch):
    """The point: an organisation takes someone's access away by changing the
    password, and that has to end their open sessions without anyone
    remembering to revoke anything."""
    token = sign_in(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/sending-mode", headers=headers).status_code == 200

    monkeypatch.setenv("DASHBOARD_PASS", "a-different-long-password")
    assert client.get("/api/sending-mode", headers=headers).status_code == 401


def test_changing_the_username_ends_them_too(client, monkeypatch):
    token = sign_in(client).json()["token"]
    monkeypatch.setenv("DASHBOARD_USER", "someone-else")
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_moving_from_a_plain_password_to_its_hash_ends_them(client, monkeypatch):
    """Even though the password itself hasn't changed: the credential has,
    and keeping the old sessions alive would mean the plain value still
    mattered after it was supposed to be gone."""
    token = sign_in(client).json()["token"]
    monkeypatch.setenv("DASHBOARD_PASS_HASH", sessions.hash_password(PASSWORD))
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_the_new_password_signs_in_as_normal(client, monkeypatch):
    sign_in(client)
    monkeypatch.setenv("DASHBOARD_PASS", "a-different-long-password")
    fresh = sign_in(client, password="a-different-long-password")
    assert fresh.status_code == 200
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {fresh.json()['token']}"}
                      ).status_code == 200


def test_an_unchanged_password_leaves_sessions_alone(client, monkeypatch):
    """A redeploy that sets the same values must not sign everyone out."""
    token = sign_in(client).json()["token"]
    monkeypatch.setenv("DASHBOARD_PASS", PASSWORD)          # same value again
    monkeypatch.setenv("DASHBOARD_USER", "operator")
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_the_master_token_is_not_affected_by_a_password_change(client, env, monkeypatch):
    """It is a file on the server and the CLI uses it; a password it was
    never connected to must not disable it."""
    monkeypatch.setenv("DASHBOARD_PASS", "a-different-long-password")
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {env['token']}"}).status_code == 200


def test_what_is_signed_in_can_be_listed(client, env):
    sign_in(client)
    sign_in(client)
    body = client.get("/api/auth/sessions",
                      headers={"Authorization": f"Bearer {env['token']}"}).json()
    assert len(body["sessions"]) == 2
    assert body["idle_timeout"] == sessions.IDLE_SECONDS
    for row in body["sessions"]:
        assert set(row) >= {"id", "created_at", "expires_at", "label"}
        assert "credential" not in json.dumps(row) or row.get("credential") is None


@pytest.mark.parametrize("ua, expected", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
     "Gecko) Chrome/141.0.0.0 Safari/537.36", "Chrome"),
    ("Mozilla/5.0 ... Chrome/141 Safari/537.36 Edg/141.0.0.0", "Edge"),
    ("Mozilla/5.0 ... Chrome/141 Safari/537.36 OPR/121.0.0.0", "Opera"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0", "Firefox"),
    ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15", "Safari"),
    ("curl/8.5.0", ""),
    ("", ""),
])
def test_a_sign_in_records_the_browsers_name(ua, expected):
    """Keeping the first 80 characters of a user agent kept only boilerplate
    — the name comes later in the string — and a whole user agent is more
    about the person than this needs to hold."""
    assert dash.browser_name(ua) == expected


def test_the_stored_label_is_the_name_and_not_the_user_agent(client, env):
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, "
          "like Gecko) Chrome/141.0.0.0 Safari/537.36")
    client.post("/api/auth/login", json={"username": "operator", "password": PASSWORD},
                headers={"User-Agent": ua})
    rows = client.get("/api/auth/sessions",
                      headers={"Authorization": f"Bearer {env['token']}"}).json()["sessions"]
    assert [r["label"] for r in rows] == ["Chrome"]
    assert "Mozilla" not in json.dumps(rows)


# ── Being locked out must always have a way back ──


def test_a_broken_password_does_not_hide_the_token_box():
    """The dashboard token is the credential a deployment's settings cannot
    break. The locked screen used to hide it whenever a password sign-in was
    configured and send the browser to /login instead -- which left exactly
    one door offered, and it was the broken one, when a mangled hash was the
    thing that went wrong."""
    app_js = (Path(__file__).resolve().parent.parent
              / "sellbird" / "web" / "app.js").read_text(encoding="utf-8")
    start = app_js.index("async function lockIfRefused(")
    end = app_js.index(chr(10) + "}" + chr(10), start)   # that function alone
    lock = app_js[start:end]
    # No redirect to the sign-in page from a refused request...
    assert "goSignIn()" not in lock
    # ...and the stored token is never thrown away on the way past.
    assert "clearAuthToken()" not in lock
    # The token box has no display toggle any more; only the sign-in button does.
    assert "lock-token-way" not in app_js


def test_both_ways_in_are_on_the_locked_screen():
    html = (Path(__file__).resolve().parent.parent
            / "sellbird" / "web" / "index.html").read_text(encoding="utf-8")
    lock = html[html.index('id="lock-screen"'):html.index('<div class="app">')]
    assert 'id="lock-token"' in lock and "unlockWithToken" in lock
    assert 'id="lock-login-way"' in lock and "goSignIn()" in lock


def test_the_master_token_works_whatever_the_password_is_doing(client, env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASS_HASH", "scrypt1")   # mangled in transit
    monkeypatch.delenv("DASHBOARD_PASS", raising=False)
    # No password gets in: the hash is unreadable and there is no plain one,
    # so the sign-in reports itself unconfigured rather than wrong.
    assert sign_in(client).status_code == 503
    assert client.get("/api/sending-mode",
                      headers={"Authorization": f"Bearer {env['token']}"}).status_code == 200


# ── The credential can live on the volume ──


def test_a_credential_in_the_env_file_is_used(client, env, monkeypatch):
    """A value has to survive the host's UI, its variable substitution and a
    compose file to reach the process. .env on the data volume skips all of
    it, is written in one line from a terminal, and needs no redeploy."""
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASS", raising=False)
    monkeypatch.delenv("DASHBOARD_PASS_HASH", raising=False)
    dash.ENV_FILE.write_text(
        "DASHBOARD_USER=fromfile\n"
        f"DASHBOARD_PASS_HASH={sessions.hash_password('file-password-here')}\n",
        encoding="utf-8")
    assert dash.login_configured() is True
    assert dash.login_source("DASHBOARD_USER") == ".env"
    r = sign_in(client, "fromfile", "file-password-here")
    assert r.status_code == 200


def test_the_environment_wins_over_the_env_file(client, env, monkeypatch):
    """The host's own settings are what they are for; .env fills gaps."""
    dash.ENV_FILE.write_text("DASHBOARD_USER=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("DASHBOARD_USER", "fromenv")
    assert dash.login_setting("DASHBOARD_USER") == "fromenv"
    assert dash.login_source("DASHBOARD_USER") == "environment"


def test_a_quoted_or_padded_value_in_the_env_file_still_reads(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    dash.ENV_FILE.write_text('  DASHBOARD_USER = "quoted"  \n', encoding="utf-8")
    assert dash.login_setting("DASHBOARD_USER") == "quoted"


def test_a_missing_env_file_is_not_an_error(client, env, monkeypatch):
    monkeypatch.delenv("DASHBOARD_USER", raising=False)
    dash.ENV_FILE.unlink(missing_ok=True)
    assert dash.login_setting("DASHBOARD_USER") == ""


def test_only_the_sign_in_keys_can_be_read_this_way():
    """It is not a general back door into the environment."""
    with pytest.raises(ValueError):
        dash.login_setting("APOLLO_HARVEY_KEY")


# ── A redeploy has to land somewhere usable ──


def test_a_hash_that_lost_a_piece_falls_back_to_the_password(client, monkeypatch):
    """An unreadable hash carries no intent: it is a value that lost a piece
    between the host's settings and this process. Treating it as the final
    word locks the operator out of their own dashboard with nothing to go on,
    so the password they also set is used, and the reason is logged."""
    monkeypatch.setenv("DASHBOARD_PASS_HASH", "scrypt1")
    monkeypatch.setenv("DASHBOARD_PASS", PASSWORD)
    assert sign_in(client).status_code == 200
    assert dash.hash_is_usable() is False
    assert "field(s) instead of 6" in dash.secret_problem()


def test_a_readable_hash_is_still_the_only_thing_consulted(client, monkeypatch):
    """Otherwise moving to a hash and forgetting to clear the plain value
    would leave the weaker of the two working."""
    monkeypatch.setenv("DASHBOARD_PASS_HASH", sessions.hash_password("the-real-one"))
    monkeypatch.setenv("DASHBOARD_PASS", PASSWORD)
    assert sign_in(client, password=PASSWORD).status_code == 401
    assert sign_in(client, password="the-real-one").status_code == 200


def test_an_unusable_hash_alone_is_not_called_configured(client, monkeypatch):
    """The page asks this to decide whether to offer a sign-in. Offering one
    that cannot possibly succeed, and hiding the token box behind it, is how
    someone ends up with no way in at all."""
    monkeypatch.setenv("DASHBOARD_PASS_HASH", "scrypt1")
    monkeypatch.delenv("DASHBOARD_PASS", raising=False)
    assert dash.login_configured() is False
    assert client.get("/api/instance").json()["login_enabled"] is False
    assert sign_in(client).status_code == 503


@pytest.mark.parametrize("value, usable", [
    ("", True),                                     # nothing set is not a fault
    ("scrypt1", False),
    ("scrypt.16384.8.1.zz.zz", False),
    ("my-actual-password", False),
])
def test_whether_a_stored_hash_can_be_used(monkeypatch, value, usable):
    monkeypatch.setenv("DASHBOARD_PASS_HASH", value)
    assert dash.hash_is_usable() is usable


def test_the_old_dollar_hash_counts_as_usable(monkeypatch):
    """It works; it is only the newer form that avoids the '$' in the first
    place. An install carrying one must not be told it is broken."""
    good = sessions.hash_password(PASSWORD)
    fields = good.split(".")
    monkeypatch.setenv("DASHBOARD_PASS_HASH", "$".join(fields))
    assert dash.hash_is_usable() is True
    assert dash.login_configured() is True

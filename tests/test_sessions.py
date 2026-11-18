"""Signed-in sessions (sellbird/sessions.py).

A password sign-in used to hand back the dashboard's own token: one
credential, no expiry, no sign-out. These cover what replaced it — a session
with two clocks, a sign-out that ends only itself, a throttle that makes
guessing a password pointless, and a password hash so the plain value never
has to sit in a deployment's environment.
"""

import json
import os
import stat
import time

import pytest

from sellbird import sessions
from sellbird.sessions import LoginThrottle, SessionStore, hash_password, verify_password

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes (Windows uses ACLs)")


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions.json")


# ── A session is its own credential ──


def test_a_sign_in_gets_its_own_token_and_is_accepted(store):
    token = store.create("a browser")
    assert len(token) >= 43
    session = store.check(token)
    assert session and session["label"] == "a browser"


def test_two_sign_ins_get_different_tokens(store):
    assert store.create() != store.create()
    assert len(store.sessions()) == 2


def test_the_token_itself_is_never_written_down(store, tmp_path):
    token = store.create()
    body = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert token not in body
    # Only its hash, as a key.
    keys = list(json.loads(body)["sessions"])
    assert len(keys) == 1 and len(keys[0]) == 64


def test_an_unknown_token_is_refused(store):
    store.create()
    assert store.check("not-a-real-token") is None
    assert store.check("") is None


# ── Two clocks ──


def test_a_session_ends_when_it_is_too_old(store, monkeypatch):
    token = store.create()
    later = time.time() + sessions.MAX_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert store.check(token) is None


def test_a_session_ends_after_long_enough_unused(store, monkeypatch):
    token = store.create()
    later = time.time() + sessions.IDLE_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert store.check(token) is None


def test_using_a_session_pushes_its_idle_clock_back(store, monkeypatch):
    token = store.create()
    start = time.time()
    # Used again inside the idle window...
    monkeypatch.setattr(time, "time", lambda: start + sessions.IDLE_SECONDS - 10)
    assert store.check(token) is not None
    # ...so it is still alive a whole window after that use.
    monkeypatch.setattr(time, "time",
                        lambda: start + 2 * sessions.IDLE_SECONDS - 20)
    assert store.check(token) is not None


def test_a_used_session_still_cannot_outlive_its_end_date(store, monkeypatch):
    token = store.create()
    start = time.time()
    end = start + sessions.MAX_SECONDS + 1
    # Kept warm right up to the end date, never idle long enough to lapse...
    step = sessions.IDLE_SECONDS // 2
    for n in range(1, sessions.MAX_SECONDS // step):
        moment = start + n * step
        monkeypatch.setattr(time, "time", lambda m=moment: m)
        assert store.check(token) is not None, f"lapsed after {moment - start}s"
    # ...and it still ends there.
    monkeypatch.setattr(time, "time", lambda: end)
    assert store.check(token) is None


def test_an_expired_session_is_dropped_from_the_file(store, monkeypatch, tmp_path):
    token = store.create()
    later = time.time() + sessions.MAX_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert store.check(token) is None
    assert json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))["sessions"] == {}


# ── Signing out ──


def test_signing_out_ends_that_session_and_no_other(store):
    mine, theirs = store.create("mine"), store.create("theirs")
    assert store.revoke(mine) is True
    assert store.check(mine) is None
    assert store.check(theirs) is not None


def test_signing_out_twice_is_not_an_error(store):
    token = store.create()
    assert store.revoke(token) is True
    assert store.revoke(token) is False


def test_everyone_can_be_signed_out_at_once(store):
    tokens = [store.create() for _ in range(3)]
    assert store.revoke_all() == 3
    assert all(store.check(t) is None for t in tokens)
    assert store.sessions() == []


# ── On disk ──


def test_sessions_survive_a_restart(tmp_path):
    token = SessionStore(tmp_path / "sessions.json").create()
    assert SessionStore(tmp_path / "sessions.json").check(token) is not None


def test_a_corrupt_file_signs_everyone_out_rather_than_crashing(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")
    store = SessionStore(path)
    assert store.sessions() == []
    # And it recovers: a new sign-in works.
    assert store.check(store.create()) is not None


def test_a_missing_file_is_simply_nobody_signed_in(tmp_path):
    store = SessionStore(tmp_path / "nothing-here.json")
    assert store.sessions() == []
    assert store.check("x" * 43) is None


@POSIX
def test_the_file_is_not_readable_by_other_users(tmp_path):
    path = tmp_path / "sessions.json"
    SessionStore(path).create()
    assert not stat.S_IMODE(path.stat().st_mode) & 0o077


@POSIX
def test_a_readable_file_is_reported(tmp_path):
    path = tmp_path / "sessions.json"
    store = SessionStore(path)
    store.create()
    assert store.owner_warning() == ""
    path.chmod(0o644)
    assert "readable by other users" in store.owner_warning()


def test_sessions_never_pile_up_without_limit(store):
    tokens = [store.create() for _ in range(sessions.MAX_SESSIONS + 5)]
    assert len(store.sessions()) <= sessions.MAX_SESSIONS
    # The newest are the ones kept.
    assert store.check(tokens[-1]) is not None


def test_what_is_signed_in_carries_no_tokens(store):
    token = store.create("a browser")
    rows = store.sessions()
    assert rows and token not in json.dumps(rows)
    assert rows[0]["label"] == "a browser"


# ── Guessing a password ──


def test_a_few_wrong_tries_are_allowed_then_refused():
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.PER_SOURCE):
        assert throttle.retry_after("10.0.0.1") == 0
        throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") > 0


def test_one_blocked_source_does_not_block_everybody():
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.PER_SOURCE + 1):
        throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") > 0
    assert throttle.retry_after("10.0.0.2") == 0


def test_rotating_source_addresses_still_hits_a_ceiling():
    throttle = LoginThrottle()
    for i in range(LoginThrottle.TOTAL):
        throttle.record_failure(f"10.0.0.{i}")
    assert throttle.retry_after("10.9.9.9") > 0


def test_signing_in_clears_that_source():
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.PER_SOURCE):
        throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") > 0
    throttle.record_success("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") == 0


def test_the_count_only_covers_a_window(monkeypatch):
    throttle = LoginThrottle()
    for _ in range(LoginThrottle.PER_SOURCE):
        throttle.record_failure("10.0.0.1")
    assert throttle.retry_after("10.0.0.1") > 0
    later = time.time() + LoginThrottle.WINDOW_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert throttle.retry_after("10.0.0.1") == 0


# ── The password hash ──


def test_a_hashed_password_verifies_and_nothing_else_does():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("correct horse battery stapl", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_every_time():
    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize("stored", [
    "", "not-a-hash", "scrypt$1", "scrypt$0$8$1$00$00",
    "bcrypt$16384$8$1$00$00", "scrypt$16384$8$1$zz$00",
    "scrypt$99999999$8$1$00$00",
])
def test_a_hash_that_cannot_be_read_lets_nothing_through(stored):
    assert not verify_password("anything", stored)
    assert not verify_password("", stored)


# ── A session is tied to the password that made it ──


def test_a_session_made_under_another_password_is_refused(store):
    token = store.create("browser", credential="old-mark")
    assert store.check(token, "old-mark") is not None
    assert store.check(token, "new-mark") is None


def test_a_refused_session_is_dropped_not_just_hidden(store):
    token = store.create("browser", credential="old-mark")
    assert store.check(token, "new-mark") is None
    assert store.check(token, "old-mark") is None      # gone, not merely filtered


def test_no_credential_to_check_against_leaves_a_session_alone(store):
    """Token-only installs have no password, so there is no mark to match
    and a session must not be refused for lacking one."""
    token = store.create("browser", credential="a-mark")
    assert store.check(token, "") is not None


def test_the_credential_mark_is_not_the_credential():
    from sellbird.sessions import credential_fingerprint
    mark = credential_fingerprint("operator", "scrypt$16384$8$1$aa$bb")
    assert "operator" not in mark and "scrypt" not in mark
    assert len(mark) == 32
    assert credential_fingerprint("", "") == ""
    # The same inputs give the same mark, or a redeploy would sign everyone out.
    assert mark == credential_fingerprint("operator", "scrypt$16384$8$1$aa$bb")


def test_the_mark_never_comes_back_out_of_the_store(store):
    """With a plain password the mark is a hash of it, so handing it to a
    page would make it guessable offline."""
    token = store.create("browser", credential="secret-mark")
    assert "secret-mark" not in json.dumps(store.sessions())
    assert "secret-mark" not in json.dumps(store.check(token, "secret-mark"))


# ── The hash has to survive being pasted into a deployment ──


def test_a_new_hash_has_no_dollar_signs_in_it():
    """A "$" is the one character the layer this value travels through treats
    as special: docker compose, .env files, shells and most hosting UIs read
    "$16384" as a variable reference. A mangled value fails to verify and the
    only symptom is a password that "doesn't work", with nothing in any log."""
    value = hash_password("a-long-enough-password")
    assert "$" not in value
    # Hex and dots, so nothing along the way has an opinion about it.
    assert set(value) <= set("0123456789abcdef.scrypt")


def test_the_old_dollar_form_still_verifies():
    """Installs already carry one; changing the format must not lock them out."""
    salt, digest = "aa" * 16, "bb" * 32
    stored = f"scrypt$16384$8$1${salt}${digest}"
    # It parses (the digest won't match, but it is read, not rejected).
    assert verify_password("x", stored) is False
    assert "older '$' form" in sessions.describe_secret(stored)


def test_a_hash_doubled_by_compose_escaping_still_works():
    """One round of compose escaping turns every "$" into "$$". That is a
    value we can read correctly, so refusing it would lock someone out of
    their own dashboard for a reason they cannot see."""
    value = "scrypt$16384$8$1$" + "aa" * 16 + "$" + "bb" * 32
    import hashlib
    digest = hashlib.scrypt(b"right", salt=bytes.fromhex("aa" * 16),
                            n=16384, r=8, p=1, dklen=32).hex()
    value = f"scrypt$16384$8$1${'aa' * 16}${digest}"
    assert verify_password("right", value)
    assert verify_password("right", value.replace("$", "$$"))
    assert not verify_password("wrong", value.replace("$", "$$"))


def test_a_mangled_hash_still_lets_nothing_through():
    """Tolerating the doubled form must not mean tolerating a broken one."""
    for broken in ("scrypt1", "scrypt", "scrypt.16384.8", "", "   ",
                   "scrypt.16384.8.1.nothex.nothex"):
        assert not verify_password("right", broken)


@pytest.mark.parametrize("stored, says", [
    ("", "empty"),
    ("   ", "empty"),
    ("scrypt1", "field"),
    ("my-actual-password", "doesn't look like a hash"),
    ("scrypt.16384.8.1.zz.zz", "isn't hex"),
])
def test_a_bad_hash_is_explained_in_a_sentence(stored, says):
    """Written for someone locked out of their own dashboard, so it says what
    to do rather than what the parser thought."""
    note = sessions.describe_secret(stored)
    assert says in note
    assert stored.strip() not in note or not stored.strip()   # never echoes it


def test_a_good_hash_has_nothing_to_say():
    assert sessions.describe_secret(hash_password("a-long-enough-password")) == ""


def test_a_hash_with_whitespace_around_it_is_called_out():
    note = sessions.describe_secret(" " + hash_password("a-long-enough-password") + "\n")
    assert "space or newline" in note

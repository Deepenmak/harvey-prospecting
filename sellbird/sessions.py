"""Signed-in sessions, so a password is not an alias for the master token.

The dashboard's own token (``data/dashboard-token``) is a machine credential:
printed in a terminal, pasted into a tab, used by the CLI. It never expires
and it is the same for everybody. Handing it out to whoever typed the password
made the password an alias for it — no expiry, no sign-out, and no way to end
one person's access without changing everyone's.

A sign-in now mints its own token. Only its SHA-256 is kept, beside when it
was made, when it was last used, and when it must end, in
``data/sessions.json`` (owner-only, replaced atomically). The guard accepts a
session token exactly where it accepts the user's token, so nothing
downstream has to know which one arrived.

Two clocks end a session: ``IDLE_SECONDS`` since it was last used, and
``MAX_SECONDS`` since it was made. Neither can be extended past the other.

This does not protect against code already running as the same OS user: it
can read ``data/sessions.json`` as easily as the token file. It protects
against a leaked password staying useful forever, against a browser left
signed in on a machine nobody owns any more, and against one credential
shared by everyone who ever logged in.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import time
from pathlib import Path

# A session lasts this long unused, and this long in all.
IDLE_SECONDS = 12 * 60 * 60
MAX_SECONDS = 7 * 24 * 60 * 60
# More than this many live sessions and the least recently used go. A person
# signing in from a few browsers is normal; a hundred is not.
MAX_SESSIONS = 24
# How stale ``last_seen_at`` may get before a check writes the file. Without
# this every request would be a disk write.
TOUCH_SECONDS = 60

_IS_WINDOWS = os.name == "nt"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def credential_fingerprint(*parts: str) -> str:
    """A short, one-way mark of the credential a session was made with.

    Changing the password is how an organisation ends someone's access, and
    it has to actually end it: a session minted under the old password must
    stop working the moment the new one is set, without anyone remembering to
    revoke anything. Sessions carry this mark and are dropped when it stops
    matching.

    It is a hash of a hash (or of the plain password, when that is all there
    is), truncated, and it never leaves the server -- it identifies the
    credential, it does not reveal it.
    """
    joined = "\x00".join(str(p or "") for p in parts)
    if not joined.strip("\x00"):
        return ""
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()[:32]


class SessionStore:
    """Every live sign-in, on disk so a redeploy doesn't sign everyone out."""

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self.configure(path)

    def configure(self, path: Path):
        """Point at another file, forgetting what was cached. The dashboard's
        tests and any workspace switch need this; nothing else should."""
        with self._lock:
            self.path = Path(path)
            self._rows: dict[str, dict] | None = None

    # ── disk ──

    def _load(self) -> dict[str, dict]:
        if self._rows is not None:
            return self._rows
        rows: dict[str, dict] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for key, row in (raw.get("sessions") or {}).items():
                if isinstance(key, str) and len(key) == 64 and isinstance(row, dict):
                    rows[key] = {
                        "created_at": float(row.get("created_at") or 0),
                        "last_seen_at": float(row.get("last_seen_at") or 0),
                        "expires_at": float(row.get("expires_at") or 0),
                        "label": str(row.get("label") or "")[:120],
                        "credential": str(row.get("credential") or "")[:64],
                    }
        except (OSError, ValueError, TypeError, AttributeError):
            rows = {}   # unreadable or corrupt: nobody is signed in, which is safe
        self._rows = rows
        return rows

    def _save(self, rows: dict[str, dict]):
        body = json.dumps({"version": 1, "sessions": rows}, indent=0).encode("utf-8")
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
            fd = os.open(tmp, flags, 0o600)
            try:
                if not _IS_WINDOWS:
                    os.fchmod(fd, 0o600)
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except OSError:
            # A session that can't be written is still usable in this process.
            # Failing the sign-in over it would be worse than losing it on a
            # restart, and the next write may well succeed.
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _prune(self, rows: dict[str, dict], now: float) -> dict[str, dict]:
        live = {k: v for k, v in rows.items()
                if v["expires_at"] > now and now - v["last_seen_at"] < IDLE_SECONDS}
        if len(live) > MAX_SESSIONS:
            oldest = sorted(live.items(), key=lambda kv: kv[1]["last_seen_at"])
            for key, _ in oldest[:len(live) - MAX_SESSIONS]:
                live.pop(key, None)
        return live

    # ── use ──

    def create(self, label: str = "", credential: str = "") -> str:
        """Mint a session and return its token. The token is never stored.

        ``credential`` is the fingerprint of the password it was made with
        (see ``credential_fingerprint``), so changing the password ends it.
        """
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            rows = self._prune(dict(self._load()), now)
            rows[_hash(token)] = {
                "created_at": now,
                "last_seen_at": now,
                "expires_at": now + MAX_SECONDS,
                "label": str(label or "")[:120],
                "credential": str(credential or "")[:64],
            }
            self._rows = rows
            self._save(rows)
        return token

    def check(self, token: str, credential: str = "") -> dict | None:
        """The session this token names, sliding its idle clock.

        None if it is unknown, idle too long, past its end, or made with a
        password that is no longer the password.
        """
        if not token:
            return None
        key = _hash(token)
        now = time.time()
        with self._lock:
            rows = self._load()
            row = rows.get(key)
            if row is None:
                return None
            stale = bool(credential) and row.get("credential", "") != credential
            if stale or row["expires_at"] <= now \
                    or now - row["last_seen_at"] >= IDLE_SECONDS:
                rows.pop(key, None)
                self._rows = rows
                self._save(rows)
                return None
            if now - row["last_seen_at"] >= TOUCH_SECONDS:
                row["last_seen_at"] = now
                self._rows = rows
                self._save(rows)
            return {"id": key[:12], "created_at": row["created_at"],
                    "last_seen_at": row["last_seen_at"],
                    "expires_at": row["expires_at"], "label": row["label"]}

    def revoke(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            rows = dict(self._load())
            if rows.pop(_hash(token), None) is None:
                return False
            self._rows = rows
            self._save(rows)
            return True

    def revoke_all(self) -> int:
        with self._lock:
            rows = self._load()
            count = len(rows)
            self._rows = {}
            self._save({})
            return count

    def sessions(self) -> list[dict]:
        """What is signed in, newest first. Carries no tokens."""
        now = time.time()
        with self._lock:
            before = self._load()
            rows = self._prune(dict(before), now)
            if len(rows) != len(before):
                self._rows = rows
                self._save(rows)
            # Never the credential mark: with a plain password it is a hash
            # of the password, and handing it out would make it guessable
            # offline by anyone who can read one page of the dashboard.
            out = [{"id": k[:12], "created_at": v["created_at"],
                    "last_seen_at": v["last_seen_at"],
                    "expires_at": v["expires_at"], "label": v["label"]}
                   for k, v in rows.items()]
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return out

    def owner_warning(self) -> str:
        """Why the file on disk can't be trusted, or ''."""
        try:
            st = self.path.stat()
        except OSError:
            return ""
        if _IS_WINDOWS:
            return ""
        if not stat.S_ISREG(st.st_mode):
            return f"{self.path} is not a regular file."
        if st.st_mode & 0o077:
            return (f"{self.path} is readable by other users "
                    f"(mode {stat.S_IMODE(st.st_mode):o}); signed-in sessions may be known.")
        return ""


class LoginThrottle:
    """Failed sign-ins get slower, then stop.

    Counted per source address *and* in total, so rotating addresses buys no
    faster guessing, and one noisy address can't lock everybody else out for
    longer than the window. Held in memory only: losing the count on a
    restart is fine, because restarting needs access to the box already.
    """

    WINDOW_SECONDS = 15 * 60
    PER_SOURCE = 5
    TOTAL = 25

    def __init__(self):
        self._lock = threading.Lock()
        self._by_source: dict[str, list[float]] = {}
        self._all: list[float] = []

    @staticmethod
    def _recent(stamps: list[float], now: float) -> list[float]:
        cutoff = now - LoginThrottle.WINDOW_SECONDS
        return [t for t in stamps if t > cutoff]

    def retry_after(self, source: str) -> int:
        """Seconds to wait before another attempt is allowed; 0 if it is."""
        now = time.time()
        with self._lock:
            mine = self._recent(self._by_source.get(source, []), now)
            self._by_source[source] = mine
            everyone = self._recent(self._all, now)
            self._all = everyone
            waits = []
            if len(mine) >= self.PER_SOURCE:
                waits.append(mine[-self.PER_SOURCE] + self.WINDOW_SECONDS - now)
            if len(everyone) >= self.TOTAL:
                waits.append(everyone[-self.TOTAL] + self.WINDOW_SECONDS - now)
            return max(1, int(max(waits))) if waits else 0

    def record_failure(self, source: str):
        now = time.time()
        with self._lock:
            mine = self._recent(self._by_source.get(source, []), now)
            mine.append(now)
            self._by_source[source] = mine
            self._all = self._recent(self._all, now)
            self._all.append(now)

    def record_success(self, source: str):
        with self._lock:
            self._by_source.pop(source, None)


# ── The password itself ──
#
# DASHBOARD_PASS is the plain password, which means it sits in the host's
# environment where anyone who can read the deployment's configuration can
# read it. DASHBOARD_PASS_HASH is preferred: scrypt, with the parameters and
# salt in the string, so the stored value is useless to whoever reads it.

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

# Fields are separated by a dot, not the "$" that password hashes traditionally
# use. This value's whole job is to be pasted into a deployment's environment,
# and "$" is the one character that layer treats as special: docker compose,
# .env files, shells and most hosting UIs all read "$16384" as a variable
# reference. A value that arrives mangled fails to verify, and the only symptom
# is a password that "doesn't work" with nothing in any log to explain it. Dots
# and hex travel through all of it untouched.
#
# The "$" form is still accepted, because installs already carry one, and so is
# a "$$" doubled form, which is what one round of compose escaping leaves
# behind -- that is a value we can read correctly, so refusing it would only
# lock someone out of their own dashboard for a reason they cannot see.


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return ".".join(["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
                     salt.hex(), digest.hex()])


def _fields(stored: str) -> list[str] | None:
    """The six fields of a stored hash, however it was written, or None."""
    text = str(stored or "").strip()
    if not text:
        return None
    if "." in text and "$" not in text:
        parts = text.split(".")
    else:
        # "$$" is one round of compose escaping over the old "$" form.
        parts = text.replace("$$", "$").split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    return parts


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a hash_password() string. False on anything
    malformed: an unreadable hash must never let a password through."""
    parts = _fields(stored)
    if parts is None:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return False
    if not (0 < n <= 2 ** 20 and 0 < r <= 64 and 0 < p <= 16) or not salt or not expected:
        return False
    try:
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=n, r=r, p=p, dklen=len(expected),
                                maxmem=132 * 1024 * 1024)
    except ValueError:
        return False
    return secrets.compare_digest(digest, expected)


def describe_secret(stored: str) -> str:
    """What is wrong with a stored hash, in a sentence, or ''.

    Written for someone locked out of their own dashboard, so it says what to
    do rather than what the parser thought. It never echoes the value.
    """
    text = str(stored or "")
    if not text.strip():
        return "It is empty. Nothing was passed through to the container."
    if text != text.strip():
        return ("It has a space or newline around it. Paste it again with no "
                "trailing whitespace.")
    parts = _fields(text)
    if parts is None:
        if "scrypt" not in text:
            return ("It doesn't look like a hash at all. This variable takes the "
                    "output of 'sellbird password-hash', not the password.")
        pieces = len(text.replace("$$", "$").replace(".", "$").split("$"))
        if pieces < 6:
            return (f"It has {pieces} field(s) instead of 6, so something along the "
                    "way ate part of it -- usually a '$' being read as a variable. "
                    "Run 'sellbird password-hash' again for a '$'-free value.")
        return "Its fields can't be read. Run 'sellbird password-hash' again."
    try:
        bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
        int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return ("Its salt or digest isn't hex, so the value was altered in transit. "
                "Run 'sellbird password-hash' again for a '$'-free value.")
    if "$" in text:
        return ("It works, but it is the older '$' form. A '$' can be read as a "
                "variable by the host's configuration; 'sellbird password-hash' "
                "now prints a form without one.")
    return ""

"""Signing in to Claude has to survive a redeploy.

The container keeps everything that matters under SELLBIRD_HOME (/config),
which is a mounted volume. Claude's login was the exception: with no
CLAUDE_CONFIG_DIR it landed in $HOME, which on this image is /root -- part
of the image layer, thrown away with the container. So every redeploy signed
Sellbird out of Claude.

It failed quietly, which is what made it expensive: the heartbeat kept
running, websites kept being read (no model call), and only the writing
stopped. The dashboard showed drafts stuck at zero with nothing saying why.

These tests pin the fix: the login directory is on the volume, and nothing
in Sellbird's own isolation strips the variable that puts it there.
"""

import os
from pathlib import Path

from sellbird import claude_runtime
from sellbird.usage import claude_config_dir

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
START_SH = (ROOT / "start.sh").read_text(encoding="utf-8")


def test_the_image_puts_claudes_login_on_the_volume():
    assert "ENV CLAUDE_CONFIG_DIR=/config/.claude" in DOCKERFILE
    # ...and the volume it names is the one that is actually mounted.
    assert 'VOLUME ["/config"]' in DOCKERFILE
    assert "ENV SELLBIRD_HOME=/config" in DOCKERFILE


def test_the_login_directory_exists_before_anyone_signs_in():
    """`claude login` into a directory that isn't there fails on some images,
    and a world-readable one would leak credentials to any other process."""
    assert "/config/.claude" in DOCKERFILE
    assert 'mkdir -p "$CLAUDE_DIR"' in START_SH
    assert 'chmod 700 "$CLAUDE_DIR"' in START_SH


def test_boot_says_when_claude_is_not_signed_in():
    """The failure used to be silent. Now the first lines of the log say it."""
    assert "claude login" in START_SH
    assert "Claude is not signed in yet" in START_SH


def test_start_falls_back_to_the_home_volume_if_the_variable_is_unset():
    """An older container, or someone running start.sh by hand, still puts
    the login somewhere that survives rather than in /root."""
    assert 'CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME_DIR/.claude}"' in START_SH


def test_sellbird_reads_the_login_from_where_it_put_it(monkeypatch, tmp_path):
    """The quota gauge reads the credentials file. If it looked in $HOME
    while the CLI wrote to the volume, the gauge would go blank."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude_config_dir() == tmp_path / "cfg"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claude_config_dir() == Path.home() / ".claude"


def test_the_isolation_pass_does_not_strip_the_login_location(monkeypatch):
    """Sellbird strips instruction-carrying variables out of the CLI's
    environment. Stripping this one would send every call to a Claude with
    no login, which is the bug again by another route.
    """
    assert "CLAUDE_CONFIG_DIR" not in claude_runtime.STRIPPED_ENV
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/config/.claude")
    env = claude_runtime.runtime_env(os.getcwd())
    assert env["CLAUDE_CONFIG_DIR"] == "/config/.claude"

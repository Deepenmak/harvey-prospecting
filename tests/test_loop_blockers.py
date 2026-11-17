"""The dashboard has to say why the loop isn't working.

It used to say "Sellbird is stopped" and nothing else, while Today's
pipeline card cheerfully said "Next: write first emails for 30 contacts" --
advice for a pipeline that could not move. Everything that stops the loop is
knowable without pressing anything: setup not finished, no Claude CLI, not
signed in, or the loop pausing itself every cycle.

That last one matters most in a container. The heartbeat restarts every 60
seconds and prints its reason into `docker logs`, where nobody looks, and
the dashboard shows a healthy-looking pipeline frozen at zero.
"""

import asyncio

import pytest

from sellbird import dashboard


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """No cached answer from another test, and a signed-in Claude by default."""
    monkeypatch.setattr(dashboard, "_blockers_cache", None)
    monkeypatch.setattr("sellbird.claude_runtime.check_cli", lambda env=None: "/usr/bin/claude")
    monkeypatch.setattr("sellbird.integrations.quota.get_oauth_token", lambda: "tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
    monkeypatch.setattr(dashboard, "_blockers_cache", None)


def workspace(tmp_path, monkeypatch, company="Acme Ltd", env=True):
    config = tmp_path / "sellbird.yaml"
    config.write_text(f"persona:\n  company: {company!r}\n", encoding="utf-8")
    if env:
        (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(dashboard, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_resolve_config_file", lambda: config)
    return config


def titles(tmp_path, monkeypatch, **kw):
    workspace(tmp_path, monkeypatch, **kw)
    return [b["title"] for b in dashboard._loop_blockers()]


# ── What stops the loop ──────────────────────────────────────────────


def test_a_ready_workspace_has_nothing_in_the_way(tmp_path, monkeypatch):
    assert titles(tmp_path, monkeypatch) == []


def test_the_template_company_is_named_as_the_reason(tmp_path, monkeypatch):
    """The exact failure on a fresh container: the image ships the template,
    sellbird.local.yaml is gitignored and never reaches it, and the
    heartbeat exits before its first cycle.
    """
    found = titles(tmp_path, monkeypatch, company="Your Company")
    assert found == ["Your company isn't filled in"]


def test_an_empty_company_counts_too(tmp_path, monkeypatch):
    assert titles(tmp_path, monkeypatch, company="") == ["Your company isn't filled in"]


def test_a_missing_env_file_is_named(tmp_path, monkeypatch):
    found = titles(tmp_path, monkeypatch, env=False)
    assert found == ["No credentials file yet"]


def test_a_broken_claude_cli_says_what_it_said(tmp_path, monkeypatch):
    from sellbird.claude_runtime import ClaudeRuntimeError

    def boom(env=None):
        raise ClaudeRuntimeError("Claude CLI not found on PATH.")
    monkeypatch.setattr("sellbird.claude_runtime.check_cli", boom)
    workspace(tmp_path, monkeypatch)
    found = dashboard._loop_blockers()
    assert found[0]["title"] == "Claude Code can't be used here"
    assert "not found on PATH" in found[0]["detail"]


def test_being_signed_out_is_named_with_the_command_to_fix_it(tmp_path, monkeypatch):
    """The redeploy case: the CLI is fine, the login is gone. Websites keep
    being read, only the writing stops, and nothing said why."""
    monkeypatch.setattr("sellbird.integrations.quota.get_oauth_token", lambda: None)
    workspace(tmp_path, monkeypatch)
    found = dashboard._loop_blockers()
    assert found[0]["title"] == "Claude isn't signed in"
    assert "claude login" in found[0]["detail"]


def test_an_api_key_counts_as_signed_in(tmp_path, monkeypatch):
    monkeypatch.setattr("sellbird.integrations.quota.get_oauth_token", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert titles(tmp_path, monkeypatch) == []


def test_a_login_check_that_cannot_answer_does_not_cry_wolf(tmp_path, monkeypatch):
    """A keychain that won't answer is not proof of being signed out, and a
    false "sign in again" would send someone chasing a problem they
    don't have."""
    def boom():
        raise OSError("keychain locked")
    monkeypatch.setattr("sellbird.integrations.quota.get_oauth_token", boom)
    assert titles(tmp_path, monkeypatch) == []


def test_every_blocker_says_what_to_do(tmp_path, monkeypatch):
    workspace(tmp_path, monkeypatch, company="Your Company", env=False)
    for blocker in dashboard._loop_blockers():
        assert blocker["detail"].strip()
        assert set(blocker) == {"title", "detail", "tab"}


# ── Not in the way of the page ───────────────────────────────────────


def test_the_answer_is_cached_so_polling_cannot_stall_the_page(tmp_path, monkeypatch):
    """The status endpoint polls every few seconds and this runs
    `claude --help`. Once every 30 seconds is enough."""
    workspace(tmp_path, monkeypatch, company="Your Company")
    calls = []
    real = dashboard._loop_blockers
    monkeypatch.setattr(dashboard, "_loop_blockers",
                        lambda: (calls.append(1), real())[1])
    first = run(dashboard.loop_blockers())
    second = run(dashboard.loop_blockers())
    assert first == second
    assert len(calls) == 1


def test_a_hosted_workspace_is_not_judged_by_this_machines_files(tmp_path, monkeypatch):
    """Hosted keeps its config and its brain elsewhere. Reading this
    machine's files would report problems it does not have."""
    class Home:
        hosted = True
    monkeypatch.setattr("sellbird.workspace_home.current_home", lambda: Home())
    assert titles(tmp_path, monkeypatch, company="Your Company", env=False) == []


def test_a_check_that_throws_never_breaks_the_status_call(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(dashboard, "_loop_blockers", boom)
    monkeypatch.setattr(dashboard, "_blockers_cache", None)
    assert run(dashboard.loop_blockers()) == []

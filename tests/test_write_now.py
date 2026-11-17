"""Writing the first emails without waiting for the loop.

Today would say "Next: write first emails for 30 contacts" and the only
button on that row walked you to a Start screen. For anyone whose heartbeat
isn't up yet -- a fresh container, a laptop where nobody pressed Start --
the pipeline named a job and offered nowhere to do it, and the Campaigns
tab said "no campaigns yet" with no way to make one.

The button runs the same Writer the heartbeat runs, under the same rules:
deliverable addresses only, and every draft lands in the Outbox for
approval. Nothing is sent from here.

These tests are about the guards. The one that matters most is the loop:
two Writers over the same 'new' prospects would each make a campaign for
them, which is two first emails to one person.
"""

import asyncio

import pytest

from sellbird import dashboard
from tests.test_dashboard_signals import client as _dashboard_client  # noqa: F401


@pytest.fixture
def client(_dashboard_client, monkeypatch):  # noqa: F811
    """The shared dashboard client, with nothing in the way of writing."""
    monkeypatch.setattr(dashboard, "_writer_task", None)
    monkeypatch.setattr(dashboard, "_writer_report", None)
    monkeypatch.setattr(dashboard, "_blockers_cache", (float("inf"), []))
    monkeypatch.setattr(dashboard, "_check_sellbird_pid", lambda: None)
    return _dashboard_client


def test_it_refuses_while_the_loop_is_running(client, monkeypatch):
    """Both would pick the same contacts and each make a campaign for them:
    two first emails to one person."""
    monkeypatch.setattr(dashboard, "_check_sellbird_pid", lambda: 4242)
    r = client.post("/api/writer/run")
    assert r.status_code == 409
    assert "writes these by itself" in r.json()["message"]


def test_it_refuses_while_another_pass_is_going(client, monkeypatch):
    class Busy:
        def done(self):
            return False
    monkeypatch.setattr(dashboard, "_writer_task", Busy())
    assert client.post("/api/writer/run").status_code == 409


def test_it_says_what_is_in_the_way_rather_than_failing_oddly(client, monkeypatch):
    """With no Claude there is nothing to write with. Say that, not a
    stack trace five minutes later."""
    monkeypatch.setattr(dashboard, "_blockers_cache",
                        (float("inf"), [{"title": "Claude isn't signed in",
                                         "detail": "Run: claude login", "tab": ""}]))
    r = client.post("/api/writer/run")
    assert r.status_code == 400
    assert "claude login" in r.json()["message"]


def test_a_pass_reports_what_it_wrote(client, monkeypatch):
    """The report is what the button reads back: how many campaigns, and
    how many drafts are now waiting on a human."""
    ran = []

    class FakeWriter:
        def __init__(self, *a, **k):
            pass

        async def run(self):
            ran.append(True)

    monkeypatch.setattr("sellbird.agents.writer.Writer", FakeWriter)
    assert client.post("/api/writer/run").json() == {"success": True}

    for _ in range(100):
        body = client.get("/api/writer/run").json()
        if not body["running"]:
            break
        asyncio.run(asyncio.sleep(0.05))
    assert ran == [True]
    assert body["report"]["error"] == ""
    assert body["report"]["campaigns"] == 0      # the fake wrote nothing


def test_a_writer_that_throws_is_reported_not_swallowed(client, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        async def run(self):
            raise RuntimeError("the model said no")

    monkeypatch.setattr("sellbird.agents.writer.Writer", Boom)
    client.post("/api/writer/run")
    for _ in range(100):
        body = client.get("/api/writer/run").json()
        if not body["running"]:
            break
        asyncio.run(asyncio.sleep(0.05))
    assert "the model said no" in body["report"]["error"]


def test_nothing_is_running_before_anyone_presses_it(client):
    assert client.get("/api/writer/run").json() == {"running": False, "report": None}

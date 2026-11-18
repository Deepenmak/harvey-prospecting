"""What the API promises about its own failures, and about a page of rows.

Three error shapes used to come back from this API — the guard's
``{error, message}``, a route's ``{success: false, message}``, and FastAPI's
``{detail}`` — and an unhandled exception came back as plain text, which the
page could only read as "the dashboard isn't answering". A bug in one route
therefore looked exactly like a stopped server.

Also here: ``/api/prospects`` used to answer with a silent ``LIMIT 200``, so
the coding agent could reason about "every prospect" from a slice it had no
way to notice it had been given.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash
from sellbird.background import Jobs

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "sellbird.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    dash.session_store.configure(tmp_path / "data" / "sessions.json")
    return {"dir": tmp_path, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


# ── Every error looks the same ──


def test_a_refusal_carries_error_and_message(client):
    r = client.get("/api/companies/nope/profile")
    assert r.status_code in (404, 500)
    body = r.json()
    assert isinstance(body.get("error"), str) and body["error"]
    assert isinstance(body.get("message"), str) and body["message"]


def test_a_bad_request_body_carries_the_same_shape(client):
    r = client.post("/api/settings/env", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "message" in r.json()


def test_the_guards_own_refusals_carry_it_too(env):
    with TestClient(dash.app, base_url=ORIGIN) as anon:
        for r in (anon.get("/api/today"),
                  anon.get("/api/today", headers={"Host": "evil.example"})):
            body = r.json()
            assert body["error"] and body["message"]


def test_a_missing_route_is_json_not_html(client):
    r = client.get("/api/there-is-no-such-thing")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"] == "not_found"


def test_the_old_detail_key_is_still_there(client):
    """Anything written against FastAPI's default shape keeps working."""
    body = client.get("/api/there-is-no-such-thing").json()
    assert body["detail"] == body["message"]


# ── A bug is a bug, not an outage ──


def test_an_unhandled_error_is_json_with_a_reference(env, monkeypatch):
    """The page can tell "this request broke" from "nothing is answering",
    and the operator gets something to search the log for."""
    @dash.app.get("/api/test-only-explode")
    async def _explode():
        raise RuntimeError("the secret is hunter2")

    try:
        with TestClient(dash.app, base_url=ORIGIN, raise_server_exceptions=False,
                        headers={"Authorization": f"Bearer {env['token']}"}) as c:
            r = c.get("/api/test-only-explode")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "internal" and body["success"] is False
        assert len(body["reference"]) == 8
        assert body["reference"] in body["message"]
        # Never the exception's own text: it can hold a path, a query or a key.
        assert "hunter2" not in r.text
    finally:
        dash.app.router.routes[:] = [route for route in dash.app.router.routes
                                     if getattr(route, "path", "") != "/api/test-only-explode"]


def test_a_broken_request_is_logged_under_the_same_reference(env, monkeypatch, caplog):
    @dash.app.get("/api/test-only-explode-2")
    async def _explode():
        raise RuntimeError("boom")

    try:
        with caplog.at_level("ERROR"):
            with TestClient(dash.app, base_url=ORIGIN, raise_server_exceptions=False,
                            headers={"Authorization": f"Bearer {env['token']}"}) as c:
                ref = c.get("/api/test-only-explode-2").json()["reference"]
        assert any(ref in record.getMessage() for record in caplog.records)
    finally:
        dash.app.router.routes[:] = [route for route in dash.app.router.routes
                                     if getattr(route, "path", "") != "/api/test-only-explode-2"]


def test_an_error_reply_still_carries_the_security_headers(client):
    r = client.get("/api/there-is-no-such-thing")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


# ── A page of contacts says it is a page ──


async def _seed(count: int):
    from sellbird.models.prospect import Prospect
    from sellbird.state import StateManager
    state = StateManager(db_path=str(dash.DB_PATH))
    await state.init_db()
    for i in range(count):
        await state.add_prospect(Prospect(
            first_name=f"P{i}", last_name="Example", title="Head of Ops",
            company="Example Ltd", email=f"p{i}@example.com"))


def test_contacts_come_back_with_a_total_and_a_truncated_flag(client):
    asyncio.run(_seed(7))
    body = client.get("/api/prospects", params={"limit": 3}).json()
    assert body["returned"] == 3
    assert body["total"] == 7
    assert body["truncated"] is True
    assert body["limit"] == 3 and body["offset"] == 0


def test_the_last_page_is_not_marked_truncated(client):
    asyncio.run(_seed(7))
    body = client.get("/api/prospects", params={"limit": 3, "offset": 6}).json()
    assert body["returned"] == 1 and body["truncated"] is False


def test_paging_walks_every_contact_exactly_once(client):
    asyncio.run(_seed(10))
    seen, offset = [], 0
    while True:
        body = client.get("/api/prospects", params={"limit": 4, "offset": offset}).json()
        seen += [row["email"] for row in body["prospects"]]
        if not body["truncated"]:
            break
        offset += body["returned"]
    assert len(seen) == len(set(seen)) == 10


def test_a_search_narrows_the_total_too(client):
    asyncio.run(_seed(5))
    body = client.get("/api/prospects", params={"search": "p3@"}).json()
    assert body["total"] == 1 and body["returned"] == 1
    assert body["search"] == "p3@"


def test_a_search_for_nothing_is_empty_not_everything(client):
    asyncio.run(_seed(5))
    body = client.get("/api/prospects", params={"search": "nobody-by-that-name"}).json()
    assert body["prospects"] == [] and body["total"] == 0


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": -5}, {"offset": -1}, {"limit": 10 ** 9},
])
def test_paging_out_of_range_is_clamped_not_an_error(client, params):
    asyncio.run(_seed(3))
    body = client.get("/api/prospects", params=params).json()
    assert 1 <= body["limit"] <= dash.PROSPECT_PAGE_MAX
    assert body["offset"] >= 0
    assert body["total"] == 3


def test_paging_that_is_not_a_number_is_a_bad_request(client):
    """Clamping a number is a courtesy; inventing one from "abc" would be a
    guess, and a caller sending it has a bug worth hearing about."""
    r = client.get("/api/prospects", params={"limit": "abc"})
    assert r.status_code == 422
    assert r.json()["error"] == "bad_request"


def test_a_search_term_cannot_be_unbounded(client):
    asyncio.run(_seed(1))
    body = client.get("/api/prospects", params={"search": "x" * 500}).json()
    assert len(body["search"]) <= 120


# ── Background jobs are asked to stop ──


def test_a_tracked_job_is_cancelled_and_named_at_shutdown():
    async def run():
        registry = Jobs()
        started = asyncio.Event()
        finished = []

        async def slow():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished.append("unwound")   # where a job records what it did
                raise

        registry.start("apollo", slow())
        await started.wait()
        assert registry.running() == ["apollo"]
        interrupted = await registry.shutdown(grace=2)
        assert interrupted == ["apollo"]
        assert finished == ["unwound"]
        assert registry.running() == []

    asyncio.run(run())


def test_a_finished_job_is_forgotten():
    async def run():
        registry = Jobs()

        async def quick():
            return 1

        task = registry.start("writer", quick())
        await task
        await asyncio.sleep(0)
        assert registry.running() == []
        assert await registry.shutdown() == []

    asyncio.run(run())


def test_two_jobs_of_one_name_both_stay_tracked():
    """Dropping the reference to a running task is how it gets collected
    mid-flight, so the second must not evict the first."""
    async def run():
        registry = Jobs()

        async def slow():
            await asyncio.sleep(30)

        registry.start("apollo", slow())
        registry.start("apollo", slow())
        await asyncio.sleep(0)
        assert registry.running() == ["apollo", "apollo#2"]
        assert len(await registry.shutdown(grace=2)) == 2

    asyncio.run(run())


def test_a_job_that_raises_does_not_break_shutdown():
    async def run():
        registry = Jobs()

        async def boom():
            await asyncio.sleep(0)
            raise RuntimeError("no")

        registry.start("writer", boom())
        await asyncio.sleep(0.05)
        assert await registry.shutdown() == []

    asyncio.run(run())


def test_the_dashboard_app_runs_that_shutdown(env):
    """Wired, not just written: a mounted router's lifespan is skipped, which
    is exactly the mistake this guards."""
    from sellbird.background import jobs
    assert dash.app.router.lifespan_context is not None
    built = dash.build_app()
    assert built.router.lifespan_context is dash.app.router.lifespan_context
    assert jobs.running() == []

"""Scheduled Scout sweeps: finding candidates for free, staging them like
anything else on the Imports tab, and never writing a real contact or
spending an Apollo credit without a human saying so.

Off by default (sellbird.main.scout_schedule_due reads the setting), so
these tests exercise the on-state explicitly rather than assuming it.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import sellbird.main as main_mod
from sellbird import import_service
from sellbird.agents.scout import Scout
from sellbird.config import ComplianceConfig, EnvConfig
from sellbird.import_store import ImportStore

SEGMENTS = {"segments": [
    {"key": "us_insurance", "keyword_tags": ["insurance carrier"],
     "locations": ["united states"]},
    {"key": "us_fintech", "keyword_tags": ["fintech"], "locations": ["united states"]},
]}


class FakeApolloOrgs:
    """Answers org_search like Apollo, charges nothing, counts calls."""

    def __init__(self):
        self.calls = 0

    async def search_organizations(self, filters, page=1, per_page=100):
        self.calls += 1
        name = str(filters.get("q_organization_name") or "")
        return {"organizations": [{"name": name or "Example Co",
                                   "industry": "insurance", "estimated_num_employees": 300}]}


@pytest.fixture
def store(tmp_path):
    s = ImportStore(db_path=str(tmp_path / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


@pytest.fixture
def config():
    return SimpleNamespace(compliance=ComplianceConfig())


@pytest.fixture
def env():
    return EnvConfig()


SEARCH_RESULTS = [
    ("https://acmeclaims.example/about", "Acme Claims handles P&C claims"),
    ("https://widgetco.example/", "Widgets for everyone"),
    ("https://northside.example/team", "Northside Health revenue cycle"),
]


def _patched(results=SEARCH_RESULTS, info=None):
    """The three network-touching calls a sweep makes, faked."""
    info = info or {"name": "Example Co", "description": "x",
                    "website": "https://example.test"}
    return (
        patch.object(Scout, "_web_search", new=AsyncMock(return_value=results)),
        patch.object(Scout, "_scrape_company_info", new=AsyncMock(return_value=info)),
        patch("sellbird.fetch.load_segments", return_value=SEGMENTS),
    )


def sweep(scout, limit=10):
    return asyncio.run(scout.sweep_candidates(limit=limit))


def stage(store, config, env, limit=10, client=None):
    return asyncio.run(import_service.stage_from_scout(store, config, env, limit, client))


# ── sweep_candidates: finds things, writes nothing real ──


def test_a_sweep_returns_plain_candidate_dicts(store, config, env):
    scout = Scout(None, store, config, env)
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        candidates = sweep(scout)
    assert candidates
    assert all({"company", "domain", "_raw"} <= set(c) for c in candidates)
    assert {c["domain"] for c in candidates} == {
        "acmeclaims.example", "widgetco.example", "northside.example"}


def test_a_sweep_writes_no_real_company_or_prospect(store, config, env):
    scout = Scout(None, store, config, env)
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        sweep(scout)
    companies = asyncio.run(store._rows("SELECT COUNT(*) AS n FROM companies"))
    prospects = asyncio.run(store._rows("SELECT COUNT(*) AS n FROM prospects"))
    assert companies[0]["n"] == 0
    assert prospects[0]["n"] == 0


def test_a_sweep_respects_the_limit(store, config, env):
    scout = Scout(None, store, config, env)
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        candidates = sweep(scout, limit=1)
    assert len(candidates) == 1


def test_a_sweep_skips_a_company_already_on_file(store, config, env):
    """So an hourly sweep doesn't keep re-finding what it already found."""
    async def already_exists(domain):
        return domain == "acmeclaims.example"
    scout = Scout(None, store, config, env)
    p1, p2, p3 = _patched()
    with p1, p2, p3, patch.object(store, "company_exists", new=already_exists):
        candidates = sweep(scout)
    assert "acmeclaims.example" not in {c["domain"] for c in candidates}
    assert len(candidates) == 2


def test_a_sweep_skips_a_do_not_contact_domain(store, config, env):
    config = SimpleNamespace(
        compliance=ComplianceConfig(do_not_contact=["acmeclaims.example"]))
    scout = Scout(None, store, config, env)
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        candidates = sweep(scout)
    assert "acmeclaims.example" not in {c["domain"] for c in candidates}


def test_no_segments_file_means_no_candidates_not_a_crash(store, config, env):
    scout = Scout(None, store, config, env)
    with patch("sellbird.fetch.load_segments", side_effect=FileNotFoundError("no file")):
        candidates = sweep(scout)
    assert candidates == []


def test_queries_come_from_segments_yaml_not_a_blank_icp_block(store, config, env):
    """The point of the whole feature: this fork's real ICP lives in
    segments.yaml, not the icp: block Scout's run() reads for upstream
    installs, which this deployment never filled in."""
    scout = Scout(None, store, config, env)
    with patch("sellbird.fetch.load_segments", return_value=SEGMENTS):
        queries = scout._segment_search_queries()
    assert any("insurance carrier" in q for q in queries)
    assert any("fintech" in q for q in queries)


# ── stage_from_scout: the same staging discipline as a CSV or Apollo search ──


def test_staging_creates_a_batch_sourced_as_scout(store, config, env):
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        result = stage(store, config, env, client=FakeApolloOrgs())
    batch = asyncio.run(store.get_batch(result["batch_id"]))
    assert batch["source"] == "scout"
    assert batch["status"] == "review"


def test_staging_writes_no_real_contact_either(store, config, env):
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        stage(store, config, env, client=FakeApolloOrgs())
    prospects = asyncio.run(store._rows("SELECT COUNT(*) AS n FROM prospects"))
    assert prospects[0]["n"] == 0


def test_staging_with_nothing_found_refuses_cleanly(store, config, env):
    from sellbird.import_staging import StagingError
    with patch.object(Scout, "_web_search", new=AsyncMock(return_value=[])):
        with pytest.raises(StagingError):
            stage(store, config, env)


def test_the_free_lookup_runs_automatically_and_spends_nothing(store, config, env):
    """org_search is 0 credits, so it needs no approval -- the same rule
    every other free lookup in this menu follows."""
    client = FakeApolloOrgs()
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        result = stage(store, config, env, client=client)
    assert client.calls > 0
    batch = asyncio.run(store.get_batch(result["batch_id"]))
    assert batch["credits_spent"] == 0
    assert result["confirmed"] > 0


def test_a_missing_apollo_key_still_leaves_the_batch_staged(store, config, env):
    """The free confirmation pass is a bonus, not a requirement: no key
    configured must not stop candidates from landing in Imports."""
    p1, p2, p3 = _patched()
    with p1, p2, p3, patch("sellbird.integrations.apollo.apollo_key",
                          side_effect=Exception("no key")):
        result = stage(store, config, env)   # no client: tries to open a real one
    assert result["staged"] > 0
    assert result["confirmed"] == 0


def test_staged_rows_carry_company_and_domain_but_no_address(store, config, env):
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        result = stage(store, config, env, client=FakeApolloOrgs())
    rows = asyncio.run(store.get_rows(result["batch_id"]))
    assert all(r["company"] and r["domain"] for r in rows)
    assert all(not r["email"] for r in rows)   # the priced step, not this one


def test_a_staged_candidate_still_needs_approval_to_buy_an_address(store, config, env):
    """The one rule this whole feature exists to respect: finding is free
    and automatic, buying an address is not."""
    from sellbird import apollo_ops
    from sellbird.import_staging import StagingError
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        result = stage(store, config, env, client=FakeApolloOrgs())
    with pytest.raises(StagingError, match="approve a credit cap"):
        asyncio.run(import_service.enrich(
            store, result["batch_id"], "people_match_name_domain", max_credits=0))


def test_promoting_a_scout_batch_uses_the_same_importer_as_everything_else(store, config, env):
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        result = stage(store, config, env, client=FakeApolloOrgs())
    rows = asyncio.run(store.get_rows(result["batch_id"]))
    # Give one row an email the way a real enrichment would, so it has
    # something to promote (promote() needs ROW_FIELDS-complete rows).
    asyncio.run(store.update_row(rows[0]["id"], email=f"a@{rows[0]['domain']}",
                                  email_status="guess", status="enriched"))
    report = asyncio.run(import_service.promote(store, result["batch_id"]))
    assert report["promoted"] >= 1
    batch = asyncio.run(store.get_batch(result["batch_id"]))
    assert batch["status"] in ("imported", "review")  # partial promotions stay "review"


# ── The schedule: off by default, hourly once on, free either way ──


def test_the_schedule_is_off_until_a_setting_turns_it_on(store):
    assert asyncio.run(main_mod.scout_schedule_due(store)) is False


def test_turning_it_on_with_no_prior_run_is_due_immediately(store):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is True


def test_a_recent_run_is_not_due_again(store):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    from datetime import datetime, timezone
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY,
                                  datetime.now(timezone.utc).isoformat()))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is False


def test_a_run_from_three_hours_ago_is_due_on_an_hourly_schedule(store):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_HOURS_KEY, "1"))
    from datetime import datetime, timedelta, timezone
    three_hours_ago = datetime.now(timezone.utc) - timedelta(hours=3)
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY,
                                  three_hours_ago.isoformat()))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is True


def test_a_longer_cadence_is_honoured(store):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_HOURS_KEY, "6"))
    from datetime import datetime, timedelta, timezone
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY,
                                  two_hours_ago.isoformat()))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is False


def test_a_corrupt_timestamp_is_treated_as_due_rather_than_crashing(store):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY, "not a date"))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is True


def test_running_the_sweep_records_that_it_ran_even_when_nothing_was_found(store, config, env):
    """So a quiet segment doesn't make every future cycle think a sweep is
    still due and retry every 15 minutes forever."""
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    with patch.object(Scout, "_web_search", new=AsyncMock(return_value=[])):
        asyncio.run(main_mod.run_scout_sweep(store, config, env))
    assert asyncio.run(main_mod.scout_schedule_due(store)) is False
    last_run = asyncio.run(store.get_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY, ""))
    assert last_run


def test_running_the_sweep_uses_the_configured_per_run_count(store, config, env):
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_PER_RUN_KEY, "2"))
    p1, p2, p3 = _patched()
    captured = {}
    real_sweep = Scout.sweep_candidates

    async def spying_sweep(self, limit=10):
        captured["limit"] = limit
        return await real_sweep(self, limit=limit)

    with p1, p2, p3, patch.object(Scout, "sweep_candidates", new=spying_sweep):
        asyncio.run(main_mod.run_scout_sweep(store, config, env))
    assert captured["limit"] == 2


def test_a_heartbeat_failure_in_the_sweep_never_stops_the_real_loop(store, config, env):
    """The sweep sits beside everything the heartbeat actually depends on
    (sending, writing, replying); a bug in it must degrade to a log line,
    never an exception that kills the process."""
    asyncio.run(store.set_setting(main_mod.SCOUT_SCHEDULE_ENABLED_KEY, "true"))
    with patch.object(Scout, "sweep_candidates", side_effect=RuntimeError("boom")):
        # run_scout_sweep itself may raise; the heartbeat's own try/except
        # around the call (sellbird/main.py) is what the comment documents.
        with pytest.raises(RuntimeError):
            asyncio.run(main_mod.run_scout_sweep(store, config, env))
    # Either way, the "last ran" marker is set so it doesn't hot-loop retrying.
    last_run = asyncio.run(store.get_setting(main_mod.SCOUT_SCHEDULE_LAST_RUN_KEY, ""))
    assert last_run




# ── The dashboard API Settings uses ──

import tempfile
from pathlib import Path as _Path

from fastapi.testclient import TestClient

import sellbird.dashboard as dash


@pytest.fixture
def api_client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = _Path(tmp) / "sellbird.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        dash.token_store.configure(_Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            yield c


def test_the_schedule_api_defaults_to_off(api_client):
    body = api_client.get("/api/scout/schedule").json()
    assert body["enabled"] is False
    assert body["per_run"] == main_mod.SCOUT_SCHEDULE_DEFAULT_PER_RUN
    assert body["every_hours"] == main_mod.SCOUT_SCHEDULE_DEFAULT_HOURS
    assert body["last_run_at"] == ""


def test_turning_it_on_through_the_api_is_read_back(api_client):
    r = api_client.post("/api/scout/schedule", json={"enabled": True, "per_run": 5,
                                                      "every_hours": 2})
    assert r.status_code == 200
    body = api_client.get("/api/scout/schedule").json()
    assert body == {"enabled": True, "per_run": 5, "every_hours": 2, "last_run_at": ""}


def test_turning_it_off_again_is_also_read_back(api_client):
    api_client.post("/api/scout/schedule", json={"enabled": True})
    api_client.post("/api/scout/schedule", json={"enabled": False})
    assert api_client.get("/api/scout/schedule").json()["enabled"] is False


@pytest.mark.parametrize("field, bad", [
    ("every_hours", 0), ("every_hours", 25), ("every_hours", -1),
    ("per_run", 0), ("per_run", 101), ("per_run", -5),
])
def test_out_of_range_schedule_values_are_refused(api_client, field, bad):
    r = api_client.post("/api/scout/schedule", json={field: bad})
    assert r.status_code == 400
    assert r.json()["success"] is False
    # And nothing was written.
    body = api_client.get("/api/scout/schedule").json()
    default = (main_mod.SCOUT_SCHEDULE_DEFAULT_HOURS if field == "every_hours"
              else main_mod.SCOUT_SCHEDULE_DEFAULT_PER_RUN)
    assert body[field] == default


def test_a_non_numeric_schedule_value_is_refused_not_a_500(api_client):
    r = api_client.post("/api/scout/schedule", json={"every_hours": "soon"})
    assert r.status_code == 400


def test_run_now_refuses_an_out_of_range_limit(api_client):
    r = api_client.post("/api/scout/sweep", json={"limit": 0})
    assert r.status_code == 400
    r = api_client.post("/api/scout/sweep", json={"limit": 101})
    assert r.status_code == 400


def test_run_now_starts_a_background_job(api_client):
    p1, p2, p3 = _patched()
    with p1, p2, p3:
        r = api_client.post("/api/scout/sweep", json={"limit": 2})
        assert r.status_code == 200 and r.json()["started"] is True
        for _ in range(60):
            status = api_client.get("/api/scout/sweep").json()
            if status["state"] != "running":
                break
            import time
            time.sleep(0.1)
    assert status["state"] == "done"
    assert status["batch_id"]
    assert status["report"]["staged"] > 0


def test_a_second_run_now_is_refused_while_one_is_in_flight(api_client):
    import threading
    release = threading.Event()

    async def slow_search(self, query):
        release.wait(timeout=5)
        return SEARCH_RESULTS

    with patch.object(Scout, "_web_search", new=slow_search), \
         patch.object(Scout, "_scrape_company_info",
                      new=AsyncMock(return_value={"name": "X", "description": "",
                                                  "website": "https://x.test"})), \
         patch("sellbird.fetch.load_segments", return_value=SEGMENTS):
        first = api_client.post("/api/scout/sweep", json={"limit": 2})
        assert first.status_code == 200
        second = api_client.post("/api/scout/sweep", json={"limit": 2})
        assert second.status_code == 409
        release.set()
        for _ in range(60):
            if api_client.get("/api/scout/sweep").json()["state"] != "running":
                break
            import time
            time.sleep(0.1)


def test_the_agent_cannot_turn_the_schedule_on_or_run_it(tmp_path, monkeypatch):
    """Spending (even free) automated behaviour is a human decision:
    sellbird.agent_policy's allowlist leaves these routes to the user only."""
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "sellbird.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    dash.agent_token_store.configure(tmp_path / "data" / "agent-token")
    agent_token = dash.agent_token_store.get()
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {agent_token}"}) as c:
        assert c.get("/api/scout/schedule").status_code == 403
        assert c.post("/api/scout/schedule", json={"enabled": True}).status_code == 403
        assert c.post("/api/scout/sweep", json={}).status_code == 403


def test_the_apollo_candidates_route_has_the_same_zero_limit_fix(api_client, monkeypatch):
    """The exact same falsy-zero bug existed in the older, similar route
    this one was modelled on (POST /api/apollo/candidates): limit=0 was
    silently promoted to the default instead of being refused."""
    monkeypatch.setattr("sellbird.integrations.apollo.apollo_key", lambda: "fake-key")
    r = api_client.post("/api/apollo/candidates", json={"limit": 0})
    assert r.status_code == 400
    r = api_client.post("/api/apollo/candidates", json={"limit": 501})
    assert r.status_code == 400

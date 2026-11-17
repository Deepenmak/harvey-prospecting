"""Apollo's free search, staged rather than bought.

The search returns people with their addresses withheld. Staging them is
free; buying the addresses is a separate, priced, capped decision. No test
here calls Apollo.
"""

import asyncio
import csv
import json

import pytest

from sellbird import apollo_ops, fetch, import_service
from sellbird.import_staging import StagingError
from sellbird.import_store import ImportStore

CANDIDATES = [
    {"segment": "us_insurance", "apollo_id": "a1", "first_name": "Dana",
     "title": "Chief Claims Officer", "company": "Example Mutual",
     "employees": "430", "industry": "insurance",
     "website": "https://www.example.com/", "title_rank": "0",
     "verdict": "", "reason": ""},
    {"segment": "us_insurance", "apollo_id": "a2", "first_name": "Lee",
     "title": "VP Claims", "company": "Northside Mutual", "employees": "900",
     "industry": "insurance", "website": "northside.test", "title_rank": "1",
     "verdict": "", "reason": ""},
]


class Report:
    """What run_fetch hands back from a plan (free) run."""
    def __init__(self, stopped=""):
        self.stopped = stopped
        self.segment = "us_insurance"
        self.searched = 200


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "CONTACTS_FILE", tmp_path / "contacts_all.csv")
    monkeypatch.setattr(fetch, "EMAIL_CONTACTS_FILE", tmp_path / "contacts_with_email.csv")
    monkeypatch.setattr(fetch, "LOCAL_LEDGER", tmp_path / "fetch_ledger.json")
    monkeypatch.setattr(fetch, "PLAN_FILE", tmp_path / "plan.csv")
    return tmp_path


@pytest.fixture
def store(workspace):
    s = ImportStore(db_path=str(workspace / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


def plan_run(workspace, monkeypatch, rows=CANDIDATES, stopped=""):
    """Stand in for run_fetch's free plan: write plan.csv, spend nothing."""
    async def _fake(state, config, **kwargs):
        assert kwargs.get("plan") is True, "staging must never run the paid path"
        assert kwargs.get("dry_run", False) is False
        with open(fetch.PLAN_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["apollo_id"])
            w.writeheader()
            w.writerows(rows)
        return Report(stopped)
    monkeypatch.setattr(fetch, "run_fetch", _fake)


def test_candidates_are_staged_with_their_apollo_ids_and_no_addresses(
        store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch)
    result = asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))
    assert result["staged"] == 2
    rows = asyncio.run(store.get_rows(result["batch_id"]))
    assert {r["apollo_id"] for r in rows} == {"a1", "a2"}
    assert all(r["email"] == "" for r in rows), "the free search withholds addresses"
    assert rows[0]["domain"] == "example.com"      # www. and scheme stripped
    assert rows[0]["title"] == "Chief Claims Officer"
    batch = asyncio.run(store.get_batch(result["batch_id"]))
    assert batch["credits_spent"] == 0
    assert batch["source"] == "apollo"


def test_the_staged_candidates_are_priced_at_a_credit_each(store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch)
    batch_id = asyncio.run(
        import_service.stage_from_apollo(store, None, "us_insurance", 25))["batch_id"]
    quote = asyncio.run(import_service.estimate(store, batch_id, "people_enrich_id"))
    assert (quote["rows"], quote["credits"]) == (2, 2)
    # Matching by email is useless here: there are no addresses to match on.
    by_email = asyncio.run(import_service.estimate(store, batch_id, "people_match_email"))
    assert by_email["rows"] == 0


def test_buying_the_addresses_respects_the_cap(store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch)
    batch_id = asyncio.run(
        import_service.stage_from_apollo(store, None, "us_insurance", 25))["batch_id"]

    class FakeApollo:
        async def enrich(self, person_id):
            return {"id": person_id, "first_name": "Dana", "last_name": "Fields",
                    "title": "Chief Claims Officer", "email": f"{person_id}@example.com",
                    "email_status": "verified", "seniority": "c_suite",
                    "organization": {"name": "Example Mutual",
                                     "website_url": "https://example.com"}}

    report = asyncio.run(import_service.enrich(
        store, batch_id, "people_enrich_id", max_credits=1, client=FakeApollo()))
    assert report["credits"] == 1
    rows = asyncio.run(store.get_rows(batch_id, status="enriched"))
    assert len(rows) == 1
    assert json.loads(rows[0]["enriched_json"])["email_status"] == "verified"


def test_a_candidate_already_in_the_pipeline_is_not_bought_again(
        store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch)
    first = asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))
    rows = asyncio.run(store.get_rows(first["batch_id"]))
    # Pretend one was bought and promoted earlier.
    asyncio.run(store.update_row(rows[0]["id"], email="a1@example.com"))
    asyncio.run(import_service.promote(store, first["batch_id"]))

    plan_run(workspace, monkeypatch)
    again = asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))
    staged = asyncio.run(store.get_rows(again["batch_id"]))

    # A candidate has no address yet, by design, so its Apollo person id is
    # the only identity it has. Checking the address alone used to let a
    # candidate already paid for be staged, priced and bought a second time.
    bought = next(r for r in staged if r["apollo_id"] == rows[0]["apollo_id"])
    assert bought["status"] == "duplicate"
    # The one nobody bought is still work to do.
    assert any(r["status"] == "staged" for r in staged)
    # And the apollo id dedups inside one batch too.
    assert len({r["apollo_id"] for r in staged}) == len(staged)


def test_a_duplicate_candidate_is_priced_at_nothing(store, workspace, monkeypatch):
    """The point of marking it: it must not appear in the bill."""
    plan_run(workspace, monkeypatch)
    first = asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))
    rows = asyncio.run(store.get_rows(first["batch_id"]))
    asyncio.run(store.update_row(rows[0]["id"], email="a1@example.com"))
    asyncio.run(import_service.promote(store, first["batch_id"]))

    plan_run(workspace, monkeypatch)
    again = asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))
    quote = asyncio.run(import_service.estimate(store, again["batch_id"],
                                                "people_enrich_id"))
    assert quote["credits"] == 1, "only the candidate nobody owns yet costs"
    assert quote["rows"] == 1


def test_a_search_that_finds_nothing_says_so(store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch, rows=[], stopped="credit cap reached")
    with pytest.raises(StagingError):
        asyncio.run(import_service.stage_from_apollo(store, None, "us_insurance", 25))


def test_the_menu_leads_with_buying_these_addresses(store, workspace, monkeypatch):
    plan_run(workspace, monkeypatch)
    batch_id = asyncio.run(
        import_service.stage_from_apollo(store, None, "us_insurance", 25))["batch_id"]
    rows = asyncio.run(store.get_rows(batch_id))
    assert apollo_ops.recommend(rows)[0]["op"] == "people_enrich_id"

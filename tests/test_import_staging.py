"""Staged imports: parse, price, enrich inside a cap, promote.

No test here touches the network or an Apollo credit: the client is a stand-in
that counts what it was asked for.
"""

import asyncio
import csv
import json
import sqlite3

import pytest

from sellbird import apollo_ops, fetch, import_service
from sellbird.import_analysis import counts_for, fallback
from sellbird.import_staging import (StagingError, map_headers, parse,
                                     rows_from)
from sellbird.import_store import ImportStore

CSV_TEXT = (
    "Full Name,Job Title,Company Name,Work Email,Company Website\n"
    "Dana Fields,Chief Claims Officer,Example Mutual,dana@example.com,https://example.com/\n"
    "Lee Okafor,VP Revenue Cycle,Northside Health,lee@northside.test,northside.test\n"
    "Sam Reyes,CTO,Pine Fintech,,pinefin.test\n"
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "CONTACTS_FILE", tmp_path / "contacts_all.csv")
    monkeypatch.setattr(fetch, "EMAIL_CONTACTS_FILE", tmp_path / "contacts_with_email.csv")
    monkeypatch.setattr(fetch, "LOCAL_LEDGER", tmp_path / "fetch_ledger.json")
    return tmp_path


@pytest.fixture
def store(workspace):
    s = ImportStore(db_path=str(workspace / "sellbird.db"))
    asyncio.run(s.init_db())
    return s


class FakeApollo:
    """Answers like Apollo, counts what it was asked, charges nothing."""

    def __init__(self, found=True):
        self.found = found
        self.calls = 0
        self.people = 0

    async def match(self, *, email="", first_name="", last_name="", domain="",
                    organization_name=""):
        self.calls += 1
        self.people += 1
        if not self.found:
            return None
        return self._person(email or f"{first_name}@{domain}".lower())

    async def bulk_match(self, people):
        self.calls += 1
        self.people += len(people)
        if not self.found:
            return [None for _ in people]
        return [self._person(p.get("email", "")) for p in people]

    async def enrich_organization(self, domain):
        self.calls += 1
        return {"name": "Example Mutual", "industry": "insurance",
                "estimated_num_employees": 430,
                "technology_names": ["Salesforce", "Guidewire"]}

    async def search_organizations(self, filters, page=1, per_page=100):
        self.calls += 1
        return {"organizations": [{"name": "Example Mutual", "industry": "insurance"}]}

    @staticmethod
    def _person(email):
        local = (email or "someone@example.com").split("@")[0]
        return {
            "id": "apollo-" + local, "first_name": local.title(), "last_name": "Fields",
            "title": "Chief Claims Officer", "email": email, "email_status": "verified",
            "seniority": "c_suite", "linkedin_url": "",
            "organization": {"name": "Example Mutual", "website_url": "https://example.com",
                             "industry": "insurance", "estimated_num_employees": 430},
        }


def stage(store, text=CSV_TEXT, name="list.csv", fmt=""):
    return asyncio.run(import_service.stage_file(store, name, text, fmt))


# ── Reading a file ───────────────────────────────────────────────────


def test_headers_are_mapped_by_name_whatever_the_export_called_them():
    mapping = map_headers(["Full Name", "Job Title", "Company Name",
                           "Work Email", "Company Website"])
    assert mapping["Work Email"] == "email"
    assert mapping["Job Title"] == "title"
    assert mapping["Company Name"] == "company"
    assert mapping["Company Website"] == "domain"
    assert mapping["Full Name"] == "full_name"


def test_a_single_name_column_is_split_and_a_domain_inferred_from_the_address():
    fmt, headers, raw = parse(CSV_TEXT)
    rows = rows_from(raw, map_headers(headers))
    assert fmt == "csv"
    assert (rows[0]["first_name"], rows[0]["last_name"]) == ("Dana", "Fields")
    assert rows[0]["domain"] == "example.com"      # from the website column
    assert rows[1]["domain"] == "northside.test"


def test_a_pasted_list_of_addresses_is_a_contact_list():
    fmt, headers, raw = parse("dana@example.com, lee@northside.test\nsam@pinefin.test")
    assert fmt == "paste"
    assert len(raw) == 3
    assert raw[0]["email"] == "dana@example.com"


def test_a_sellbird_export_keeps_every_column_it_carried():
    text = json.dumps([{"apollo_id": "a1", "email": "dana@example.com",
                        "first_name": "Dana", "company": "Example Mutual",
                        "angle": "Storm-season claims surges.",
                        "email_status": "verified"}])
    fmt, headers, raw = parse(text)
    assert fmt == "json"
    assert raw[0]["angle"] == "Storm-season claims surges."


def test_rubbish_is_refused_with_a_reason():
    for bad in ("", "   ", "no addresses and no columns here"):
        with pytest.raises(StagingError):
            parse(bad)


def test_a_semicolon_export_still_parses():
    fmt, headers, raw = parse("Email;Company\ndana@example.com;Example Mutual\n")
    assert fmt == "csv" and raw[0]["Email"] == "dana@example.com"


# ── Pricing, before anything is called ───────────────────────────────


def test_every_operation_prices_itself_against_the_real_rows():
    _, headers, raw = parse(CSV_TEXT)
    rows = rows_from(raw, map_headers(headers))
    by_key = {op["key"]: op for op in apollo_ops.menu(rows)}
    # Two of the three rows have an address.
    assert by_key["people_match_email"]["eligible"] == 2
    assert by_key["people_match_email"]["estimated_credits"] == 2
    # All three have a company or a domain, and looking those up is free.
    assert by_key["org_search"]["eligible"] == 3
    assert by_key["org_search"]["estimated_credits"] == 0
    # A mobile number costs eight, and says so.
    assert by_key["phone_reveal"]["credits_per_unit"] == 8
    assert by_key["phone_reveal"]["danger"] is True


def test_the_recommendation_never_suggests_the_expensive_one():
    _, headers, raw = parse(CSV_TEXT)
    rows = rows_from(raw, map_headers(headers))
    assert all(r["op"] != "phone_reveal" for r in apollo_ops.recommend(rows))


def test_sellbird_can_read_a_file_with_no_brain_at_all(store):
    result = stage(store)
    analysis = result["analysis"]
    assert analysis["by"] == "sellbird"
    assert analysis["pending"] is True      # Claude has not had its turn yet
    assert analysis["quality"]["with_email"] == 2
    assert analysis["recommended"][0]["op"] in apollo_ops.OPS
    assert "credits" in analysis["summary"]


# ── Staging ──────────────────────────────────────────────────────────


def test_staging_writes_rows_and_spends_nothing(store, workspace):
    result = stage(store)
    assert result["staged"] == 3
    batch = asyncio.run(store.get_batch(result["batch_id"]))
    assert batch["status"] == "review"
    assert batch["credits_spent"] == 0
    # Nothing has reached the pipeline yet.
    with sqlite3.connect(workspace / "sellbird.db") as db:
        assert db.execute("SELECT COUNT(*) FROM prospects").fetchone()[0] == 0


def test_a_contact_already_in_the_pipeline_is_marked_not_bought_again(store):
    first = stage(store)
    asyncio.run(import_service.promote(store, first["batch_id"]))
    again = stage(store)
    assert again["duplicates"] >= 1
    rows = asyncio.run(store.get_rows(again["batch_id"], status="duplicate"))
    assert "dana@example.com" in {r["email"] for r in rows}


def test_a_contact_you_already_have_is_not_paid_for_again(store):
    first = stage(store)
    asyncio.run(import_service.promote(store, first["batch_id"]))
    again = stage(store)
    # Both addresses are already yours, so matching them is not even offered.
    out = asyncio.run(import_service.estimate(store, again["batch_id"],
                                              "people_match_email"))
    assert (out["rows"], out["credits"]) == (0, 0)
    report = asyncio.run(import_service.enrich(
        store, again["batch_id"], "people_match_email", max_credits=10,
        client=FakeApollo()))
    assert report["credits"] == 0


def test_naming_a_duplicate_row_explicitly_still_enriches_it(store):
    first = stage(store)
    asyncio.run(import_service.promote(store, first["batch_id"]))
    again = stage(store)
    rows = asyncio.run(store.get_rows(again["batch_id"], status="duplicate"))
    report = asyncio.run(import_service.enrich(
        store, again["batch_id"], "people_match_email", row_ids=[rows[0]["id"]],
        max_credits=10, client=FakeApollo()))
    assert report["credits"] == 1


def test_a_company_lookup_unlocks_its_job_postings(store):
    """Each operation can leave behind what the next one needs."""
    batch_id = stage(store)["batch_id"]
    client = FakeApolloOrgs()
    asyncio.run(import_service.enrich(store, batch_id, "org_enrich",
                                      max_credits=10, client=client))
    rows = asyncio.run(store.get_rows(batch_id))
    assert apollo_ops.OPS["org_job_postings"].eligible(rows), \
        "an enriched company carries the Apollo id its postings need"
    report = asyncio.run(import_service.enrich(
        store, batch_id, "org_job_postings", max_credits=10, client=client))
    assert report["credits"] >= 1
    enriched = [json.loads(r["enriched_json"]) for r in
                asyncio.run(store.get_rows(batch_id, status="enriched"))]
    assert any(e.get("hiring_roles") for e in enriched)
    # The company facts from the first operation survived the second.
    assert any(e.get("tech_stack") for e in enriched)


class FakeApolloOrgs(FakeApollo):
    async def enrich_organization(self, domain):
        self.calls += 1
        return {"id": "org-1", "name": "Example Mutual", "industry": "insurance",
                "estimated_num_employees": 430,
                "technology_names": ["Salesforce", "Guidewire"]}

    async def job_postings(self, organization_id, page=1):
        self.calls += 1
        return [{"title": "Machine Learning Engineer"}, {"title": "Claims Analyst"}]


def test_the_recommendation_leads_with_what_makes_contacts_not_what_is_cheapest(store):
    _, headers, raw = parse(CSV_TEXT)
    rows = rows_from(raw, map_headers(headers))
    assert apollo_ops.recommend(rows)[0]["op"] == "people_match_email"


def test_the_same_address_twice_in_one_file_is_staged_once(store):
    text = CSV_TEXT + "Dana Fields,Chief Claims Officer,Example Mutual,dana@example.com,example.com\n"
    result = stage(store, text)
    assert result["staged"] == 3


def test_a_corrected_mapping_re_reads_the_rows_from_the_untouched_source(store):
    result = stage(store)
    batch_id = result["batch_id"]
    changed = asyncio.run(import_service.remap(store, batch_id, {
        "Work Email": "email", "Company Name": "company", "Job Title": "company"}))
    assert changed >= 1
    rows = asyncio.run(store.get_rows(batch_id))
    assert rows[0]["title"] == ""          # no column maps to title any more
    assert rows[0]["email"] == "dana@example.com"


# ── Enriching, inside the cap ────────────────────────────────────────


def test_enrichment_charges_a_credit_a_person_and_records_the_answer(store):
    batch_id = stage(store)["batch_id"]
    client = FakeApollo()
    report = asyncio.run(import_service.enrich(
        store, batch_id, "people_match_email", max_credits=10, client=client))
    assert (report["found"], report["credits"]) == (2, 2)
    rows = asyncio.run(store.get_rows(batch_id, status="enriched"))
    assert len(rows) == 2
    assert all(r["email_status"] == "verified" for r in rows)
    assert all(json.loads(r["enriched_json"])["title"] for r in rows)
    batch = asyncio.run(store.get_batch(batch_id))
    assert batch["credits_spent"] == 2


def test_the_cap_is_a_ceiling_not_a_suggestion(store):
    batch_id = stage(store)["batch_id"]
    report = asyncio.run(import_service.enrich(
        store, batch_id, "people_match_email", max_credits=1, client=FakeApollo()))
    assert report["credits"] <= 1
    assert "cap" in (report.get("stopped") or "")


def test_enriching_without_a_cap_is_refused(store):
    batch_id = stage(store)["batch_id"]
    with pytest.raises(StagingError):
        asyncio.run(import_service.enrich(store, batch_id, "people_match_email",
                                          max_credits=0, client=FakeApollo()))


def test_a_person_apollo_cannot_find_costs_nothing(store):
    batch_id = stage(store)["batch_id"]
    report = asyncio.run(import_service.enrich(
        store, batch_id, "people_match_email", max_credits=10,
        client=FakeApollo(found=False)))
    assert (report["found"], report["credits"]) == (0, 0)


def test_a_row_is_never_enriched_twice(store):
    batch_id = stage(store)["batch_id"]
    client = FakeApollo()
    asyncio.run(import_service.enrich(store, batch_id, "people_match_email",
                                      max_credits=10, client=client))
    again = asyncio.run(import_service.enrich(store, batch_id, "people_match_email",
                                              max_credits=10, client=client))
    assert again["credits"] == 0            # the enriched rows are no longer targets
    batch = asyncio.run(store.get_batch(batch_id))
    assert batch["credits_spent"] == 2


def test_a_daily_cap_stops_a_run_the_user_approved(store):
    asyncio.run(store.set_setting(import_service.DAILY_CAP_KEY, "1"))
    batch_id = stage(store)["batch_id"]
    report = asyncio.run(import_service.enrich(
        store, batch_id, "people_match_email", max_credits=50, client=FakeApollo()))
    assert report["credits"] <= 1


def test_a_free_operation_needs_no_cap(store):
    batch_id = stage(store)["batch_id"]
    report = asyncio.run(import_service.enrich(
        store, batch_id, "org_search", max_credits=0, client=FakeApollo()))
    assert report["credits"] == 0
    assert report["attempted"] == 3


def test_estimating_calls_nothing_and_quotes_the_registry(store):
    batch_id = stage(store)["batch_id"]
    out = asyncio.run(import_service.estimate(store, batch_id, "people_match_email"))
    assert (out["rows"], out["credits"]) == (2, 2)
    assert out["skipped"] == 1


# ── Promoting ────────────────────────────────────────────────────────


def test_promoting_creates_contacts_and_keeps_apollo_s_verdict(store, workspace):
    batch_id = stage(store)["batch_id"]
    asyncio.run(import_service.enrich(store, batch_id, "people_match_email",
                                      max_credits=10, client=FakeApollo()))
    result = asyncio.run(import_service.promote(store, batch_id))
    # Two of the three rows had an address; the third has a company and no
    # person, so there is no contact to make from it.
    assert result["promoted"] == 2
    stored = asyncio.run(store.get_prospect_by_email("dana@example.com"))
    assert stored is not None and stored.email_status == "verified"
    rows = list(csv.DictReader(fetch.CONTACTS_FILE.open(encoding="utf-8")))
    assert len(rows) == 2


def test_an_unverified_address_is_promoted_as_a_guess_and_never_sent(store):
    batch_id = stage(store)["batch_id"]
    asyncio.run(import_service.promote(store, batch_id))
    stored = asyncio.run(store.get_prospect_by_email("lee@northside.test"))
    assert stored.email_status == "guess"


def test_promoted_rows_cannot_be_promoted_again(store):
    batch_id = stage(store)["batch_id"]
    asyncio.run(import_service.promote(store, batch_id))
    again = asyncio.run(import_service.promote(store, batch_id))
    assert again["promoted"] == 0


def test_a_sellbird_export_keeps_its_research_through_promotion(store):
    text = json.dumps([{"apollo_id": "a1", "email": "dana@example.com",
                        "first_name": "Dana", "last_name": "Fields",
                        "title": "Chief Claims Officer", "company": "Example Mutual",
                        "domain": "example.com", "email_status": "verified",
                        "score": 95, "angle": "Storm-season claims surges."}])
    batch_id = stage(store, text, "export.json")["batch_id"]
    asyncio.run(import_service.promote(store, batch_id))
    rows = list(csv.DictReader(fetch.CONTACTS_FILE.open(encoding="utf-8")))
    assert rows[0]["angle"] == "Storm-season claims surges."
    assert rows[0]["score"] == "95"


def test_counts_and_fallback_describe_the_list_without_claude():
    _, headers, raw = parse(CSV_TEXT)
    rows = rows_from(raw, map_headers(headers))
    counts = counts_for(rows)
    assert (counts["rows"], counts["with_email"], counts["with_company"]) == (3, 2, 3)
    assert fallback(rows, headers, map_headers(headers))["recommended"]

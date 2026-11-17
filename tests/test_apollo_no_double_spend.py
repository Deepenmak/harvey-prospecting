"""Never pay twice for one answer.

Three separate leaks lived here, and all three billed real credits for data
the workspace already had:

1. Three operations produce the same thing -- a work address and its status.
   Buying an address with one of them made the row *newly* eligible for the
   next, because that one needs an address to work. So the natural order
   (buy the addresses, then "find each person by email") charged a second
   credit a row for an answer already on the row.

2. A company record costs a credit a company, and the ``unit`` field said
   "company", but the arithmetic and the runner both worked per row. Five
   colleagues at one firm cost five credits for one company's record.

3. A candidate from Apollo's free search has no address yet, by design, so
   the duplicate check -- which matched on the address -- never caught one.
   A candidate bought in an earlier run was staged, priced and bought again.

No test here calls Apollo.
"""

import asyncio
import json

import pytest

from sellbird import apollo_ops
from sellbird.apollo_store import ApolloStore
from sellbird.import_store import ImportStore


def run(coro):
    return asyncio.run(coro)


def row(row_id, **over):
    base = {"id": row_id, "apollo_id": "", "email": "", "email_status": "",
            "first_name": "", "last_name": "", "company": "", "domain": "",
            "title": "", "route": "", "enriched_json": "{}", "status": "staged"}
    base.update(over)
    return base


PERSON_OPS = ("people_enrich_id", "people_match_email", "people_match_name_domain")


# ── 1. One answer, one credit, whoever fetched it ────────────────────


def test_a_row_that_already_has_an_address_is_offered_to_no_person_op():
    done = row("1", apollo_id="x1", first_name="Dana", domain="ex.example.com",
               email="dana@ex.example.com", email_status="verified",
               route="people_enrich_id")
    for key in PERSON_OPS:
        op = apollo_ops.OPS[key]
        assert op.eligible([done]) == [], key
        assert op.estimate([done]) == 0, key


def test_buying_an_address_does_not_make_the_next_op_want_the_row():
    """The leak exactly: people_match_email needs an address, so the moment
    people_enrich_id delivered one the row looked like new work."""
    candidate = row("1", apollo_id="x1")
    assert apollo_ops.OPS["people_enrich_id"].estimate([candidate]) == 1
    assert apollo_ops.OPS["people_match_email"].estimate([candidate]) == 0

    bought = row("1", apollo_id="x1", route="people_enrich_id",
                 email="dana@ex.example.com", email_status="verified")
    assert apollo_ops.OPS["people_match_email"].estimate([bought]) == 0
    assert apollo_ops.OPS["people_enrich_id"].estimate([bought]) == 0


def test_an_address_found_by_an_earlier_op_counts_even_from_enriched_json():
    """A run writes its answer into enriched_json, not always the column."""
    bought = row("1", apollo_id="x1", route="people_enrich_id",
                 enriched_json=json.dumps({"email": "dana@ex.example.com",
                                           "email_status": "verified"}))
    assert apollo_ops.OPS["people_match_email"].estimate([bought]) == 0


def test_a_csv_that_arrives_with_verified_addresses_costs_nothing_to_match():
    ready = [row(str(i), email=f"p{i}@example.com", email_status="verified")
             for i in range(5)]
    assert apollo_ops.OPS["people_match_email"].estimate(ready) == 0
    assert apollo_ops.OPS["people_match_email"].as_dict(ready)["already_have"] == 5


def test_a_row_with_no_address_is_not_said_to_already_have_one():
    """"5 already have this" on five rows with no address at all is the
    opposite of true. Only rows skipped for holding the answer count."""
    nothing_yet = [row(str(i), apollo_id=f"x{i}", first_name="Sam",
                       domain="ex.example.com") for i in range(5)]
    card = apollo_ops.OPS["people_match_email"].as_dict(nothing_yet)
    assert card["eligible"] == 0, "it needs an address, and there is none"
    assert card["already_have"] == 0, "and that is not the same as having one"


def test_a_row_still_missing_the_status_is_worth_a_credit():
    """An address nobody verified is a guess, and a guess is never sent, so
    the status is the thing being bought."""
    unverified = row("1", email="p1@example.com")
    assert apollo_ops.OPS["people_match_email"].estimate([unverified]) == 1


def test_a_mobile_number_is_still_offered_on_an_enriched_row():
    """Guard against over-blocking: phone_reveal produces a field nothing
    else does, so holding an address is no reason to hide it."""
    bought = row("1", apollo_id="x1", email="dana@ex.example.com",
                 email_status="verified", route="people_enrich_id")
    assert len(apollo_ops.OPS["phone_reveal"].eligible([bought])) == 1


# ── 2. A company costs a credit a company ────────────────────────────


class FakeApollo:
    """Counts its calls. Nothing here reaches the network."""

    def __init__(self):
        self.org_calls: list[str] = []
        self.posting_calls: list[str] = []

    async def enrich_organization(self, domain):
        self.org_calls.append(domain)
        return {"id": f"org_{domain}", "name": domain, "industry": "insurance",
                "estimated_num_employees": 400,
                "technology_names": ["Guidewire", "Snowflake"]}

    async def job_postings(self, org_id, page=1):
        self.posting_calls.append(org_id)
        return [{"title": "Claims Analyst"}, {"title": "Data Engineer"}]


def colleagues():
    """Five contacts: three at one company, two at another."""
    return [row(str(i), first_name="Sam", company="Example Mutual",
                domain="ex.example.com" if i < 4 else "other.example.com")
            for i in range(1, 6)]


def test_five_colleagues_at_two_companies_cost_two_credits():
    op = apollo_ops.OPS["org_enrich"]
    rows = colleagues()
    assert len(op.eligible(rows)) == 5
    assert len(op.charge_units(rows)) == 2
    assert op.estimate(rows) == 2
    card = op.as_dict(rows)
    assert (card["eligible"], card["charged"], card["shared"]) == (5, 2, 3)


def test_the_company_is_asked_about_once_and_every_row_gets_the_answer():
    client = FakeApollo()
    rows = colleagues()
    report = run(apollo_ops.run_op(apollo_ops.OPS["org_enrich"], client, rows,
                                   max_credits=10, pause=0))

    assert client.org_calls == ["ex.example.com", "other.example.com"]
    assert report.credits == 2
    assert report.attempted == 5, "all five rows were answered"
    assert report.found == 5
    # Only the row that was called for carries the charge, or the batch total
    # would count the same credit three times.
    assert sorted(r.credits for r in report.results) == [0, 0, 0, 1, 1]
    assert all(r.organization for r in report.results)


def test_the_saving_is_said_out_loud():
    client = FakeApollo()
    report = run(apollo_ops.run_op(apollo_ops.OPS["org_enrich"], client,
                                   colleagues(), max_credits=10, pause=0))
    line = apollo_ops.summarise(report)
    assert "2 credits spent" in line
    assert "5 rows shared 2 companies" in line


def test_a_domain_nobody_else_shares_is_its_own_charge():
    op = apollo_ops.OPS["org_enrich"]
    rows = [row("1", domain="a.example.com"), row("2", domain="b.example.com")]
    assert op.estimate(rows) == 2


def test_the_cap_counts_companies_not_rows():
    """A cap of one buys one company and leaves the rest, rather than being
    spent on three rows of the same company."""
    client = FakeApollo()
    report = run(apollo_ops.run_op(apollo_ops.OPS["org_enrich"], client,
                                   colleagues(), max_credits=1, pause=0))
    assert report.credits == 1
    assert len(client.org_calls) == 1
    assert report.attempted == 3, "the three rows at that one company"
    assert "cap" in report.stopped


def test_open_roles_are_read_once_per_company_too():
    client = FakeApollo()
    rows = [row(str(i), enriched_json=json.dumps(
        {"apollo_org_id": "org_a" if i < 3 else "org_b"})) for i in range(1, 5)]
    report = run(apollo_ops.run_op(apollo_ops.OPS["org_job_postings"], client,
                                   rows, max_credits=10, pause=0))
    assert client.posting_calls == ["org_a", "org_b"]
    assert report.credits == 2 and report.attempted == 4


def test_a_company_already_profiled_is_not_bought_again():
    known = row("1", domain="ex.example.com", enriched_json=json.dumps({
        "tech_stack": "Guidewire", "employees": "400", "industry": "insurance"}))
    assert apollo_ops.OPS["org_enrich"].estimate([known]) == 0


def test_a_company_missing_only_its_tech_stack_is_still_worth_asking_about():
    partial = row("1", domain="ex.example.com",
                  enriched_json=json.dumps({"employees": "400",
                                            "industry": "insurance"}))
    assert apollo_ops.OPS["org_enrich"].estimate([partial]) == 1


def test_one_route_to_an_address_is_recommended_not_three():
    """Three operations return a work address. Recommending all of them
    reads as "do all three", at a credit a row each time."""
    candidates = [row(str(i), apollo_id=f"x{i}", first_name="Sam",
                      company="Example Mutual", domain="ex.example.com")
                  for i in range(5)]
    advice = apollo_ops.recommend(candidates)
    address_routes = [r["op"] for r in advice if r["op"] in PERSON_OPS]
    assert address_routes == ["people_enrich_id"]
    # Five contacts at one company: five addresses and one company record.
    assert sum(r["credits"] for r in advice) == 6


def test_the_recommendation_quotes_the_company_price():
    advice = {r["op"]: r for r in apollo_ops.recommend(colleagues())}
    assert advice["org_enrich"]["credits"] == 2
    assert advice["org_enrich"]["rows"] == 5
    assert "share 2 companies" in advice["org_enrich"]["why"]


# ── 3. Someone bought in an earlier run ──────────────────────────────


@pytest.fixture
def stores(tmp_path):
    path = str(tmp_path / "sellbird.db")
    imports = ImportStore(db_path=path)
    run(imports.init_db())
    return imports, ApolloStore(db_path=path)


def stage(imports, rows):
    batch_id = run(imports.create_batch("a list", "csv", "review"))
    run(imports.add_rows(batch_id, rows))
    return batch_id


def test_a_candidate_bought_in_an_earlier_run_is_marked_not_priced(stores):
    imports, apollo = stores
    run(apollo.upsert_contacts("", [{"apollo_id": "x1", "email": "dana@example.com",
                                     "company": "Example Mutual"}]))

    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"},
                               {"apollo_id": "x2", "company": "Other Mutual"}])
    assert run(imports.mark_known_contacts(batch_id)) == 1

    rows = run(imports.get_rows(batch_id))
    by_id = {r["apollo_id"]: r["status"] for r in rows}
    assert by_id["x1"] == "duplicate"
    assert by_id["x2"] == "staged"


def test_a_credit_spent_on_someone_counts_even_with_no_contact_left(stores):
    """The ledger remembers the spend whatever happened to the contact
    afterwards, and that is the honest basis for not spending again."""
    imports, apollo = stores
    run(apollo.mark_seen("person", ["x1"], "fetched"))

    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"}])
    assert run(imports.mark_known_contacts(batch_id)) == 1
    assert run(imports.get_rows(batch_id))[0]["status"] == "duplicate"


def test_merely_looking_at_someone_is_not_paying_for_them(stores):
    """A staged candidate is not a bought one: it must stay buyable."""
    imports, apollo = stores
    run(apollo.mark_seen("person", ["x1"], "staged"))

    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"}])
    assert run(imports.mark_known_contacts(batch_id)) == 0
    assert run(imports.get_rows(batch_id))[0]["status"] == "staged"


def test_a_credit_is_recorded_the_moment_it_leaves(stores):
    """Not at the end of the run. A container stopped in between used to
    lose every id already paid for, and the next run bought them again."""
    imports, apollo = stores
    from sellbird.fetch import DbLedger, mark_spoken_for

    run(mark_spoken_for(DbLedger(apollo, None), "x1", "fetched"))
    people, _ = run(apollo.seen())
    assert "x1" in people

    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"}])
    assert run(imports.mark_known_contacts(batch_id)) == 1


def test_a_person_apollo_had_nothing_for_is_not_treated_as_bought(stores):
    """Apollo charges nothing for a miss, so a miss must not read as "we
    already own them" -- its data may be better next quarter."""
    imports, apollo = stores
    from sellbird.fetch import DbLedger, mark_spoken_for

    run(mark_spoken_for(DbLedger(apollo, None), "x1", "missed"))
    people, _ = run(apollo.seen())
    assert "x1" in people, "a later search still skips them"

    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"}])
    assert run(imports.mark_known_contacts(batch_id)) == 0
    assert run(imports.get_rows(batch_id))[0]["status"] == "staged"


def test_an_address_from_an_earlier_run_is_caught_as_well(stores):
    imports, apollo = stores
    run(apollo.upsert_contacts("", [{"apollo_id": "x9",
                                     "email": "dana@example.com"}]))

    batch_id = stage(imports, [{"email": "dana@example.com"},
                               {"email": "new@example.com"}])
    assert run(imports.mark_known_contacts(batch_id)) == 1
    statuses = {r["email"]: r["status"] for r in run(imports.get_rows(batch_id))}
    assert statuses["dana@example.com"] == "duplicate"
    assert statuses["new@example.com"] == "staged"


# ── Not offering what cannot run ─────────────────────────────────────


def test_an_operation_with_no_implementation_is_not_offered_as_runnable():
    """A card with no runner behind it was still enabled and pressable, and
    pressing it said "people_search cannot be run yet". A button that lies
    about what it does is worse than no button."""
    cards = {card["key"]: card for card in apollo_ops.menu([])}
    assert cards["people_search"]["available"] is False
    assert cards["phone_reveal"]["available"] is False
    assert cards["people_enrich_id"]["available"] is True
    assert cards["org_enrich"]["available"] is True


def test_nothing_unrunnable_is_ever_recommended():
    rows = [row("1", apollo_id="x1", first_name="Sam", company="Example Mutual",
                domain="ex.example.com")]
    for advice in apollo_ops.recommend(rows):
        assert apollo_ops.runnable(advice["op"]), advice["op"]


def test_asking_to_run_one_is_refused_before_a_cap_is_discussed(stores):
    from sellbird import import_service
    from sellbird.import_staging import StagingError

    imports, _ = stores
    batch_id = stage(imports, [{"apollo_id": "x1", "company": "Example Mutual"}])
    with pytest.raises(StagingError, match="not wired up"):
        run(import_service.enrich(imports, batch_id, "phone_reveal",
                                  max_credits=100))

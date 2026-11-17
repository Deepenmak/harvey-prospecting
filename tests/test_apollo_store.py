"""What Apollo returned has to still be there tomorrow.

Every one of these tests is about the same failure: a contact that cost a
credit, and then went missing. It went missing because the record lived in
two loose files -- exports/contacts_all.csv and data/fetch_ledger.json --
that a redeploy, a fresh volume or a half-finished write could take away.
When that happened the next free search offered the same people again and
the next enrichment charged for them again.

So the record is the database now, and the files are rebuilt from it. These
tests pin that: a run survives, the ledger survives, an older install's
files are read in rather than lost, and deleting exports/ costs nothing.

No test here calls Apollo.
"""

import asyncio
import csv
import json

import pytest

from sellbird import fetch
from sellbird.apollo_store import ApolloStore


def run(coro):
    return asyncio.run(coro)


def contact(apollo_id, **over):
    """A fetched contact in the shape run_fetch builds."""
    row = {c: "" for c in fetch.CONTACT_COLUMNS}
    row.update({
        "apollo_id": apollo_id, "fetched_at": "2026-01-01 09:00",
        "segment": "us_insurance", "first_name": "Dana", "last_name": "Fitch",
        "title": "Chief Claims Officer", "email": f"{apollo_id}@example.com",
        "email_status": "verified", "company": f"Example {apollo_id} Mutual",
        "domain": f"{apollo_id}.example.com", "score": 70, "status": "new",
    })
    row.update(over)
    return row


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway home: its own database, its own exports."""
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(fetch, "CONTACTS_FILE", exports / "contacts_all.csv")
    monkeypatch.setattr(fetch, "EMAIL_CONTACTS_FILE", exports / "contacts_with_email.csv")
    monkeypatch.setattr(fetch, "LOCAL_LEDGER", tmp_path / "fetch_ledger.json")
    monkeypatch.setattr(fetch, "PLAN_FILE", exports / "plan.csv")
    monkeypatch.delenv("SHEET_WEBHOOK_URL", raising=False)
    return tmp_path


@pytest.fixture
def store(workspace):
    s = ApolloStore(db_path=str(workspace / "sellbird.db"))
    run(s.init_db())
    return s


# ── The record itself ────────────────────────────────────────────────


def test_a_run_and_its_contacts_are_still_there_afterwards(store):
    rows = [contact("a1"), contact("a2")]
    run_id = run(store.record_run({"segment": "us_insurance", "mode": "fetch",
                                   "credits_used": 2, "prospects": 2}))
    assert run(store.upsert_contacts(run_id, rows)) == 2

    # A second ApolloStore is a second process as far as SQLite cares.
    again = ApolloStore(db_path=store.db_path)
    stored = run(again.contacts())
    assert [r["apollo_id"] for r in stored] == ["a1", "a2"]
    assert stored[0]["email"] == "a1@example.com"
    assert run(again.credits_used()) == 2
    assert run(again.runs())[0]["segment"] == "us_insurance"


def test_credits_add_up_across_runs_so_the_cap_still_means_something(store):
    for _ in range(3):
        run(store.record_run({"segment": "us_rcm", "credits_used": 4}))
    assert run(store.credits_used()) == 12


def test_a_later_merge_never_erases_what_the_fetch_knew(store):
    run_id = run(store.record_run({"segment": "us_insurance"}))
    run(store.upsert_contacts(run_id, [contact("a1")]))
    # What a review skill writes back: judgement only, every other column blank.
    judgement = {c: "" for c in fetch.CONTACT_COLUMNS}
    judgement.update({"apollo_id": "a1", "fit": "a", "intent": "high",
                      "verified": "pass"})
    run(store.upsert_contacts("", [judgement]))

    stored = run(store.contacts())[0]
    assert stored["fit"] == "a" and stored["intent"] == "high"
    assert stored["email"] == "a1@example.com"      # not blanked
    assert stored["company"] == "Example a1 Mutual"


def test_a_merge_cannot_reattribute_a_contact_to_another_run(store):
    first = run(store.record_run({"segment": "us_insurance"}))
    second = run(store.record_run({"segment": "ca_fintech"}))
    run(store.upsert_contacts(first, [contact("a1")]))
    run(store.upsert_contacts(second, [contact("a1", fit="b")]))

    rows = run(store._rows("SELECT run_id FROM apollo_contacts"))
    assert rows[0]["run_id"] == first


def test_two_people_may_share_one_address(store):
    """A role address (info@, claims@) behind two Apollo ids is uncommon and
    legal. A unique index on the address turned it into an IntegrityError
    that lost the whole run -- the failure these tables exist to prevent."""
    shared = [contact("a1", email="claims@example.com"),
              contact("a2", email="claims@example.com")]
    assert run(store.upsert_contacts("", shared)) == 2
    assert run(store.contact_count()) == 2


def test_a_contact_with_no_apollo_id_is_not_a_contact(store):
    assert run(store.upsert_contacts("", [{"email": "x@example.com"}])) == 0
    assert run(store.contact_count()) == 0


# ── The ledger: who is already spoken for ────────────────────────────


def test_the_first_reason_someone_was_seen_is_the_one_that_stands(store):
    run(store.mark_seen("person", ["a1"], "fetched"))
    run(store.mark_seen("person", ["a1"], "staged"))
    rows = run(store._rows("SELECT reason FROM apollo_seen WHERE value = 'a1'"))
    assert [r["reason"] for r in rows] == ["fetched"]


def test_companies_are_remembered_case_insensitively(store):
    run(store.mark_seen("org", ["Example Mutual", "example mutual", " "]))
    people, orgs = run(store.seen())
    assert orgs == {"example mutual"} and people == set()


@pytest.mark.parametrize("kind,reason", [("company", "fetched"), ("person", "bought")])
def test_a_kind_or_reason_nobody_defined_is_refused(store, kind, reason):
    with pytest.raises(ValueError):
        run(store.mark_seen(kind, ["a1"], reason))


# ── A redeploy ───────────────────────────────────────────────────────


def test_losing_exports_costs_nothing_because_the_files_are_rebuilt(store):
    run_id = run(store.record_run({"segment": "us_insurance", "credits_used": 3}))
    run(store.upsert_contacts(run_id, [contact("a1"), contact("a2"),
                                       contact("a3", email="")]))
    run(store.mark_seen("person", ["a1", "a2", "a3"], "fetched", run_id))
    assert run(fetch.sync_exports(store)) == 3
    assert fetch.CONTACTS_FILE.exists()

    # The new container has the volume's database and an empty exports folder.
    fetch.CONTACTS_FILE.unlink()
    fetch.EMAIL_CONTACTS_FILE.unlink()
    assert await_sync_needed(store) is False, "a missing file is a repair needed"

    result = run(fetch.reconcile(store))
    assert result["stored"] == 3
    rebuilt = fetch.read_contacts(fetch.CONTACTS_FILE)
    assert [r["apollo_id"] for r in rebuilt] == ["a1", "a2", "a3"]
    assert rebuilt[0]["email"] == "a1@example.com"
    # The emailable file holds only the two with an address.
    with open(fetch.EMAIL_CONTACTS_FILE, newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 2
    # And the ledger still knows them, so no search offers them again.
    people, _ = run(store.seen())
    assert people == {"a1", "a2", "a3"}
    assert run(store.credits_used()) == 3


def await_sync_needed(store):
    """exports_in_sync, read the way the sentence reads."""
    return run(fetch.exports_in_sync(store))


def test_the_files_being_whole_is_not_treated_as_a_repair(store):
    run(store.upsert_contacts("", [contact("a1")]))
    run(fetch.sync_exports(store))
    assert await_sync_needed(store) is True


def test_an_empty_workspace_asks_for_no_repair(store):
    assert await_sync_needed(store) is True


def test_an_empty_database_beside_full_files_does_ask_for_a_repair(store):
    """The upgrade case, and the one this check used to miss: the tables are
    new, the files hold 2 contacts, and answering "in sync" meant a dashboard
    opened before the first heartbeat showed nothing at all.
    """
    fetch.save_contacts([contact("a1"), contact("a2")],
                        fetch.CONTACTS_FILE, fetch.EMAIL_CONTACTS_FILE)
    assert run(store.contact_count()) == 0
    assert await_sync_needed(store) is False


# ── An install that predates the tables ──────────────────────────────


def test_an_older_installs_files_are_read_in_rather_than_left_behind(store, workspace):
    """The upgrade path: contacts and credits already on disk come across,
    so the phase cap does not start again from zero and buy the phase twice.
    """
    fetch.save_contacts([contact("a1"), contact("a2")],
                        fetch.CONTACTS_FILE, fetch.EMAIL_CONTACTS_FILE)
    fetch.LOCAL_LEDGER.write_text(json.dumps({
        "runs": [{"ran_at": "2026-01-01 09:00", "segment": "us_insurance",
                  "credits_used": 7, "prospects": 2, "seen_ids": ["a1", "a2"]}],
        "seen_ids": ["a1", "a2", "a9"],
        "seen_orgs": ["Example a1 Mutual"],
        "skipped_orgs": ["Nope Holdings"],
    }), encoding="utf-8")

    result = run(fetch.reconcile(store))

    assert result["stored"] == 2
    assert result["credits_used"] == 7, "credits already spent must carry over"
    people, orgs = run(store.seen())
    assert {"a1", "a2", "a9"} <= people
    assert {"example a1 mutual", "nope holdings"} <= orgs
    # Reconciling again is not a second adoption, and says so honestly: the
    # count is what the database gained, not how many rows were rewritten.
    second = run(fetch.reconcile(store))
    assert second["credits_used"] == 7
    assert second["adopted"]["contacts"] == 0
    assert second["stored"] == 2


def test_a_sheet_and_a_file_are_both_read_in_and_neither_invents_credits(store,
                                                                         workspace):
    """Reading one mirror and not the other is how credits already spent
    came back as zero. Reading both and adding them up would be worse."""
    class Sheet:
        name = "sheet"
        is_local = False

        async def has_history(self):
            return True

        async def load(self):
            return fetch.LedgerState(seen_ids={"a1"}, seen_orgs={"sheet co"},
                                     credits_used=4, last_segment="ca_fintech")

        async def record(self, run_, rows):
            pass

        async def push_angles(self, angles):
            pass

    fetch.LOCAL_LEDGER.write_text(json.dumps({
        "runs": [{"ran_at": "2026-01-01 09:00", "segment": "us_insurance",
                  "credits_used": 4, "seen_ids": ["a2"]}],
        "seen_ids": ["a2"], "seen_orgs": ["file co"], "skipped_orgs": [],
    }), encoding="utf-8")

    ledger = fetch.DbLedger(store, Sheet())
    state = run(ledger.load())

    assert state.seen_ids == {"a1", "a2"}
    assert state.seen_orgs == {"sheet co", "file co"}
    # The same four credits seen twice, not eight.
    assert state.credits_used == 4
    assert state.last_segment == "ca_fintech"


def test_judgement_written_into_the_csv_becomes_durable(store):
    run(store.upsert_contacts("", [contact("a1")]))
    run(fetch.sync_exports(store))
    # What the lead-gen skill does: edit the file, not the database.
    fetch.update_contacts([{"apollo_id": "a1", "fit": "a", "intent": "high"}],
                          path=fetch.CONTACTS_FILE,
                          email_path=fetch.EMAIL_CONTACTS_FILE)
    run(fetch.sync_exports(store))
    assert run(store.contacts())[0]["fit"] == "a"


def test_a_contact_only_the_files_know_about_is_taken_in(store):
    """A promoted import, or a hand-added row, is still a contact."""
    fetch.save_contacts([contact("imported-1")],
                        fetch.CONTACTS_FILE, fetch.EMAIL_CONTACTS_FILE)
    run(fetch.sync_exports(store))
    assert [r["apollo_id"] for r in run(store.contacts())] == ["imported-1"]


# ── Writing a file is all or nothing ─────────────────────────────────


def test_a_write_that_fails_leaves_the_old_file_untouched(tmp_path, monkeypatch):
    from sellbird import paths

    target = tmp_path / "contacts_all.csv"
    target.write_text("the original\n", encoding="utf-8")

    def explode(*args, **kwargs):
        raise OSError("the rename did not happen")

    # The temp file is written in full and the rename is what fails: the
    # closest thing to a container being stopped at the worst moment.
    monkeypatch.setattr("os.replace", explode)
    with pytest.raises(OSError):
        paths.write_atomic(target, "the replacement\n")
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == "the original\n"
    assert not list(tmp_path.glob("*.tmp*")), "no temp file left behind"


def test_a_write_that_succeeds_replaces_the_whole_file(tmp_path):
    from sellbird.paths import write_atomic

    target = tmp_path / "nested" / "ledger.json"
    write_atomic(target, '{"runs": []}')
    assert target.read_text(encoding="utf-8") == '{"runs": []}'
    write_atomic(target, '{"runs": [1]}')
    assert target.read_text(encoding="utf-8") == '{"runs": [1]}'
    assert not list(target.parent.glob("*.tmp*"))


# ── The ledger the fetch run actually uses ───────────────────────────


def test_the_database_is_the_record_and_the_file_is_a_mirror(store, workspace):
    ledger = fetch.DbLedger(store, fetch.FileLedger(fetch.LOCAL_LEDGER))
    rows = [contact("a1"), contact("a2")]
    run_id = run(ledger.record({"ran_at": "2026-01-01 09:00", "mode": "fetch",
                                "segment": "us_insurance", "credits_used": 2,
                                "prospects": 2, "seen_ids": ["a1", "a2"],
                                "skipped_orgs": ["Nope Holdings"]}, rows))

    assert run_id
    assert run(store.contact_count()) == 2
    people, orgs = run(store.seen())
    assert people == {"a1", "a2"}
    assert "nope holdings" in orgs
    # The mirror was written too, and so were the contact files.
    assert json.loads(fetch.LOCAL_LEDGER.read_text(encoding="utf-8"))["runs"]
    assert len(fetch.read_contacts(fetch.CONTACTS_FILE)) == 2

    state = run(ledger.load())
    assert state.seen_ids == {"a1", "a2"}
    assert state.credits_used == 2
    assert state.last_segment == "us_insurance"


def test_a_broken_mirror_does_not_lose_the_run(store, workspace):
    class Broken:
        name = "sheet"
        is_local = False

        async def has_history(self):
            return True

        async def load(self):
            raise RuntimeError("the sheet is unreachable")

        async def record(self, run_, rows):
            raise RuntimeError("the sheet is unreachable")

        async def push_angles(self, angles):
            raise RuntimeError("the sheet is unreachable")

    ledger = fetch.DbLedger(store, Broken())
    run_id = run(ledger.record({"segment": "us_insurance", "credits_used": 1,
                                "seen_ids": ["a1"]}, [contact("a1")]))
    assert run_id
    assert run(store.contact_count()) == 1
    assert run(ledger.load()).credits_used == 1


def test_a_free_search_is_recorded_even_though_it_costs_nothing(store):
    """exports/plan.csv is replaced by the next plan run, so the run row is
    the only lasting answer to "what did that search find"."""
    ledger = fetch.DbLedger(store, None)
    report = fetch.FetchReport(segment="us_insurance", searched=240, candidates=12)
    run_id = run(fetch.record_search(ledger, report, "plan"))

    assert run_id
    stored = run(store.runs())[0]
    assert (stored["mode"], stored["searched"], stored["candidates"]) == ("plan", 240, 12)
    assert stored["credits_used"] == 0
    # A free search must not move the paid rotation.
    assert run(store.last_segment()) == ""


def test_make_ledger_without_a_database_is_the_file_alone(workspace):
    assert isinstance(fetch.make_ledger(), fetch.FileLedger)
    assert fetch.make_ledger().name == "file"


def test_make_ledger_with_a_database_keeps_the_file_as_a_mirror(store):
    ledger = fetch.make_ledger(store)
    assert isinstance(ledger, fetch.DbLedger)
    assert ledger.name == "database+file"
    assert ledger.is_local is True

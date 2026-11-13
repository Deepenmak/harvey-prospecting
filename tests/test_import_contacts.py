"""sellbird import-contacts: a contact list produced elsewhere, folded in.

Nothing here touches the network, spends a credit or sends an email: the
importer only reads JSON and writes the contact files and the database.
"""

import asyncio
import csv
import json
import sqlite3

import pytest

from sellbird import fetch
from sellbird.import_contacts import (ImportError_, import_contacts, notes_for,
                                      parse_payload, rebuild_ledger)
from sellbird.state import StateManager

CONTACT = {
    "apollo_id": "a1", "first_name": "Dana", "last_name": "Fields",
    "title": "Chief Claims Officer", "seniority": "c_suite",
    "email": "Dana@Example.com", "email_status": "verified",
    "company": "Example Mutual", "domain": "Example.com", "industry": "insurance",
    "employees": 430, "city": "Dallas", "state": "Texas", "country": "United States",
    "score": 92, "angle": "Claims surge after storms.", "fit": "a",
    "fit_reason": "Document-heavy claims intake", "signals": "Hiring an ML engineer",
    "segment": "us_insurance",
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway home: contact files, ledger and database all under tmp."""
    monkeypatch.setattr(fetch, "CONTACTS_FILE", tmp_path / "contacts_all.csv")
    monkeypatch.setattr(fetch, "EMAIL_CONTACTS_FILE", tmp_path / "contacts_with_email.csv")
    monkeypatch.setattr(fetch, "LOCAL_LEDGER", tmp_path / "fetch_ledger.json")
    return tmp_path


def state_for(tmp_path) -> StateManager:
    return StateManager(db_path=str(tmp_path / "sellbird.db"))


def count(tmp_path, table: str) -> int:
    with sqlite3.connect(tmp_path / "sellbird.db") as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def stored_contact(state):
    return asyncio.run(state.get_prospect_by_email("dana@example.com"))


# ── Reading the payload ──────────────────────────────────────────────


def test_accepts_a_bare_list_and_an_object_with_runs():
    rows, runs = parse_payload([CONTACT])
    assert rows[0]["apollo_id"] == "a1" and runs == []
    rows, runs = parse_payload({"contacts": [CONTACT], "runs": [{"credits_used": 3}]})
    assert len(rows) == 1 and runs == [{"credits_used": 3}]


def test_accepts_json_text_and_normalises_keys():
    rows, _ = parse_payload(json.dumps([CONTACT]))
    assert rows[0]["email"] == "dana@example.com"   # dedup keys are lowercased
    assert rows[0]["domain"] == "example.com"
    assert rows[0]["employees"] == "430"            # every column is text


def test_a_contact_with_no_apollo_id_is_keyed_on_its_email():
    rows, _ = parse_payload([{**CONTACT, "apollo_id": ""}])
    assert rows[0]["apollo_id"] == "email:dana@example.com"


def test_refuses_what_is_not_a_contact_list():
    for bad in ("not json", {"contacts": "nope"}, [1, 2, 3]):
        with pytest.raises(ImportError_):
            parse_payload(bad)
    with pytest.raises(ImportError_):
        parse_payload([{"company": "No id, no email"}])


def test_notes_carry_the_research_the_writer_needs():
    notes = notes_for(parse_payload([CONTACT])[0][0])
    assert notes.startswith("Claims surge after storms.")
    assert "Fit: Document-heavy claims intake" in notes
    assert "Signals: Hiring an ML engineer" in notes


def test_ledger_counts_credits_and_remembers_what_was_seen(tmp_path):
    rows, runs = parse_payload({"contacts": [CONTACT],
                                "runs": [{"credits_used": 2, "seen_ids": ["a1", "a2"],
                                          "skipped_orgs": ["other ltd"]}]})
    path = tmp_path / "ledger.json"
    assert rebuild_ledger(rows, runs, path) == 2
    ledger = json.loads(path.read_text(encoding="utf-8"))
    assert ledger["seen_ids"] == ["a1", "a2"]
    assert ledger["skipped_orgs"] == ["other ltd"]
    assert ledger["seen_orgs"] == ["example mutual"]


# ── Importing ────────────────────────────────────────────────────────


def test_import_writes_the_contact_files_and_the_database(workspace):
    state = state_for(workspace)
    report = asyncio.run(import_contacts(state, {"contacts": [CONTACT],
                                                 "runs": [{"credits_used": 1}]}))
    assert (report.contacts, report.new_contacts) == (1, 1)
    assert (report.companies_added, report.prospects_added) == (1, 1)
    assert report.credits == 1 and report.problems == []

    rows = list(csv.DictReader(fetch.CONTACTS_FILE.open(encoding="utf-8")))
    assert rows[0]["company"] == "Example Mutual"
    assert fetch.EMAIL_CONTACTS_FILE.exists()

    assert count(workspace, "prospects") == 1
    stored = stored_contact(state)
    assert stored.email == "dana@example.com" and stored.score == 92
    assert stored.email_verified is True
    assert "Claims surge" in stored.personalization_notes


def test_importing_the_same_list_twice_changes_nothing(workspace):
    state = state_for(workspace)
    asyncio.run(import_contacts(state, [CONTACT]))
    again = asyncio.run(import_contacts(state, [CONTACT]))
    assert again.new_contacts == 0
    assert (again.companies_added, again.prospects_added) == (0, 0)
    assert again.prospects_updated == 1
    assert count(workspace, "prospects") == 1
    assert count(workspace, "companies") == 1


def test_an_import_updates_research_but_never_the_status(workspace):
    state = state_for(workspace)
    asyncio.run(import_contacts(state, [CONTACT]))
    stored = stored_contact(state)
    asyncio.run(state.update_prospect_status(stored.id, "contacted"))

    asyncio.run(import_contacts(state, [{**CONTACT, "score": 40,
                                         "angle": "A better angle."}]))
    after = stored_contact(state)
    assert after.id == stored.id
    assert after.status == "contacted"          # the pipeline's, not the import's
    assert after.score == 40
    assert after.personalization_notes.startswith("A better angle.")


def test_a_contact_with_no_email_is_kept_in_the_files_only(workspace):
    state = state_for(workspace)
    report = asyncio.run(import_contacts(state, [{**CONTACT, "email": ""}]))
    assert report.no_email == 1 and report.prospects_added == 0
    assert report.companies_added == 1          # the company is still worth knowing
    rows = list(csv.DictReader(fetch.CONTACTS_FILE.open(encoding="utf-8")))
    assert len(rows) == 1


def test_an_empty_value_never_erases_what_is_already_known(workspace):
    state = state_for(workspace)
    asyncio.run(import_contacts(state, [CONTACT]))
    asyncio.run(import_contacts(state, [{**CONTACT, "angle": "", "signals": ""}]))
    rows = list(csv.DictReader(fetch.CONTACTS_FILE.open(encoding="utf-8")))
    assert rows[0]["angle"] == "Claims surge after storms."
    assert stored_contact(state).personalization_notes


def test_write_files_false_leaves_the_contact_files_alone(workspace):
    state = state_for(workspace)
    report = asyncio.run(import_contacts(state, [CONTACT], write_files=False))
    assert report.files_written is False
    assert not fetch.CONTACTS_FILE.exists()
    assert report.prospects_added == 1

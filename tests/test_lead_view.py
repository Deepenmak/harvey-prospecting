"""A lead's whole story has to read in one place.

The Contacts tab used to show name, company, email, score and status. That is
five of the thirty-eight things the pipeline actually collects about a lead:
the reviewer's fit and why, the researcher's intent and buying state, the
signals, the red flags, the fact check, the angle, the first-touch draft.
Those lived only in exports/contacts_all.csv, so the one screen a person
reads said less than the file beside it.

These tests pin the join that fixes it: Apollo's record and the pipeline,
matched on the address, with neither side able to hide the other. Nothing
here calls Apollo and nothing spends a credit.
"""

import asyncio

import pytest

from sellbird import fetch, lead_view
from sellbird.lead_view import LeadStore
from sellbird.models.prospect import Prospect


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
        "domain": f"{apollo_id}.example.com", "employees": "900",
        "industry": "insurance", "city": "Columbus", "state": "Ohio",
        "country": "United States", "score": 70, "status": "new",
    })
    row.update(over)
    return row


@pytest.fixture
def store(tmp_path, monkeypatch):
    exports = tmp_path / "exports"
    exports.mkdir()
    monkeypatch.setattr(fetch, "CONTACTS_FILE", exports / "contacts_all.csv")
    monkeypatch.setattr(fetch, "EMAIL_CONTACTS_FILE", exports / "contacts_with_email.csv")
    monkeypatch.setattr(fetch, "LOCAL_LEDGER", tmp_path / "fetch_ledger.json")
    s = LeadStore(db_path=str(tmp_path / "sellbird.db"))
    run(s.init_db())
    return s


def add_prospect(store, **over):
    p = Prospect(first_name="Dana", last_name="Fitch", title="Chief Claims Officer",
                 email="a1@example.com", source="apollo", status="contacted",
                 company="Example a1 Mutual", score=55)
    for key, value in over.items():
        setattr(p, key, value)
    return run(store.add_prospect(p))


# ── The join ─────────────────────────────────────────────────────────


def test_the_judgement_a_skill_wrote_reaches_the_tab(store):
    """The reviewer's fit and the research used to be invisible in the UI.

    They were written into contacts_all.csv by the lead-gen skills and the
    dashboard read none of it, so the one screen a person looks at could not
    say why a lead was worth anything.
    """
    run(store.upsert_contacts("", [contact(
        "a1", fit="a", fit_reason="Claims volume and no in-house ML team",
        intent="high", buying_state="open", signals="Hiring two claims analysts",
        red_flags="", verified="pass", verify_notes="Both sources opened",
        angle="Their claims backlog is public", draft_subject="Your claims backlog",
        draft_body="Noticed the backlog...")]))
    add_prospect(store)

    row = run(store.leads())[0]
    assert row["fit"] == "a" and row["fit_label"] == "Strong"
    assert row["fit_reason"].startswith("Claims volume")
    assert row["intent_label"] == "High" and row["intent_tone"] == "good"
    assert row["check_label"] == "Held up"
    assert row["buying_label"] == "Open need"
    assert row["angle"] == "Their claims backlog is public"
    assert row["draft_body"].startswith("Noticed")
    # And the pipeline side of the same person.
    assert row["in_pipeline"] is True and row["status"] == "contacted"


def test_a_fetched_contact_never_promoted_is_still_shown(store):
    """A contact that cost a credit and was never promoted is the one most
    worth seeing: it is paid-for work sitting idle. The old tab read
    `prospects` alone, so it was invisible."""
    run(store.upsert_contacts("", [contact("a1"), contact("a2")]))
    add_prospect(store)      # only a1 is in the pipeline

    rows = run(store.leads())
    by_id = {r["apollo_id"]: r for r in rows}
    assert by_id["a2"]["in_pipeline"] is False
    assert by_id["a2"]["status"] == lead_view.NOT_IN_PIPELINE
    assert by_id["a2"]["id"] == ""           # no prospect row to act on
    assert lead_view.summary(rows)["candidates"] == 1


def test_a_contact_from_somewhere_else_is_not_dropped(store):
    """Someone the web Scout found, or an import brought in, has no Apollo
    row at all. Joining on Apollo's record must not lose them."""
    add_prospect(store, email="scout@example.com", source="web",
                 first_name="Lee", last_name="Marsh")

    rows = run(store.leads())
    assert [r["email"] for r in rows] == ["scout@example.com"]
    assert rows[0]["apollo_id"] == ""
    assert rows[0]["fit_label"] == "Unreviewed"
    assert rows[0]["in_pipeline"] is True


def test_the_two_sides_are_matched_whatever_the_case(store):
    """Apollo returns mixed case; prospects are stored lowercased. Matching
    on the raw string would show the same person twice."""
    run(store.upsert_contacts("", [contact("a1", email="Dana.Fitch@Example.com")]))
    add_prospect(store, email="dana.fitch@example.com")

    rows = run(store.leads())
    assert len(rows) == 1
    assert rows[0]["in_pipeline"] is True and rows[0]["apollo_id"] == "a1"


def test_the_pipeline_fills_what_apollo_left_blank(store):
    """A row enriched later, or imported with a title Apollo never had,
    should read as one person rather than half of two."""
    run(store.upsert_contacts("", [contact("a1", title="", industry="",
                                           linkedin_url="", employees="")]))
    add_prospect(store, title="VP Claims", industry="insurance carriers",
                 linkedin_url="https://example.com/in/dana", company_size="1200")

    row = run(store.leads())[0]
    assert row["title"] == "VP Claims"
    assert row["industry"] == "insurance carriers"
    assert row["linkedin_url"].endswith("/in/dana")
    assert row["employees"] == "1200"


def test_apollos_answer_wins_where_it_has_one(store):
    """Apollo's own fields are the fetched truth; the pipeline's copy can be
    an older guess."""
    run(store.upsert_contacts("", [contact("a1", title="Chief Claims Officer",
                                           email_status="verified")]))
    add_prospect(store, title="Claims person", email_status="guess")

    row = run(store.leads())[0]
    assert row["title"] == "Chief Claims Officer"
    assert row["email_status"] == "verified"


# ── Order, labels and the counts above the table ─────────────────────


def test_the_best_lead_is_first(store):
    run(store.upsert_contacts("", [
        contact("low", fit="skip", score=90),
        contact("mid", fit="b", intent="medium", score=50),
        contact("top", fit="a", intent="high", verified="pass", score=40),
        contact("new", score=95),
    ]))
    assert [r["apollo_id"] for r in run(store.leads())] == ["top", "mid", "new", "low"]


def test_intent_is_never_read_higher_than_the_buying_state_allows(store):
    """A company that already solved the workflow is not a hot lead however
    much AI news it has. fetch caps it; the tab must show the capped value."""
    run(store.upsert_contacts("", [contact("a1", intent="high",
                                           buying_state="solved")]))
    row = run(store.leads())[0]
    assert row["intent"] == "low" and row["intent_label"] == "Low"


def test_an_unreviewed_lead_says_so_rather_than_reading_as_a_skip(store):
    run(store.upsert_contacts("", [contact("a1", fit="", intent="")]))
    row = run(store.leads())[0]
    assert row["fit_label"] == "Unreviewed"
    assert row["intent_label"] == "Not researched"
    assert row["fit_tone"] == "idle" and row["intent_tone"] == "idle"


def test_every_figure_above_the_table_counts_the_rows_below_it(store):
    run(store.upsert_contacts("", [
        contact("a1", fit="a", intent="high", verified="pass", draft_body="Hi"),
        contact("a2", fit="b", intent="medium", email_status="risky"),
        contact("a3", fit="skip", intent="low", email=""),
    ]))
    add_prospect(store)
    rows = run(store.leads())
    s = lead_view.summary(rows, credits_used=3)
    assert s == {
        "contacts": 3, "with_email": 2, "verified": 1, "strong": 1,
        "ruled_out": 1, "researched": 3, "intent": 2, "checked": 1,
        "drafted": 1, "in_pipeline": 1, "candidates": 2, "credits_used": 3,
    }


def test_the_credits_figure_is_the_record_not_a_guess(store):
    run(store.record_run({"segment": "us_insurance", "mode": "fetch",
                          "credits_used": 7}))
    run(store.upsert_contacts("", [contact("a1")]))
    assert run(store.credits_used()) == 7


def test_segments_report_what_each_one_actually_returned(store):
    run(store.upsert_contacts("", [
        contact("a1", segment="us_insurance", fit="a", intent="high"),
        contact("a2", segment="us_insurance", fit="skip"),
        contact("a3", segment="us_rcm", fit="a", email=""),
    ]))
    spec = {"segments": [{"key": "us_insurance", "label": "US insurance"},
                         {"key": "us_rcm", "label": "US healthcare RCM"},
                         {"key": "ca_insurance", "label": "Canada insurance"}]}
    rows = run(store.leads(lead_view.segment_labels(spec)))
    out = {s["key"]: s for s in lead_view.segments(rows, spec)}
    assert out["us_insurance"] == {"key": "us_insurance", "label": "US insurance",
                                   "contacts": 2, "with_email": 2, "strong": 1,
                                   "intent": 1}
    assert out["us_rcm"]["with_email"] == 0 and out["us_rcm"]["strong"] == 1
    # A segment nothing has come from yet is listed, at zero, not hidden.
    assert out["ca_insurance"]["contacts"] == 0
    assert rows[0]["segment_label"] in ("US insurance", "US healthcare RCM")


def test_a_segment_with_no_entry_in_the_spec_still_appears(store):
    """segments.yaml can be edited after a fetch. A contact must not vanish
    because the segment it came from was renamed."""
    run(store.upsert_contacts("", [contact("a1", segment="retired_segment")]))
    rows = run(store.leads(lead_view.segment_labels({})))
    assert rows[0]["segment_label"] == "retired_segment"
    assert [s["key"] for s in lead_view.segments(rows, {})] == ["retired_segment"]


# ── Where each lead stands ───────────────────────────────────────────


def test_the_conversation_and_the_outbox_show_on_the_row(store):
    pid = add_prospect(store)
    run(store.upsert_contacts("", [contact("a1")]))
    async def write():
        async with store._connect() as db:
            await db.execute(
                "INSERT INTO conversations (id, prospect_id, stage, status,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("c1", pid, "qualifying", "open", "2026-01-02", "2026-01-02"))
            await db.execute(
                "INSERT INTO outbox (id, prospect_id, to_email, subject, body,"
                " status, send_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("o1", pid, "a1@example.com", "s", "b", "sent",
                 "2026-01-01 09:00", "2026-01-01 09:05"))
            await db.execute(
                "INSERT INTO outbox (id, prospect_id, to_email, subject, body,"
                " status, send_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("o2", pid, "a1@example.com", "s2", "b2", "approved",
                 "2026-01-04 09:00"))
            await db.commit()
    run(write())

    row = run(store.leads())[0]
    assert row["stage"] == "qualifying"
    assert row["conversation_status"] == "open"
    assert row["last_touch"].startswith("2026-01-01")
    assert row["next_send"].startswith("2026-01-04")
    assert row["sent_count"] == 1


def test_the_newest_conversation_is_the_one_shown(store):
    pid = add_prospect(store)
    run(store.upsert_contacts("", [contact("a1")]))
    async def write():
        async with store._connect() as db:
            for cid, stage, when in (("c1", "engaged", "2026-01-02"),
                                     ("c2", "negotiating", "2026-01-09")):
                await db.execute(
                    "INSERT INTO conversations (id, prospect_id, stage, status,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, pid, stage, "open", when, when))
            await db.commit()
    run(write())
    assert run(store.leads())[0]["stage"] == "negotiating"


def test_a_lead_with_no_history_says_nothing_rather_than_guessing(store):
    run(store.upsert_contacts("", [contact("a1")]))
    add_prospect(store)
    row = run(store.leads())[0]
    assert row["stage"] == "" and row["last_touch"] == "" and row["next_send"] == ""
    assert row["sent_count"] == 0


def test_an_empty_workspace_is_an_empty_list_not_a_failure(store):
    rows = run(store.leads())
    assert rows == []
    assert lead_view.summary(rows)["contacts"] == 0
    assert lead_view.segments(rows, {}) == []

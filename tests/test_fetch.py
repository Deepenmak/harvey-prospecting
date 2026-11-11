"""sellbird fetch: candidate choice and segment rotation (no network)."""

import asyncio
import csv
import importlib.util
import json
import sys

from sellbird.fetch import (FileLedger, account_tier, capped_intent, choose_candidates,
                          domain_of, email_status, interleave, merge_contacts,
                          pick_segment, plan_picks, read_contacts, relevant_roles,
                          save_contacts, score, title_priority, update_contacts)

TITLES = ["Head of AI", "VP Claims", "Chief Technology Officer"]


def person(pid, title, org, has_email=True):
    return {"id": pid, "title": title, "has_email": has_email,
            "organization": {"name": org}}


def test_title_priority_follows_segment_order():
    assert title_priority("SVP, Head of AI", TITLES) == 0
    assert title_priority("VP of Claims", TITLES) == 1  # words, not exact phrase
    assert title_priority("Office Manager", TITLES) == len(TITLES)


def test_one_person_per_company_best_title_first():
    people = [
        person("1", "Chief Technology Officer", "Example Mutual"),
        person("2", "Head of AI", "Example Mutual"),
        person("3", "VP Claims", "Sample Insurance"),
        person("4", "Head of AI", "No Email Co", has_email=False),
    ]
    picks = choose_candidates(people, TITLES, set(), set(), limit=10)
    assert [p["id"] for p in picks] == ["2", "3"]


def test_seen_people_and_companies_are_skipped():
    people = [person("1", "Head of AI", "Example Mutual"),
              person("2", "VP Claims", "Sample Insurance")]
    picks = choose_candidates(people, TITLES, {"1"}, {"sample insurance"}, limit=10)
    assert picks == []


def test_auto_rotates_after_last_segment():
    spec = {"segments": [{"key": "a"}, {"key": "b"}]}
    assert pick_segment(spec, "auto", "")["key"] == "a"
    assert pick_segment(spec, "auto", "a")["key"] == "b"
    assert pick_segment(spec, "auto", "b")["key"] == "a"
    assert pick_segment(spec, "b", "b")["key"] == "b"


def test_title_priority_matches_whole_words_in_order():
    # "ai" sits inside "claims", "of" inside "office": neither counts.
    assert title_priority("Head of Claims", TITLES) == len(TITLES)
    assert title_priority("Head Office Claims Technical Advisor", ["Head of Claims"]) == 1
    assert title_priority("Head of Retail", ["Head of AI"]) == 1
    # Spellings of the same title fold together.
    assert title_priority("SVP, Claims", TITLES) == 1
    assert title_priority("Executive Vice President of Claims", TITLES) == 1
    assert title_priority("CTO & Co-founder", TITLES) == 2
    assert title_priority("CIO", ["Chief Information Officer"]) == 0
    assert title_priority("Head of Artificial Intelligence", TITLES) == 0


def test_interleave_shares_in_turn():
    assert interleave([[1, 2, 3], ["a"], ["x", "y"]]) == [1, "a", "x", 2, "y", 3]
    assert interleave([]) == []


def test_plan_picks_keeps_reviewed_rows_and_collects_skips():
    rows = [
        {"segment": "b", "apollo_id": "1", "company": "Example Mutual", "verdict": "A"},
        {"segment": "a", "apollo_id": "7", "company": "Sample Lender", "verdict": "b"},
        {"segment": "a", "apollo_id": "2", "company": "Sample Bank", "verdict": "a"},
        {"segment": "a", "apollo_id": "3", "company": "Example Media", "verdict": "skip"},
        {"segment": "a", "apollo_id": "4", "company": "Unreviewed Co", "verdict": ""},
        {"segment": "a", "apollo_id": "5", "company": "Fetched Co", "verdict": "a"},
        {"segment": "z", "apollo_id": "6", "company": "Gone Segment Co", "verdict": "a"},
    ]
    groups, skipped = plan_picks(rows, ["a", "b"], set(), {"fetched co"})
    # Strong fits ("a") get credits before fits ("b").
    assert [[r["apollo_id"] for r in g] for g in groups] == [["2", "7"], ["1"]]
    assert skipped == ["example media"]


def test_skipped_orgs_are_remembered(tmp_path):
    ledger = FileLedger(tmp_path / "ledger.json")
    run = {"segment": "a", "credits_used": 1, "seen_ids": ["1"],
           "skipped_orgs": ["example media"]}
    asyncio.run(ledger.record(run, [{"company": "Example Mutual"}]))
    state = asyncio.run(ledger.load())
    assert state.seen_orgs == {"example mutual", "example media"}
    assert state.credits_used == 1


def test_only_icp_hiring_roles_count():
    texts = ["How to Apply", "Billing Operations Representative I",
             "Senior Machine Learning Engineer · Data · Remote · posted 2026-10-01"]
    keywords = ["machine learning engineer", "ai engineer"]
    assert relevant_roles(texts, keywords) == [texts[2]]
    assert relevant_roles(texts, []) == texts


def test_score_spreads_by_size_and_verdict():
    seg = {"titles": ["VP Claims"]}
    row = {"title": "VP Claims", "email_status": "verified", "hiring_roles": "",
           "employees": "400"}
    assert score(row, seg, "a") - score(row, seg, "b") == 10
    big = dict(row, employees="4500")
    assert score(row, seg) - score(big, seg) == 5


def contact(pid, email="", score="80", **kw):
    return dict({"apollo_id": pid, "company": f"Example {pid}", "email": email,
                 "score": score}, **kw)


def test_contacts_merge_by_id_and_never_erase():
    old = [contact("1", "a@example.com", angle="Kept angle", fit="a")]
    new = [contact("1", "a@example.com", score="90"), contact("2")]
    rows = {r["apollo_id"]: r for r in merge_contacts(old, new)}
    assert set(rows) == {"1", "2"}
    assert rows["1"]["score"] == "90"
    assert rows["1"]["angle"] == "Kept angle"  # an empty update never erases
    # Updates for unknown contacts are dropped unless asked to add them.
    assert [r["apollo_id"] for r in merge_contacts(old, new, add_new=False)] == ["1"]


def test_contact_files_rank_best_first_and_split_on_email(tmp_path):
    all_path, email_path = tmp_path / "all.csv", tmp_path / "email.csv"
    save_contacts([contact("1", "x@example.com", fit="b"),
                   contact("2", fit="a"),
                   contact("3", "y@example.com", fit="a", intent="high")],
                  all_path, email_path)
    assert [r["apollo_id"] for r in read_contacts(all_path)] == ["3", "2", "1"]
    with open(email_path, newline="", encoding="utf-8") as f:
        assert [r["apollo_id"] for r in csv.DictReader(f)] == ["3", "1"]
    # A second run updates the same two files in place.
    n = update_contacts([{"apollo_id": "1", "intent": "high", "fit": "a", "score": "95"},
                         {"apollo_id": "9", "intent": "high"}],
                        path=all_path, email_path=email_path)
    assert n == 1
    assert [r["apollo_id"] for r in read_contacts(all_path)] == ["1", "3", "2"]


def test_buying_state_caps_intent():
    assert capped_intent("high", "solved") == "low"
    assert capped_intent("high", "builds_own") == "low"
    assert capped_intent("medium", "SOLVED") == "low"
    assert capped_intent("high", "partly_solved") == "medium"
    assert capped_intent("low", "partly_solved") == "low"
    assert capped_intent("high", "open") == "high"
    assert capped_intent("high", "unknown") == "high"
    assert capped_intent("high", "") == "high"
    assert capped_intent("unknown", "solved") == "unknown"


def test_tier_a_needs_fit_intent_open_need_and_checked_facts():
    good = {"fit": "a", "intent": "high", "buying_state": "open", "verified": "pass"}
    assert account_tier(good) == "A"
    assert account_tier({**good, "verified": "partial"}) == "A"
    for k, v in (("fit", "b"), ("intent", "medium"), ("buying_state", "solved"),
                 ("verified", "fail"), ("verified", "")):
        assert account_tier({**good, k: v}) == ""


def test_saved_tier_follows_the_row_and_old_files_still_load(tmp_path):
    all_path, email_path = tmp_path / "all.csv", tmp_path / "email.csv"
    # A file written before the new columns existed.
    all_path.write_text("apollo_id,fit,intent,verified\n1,a,high,pass\n", encoding="utf-8")
    update_contacts([{"apollo_id": "1", "buying_state": "open", "call_brief": "Line one\nLine two"}],
                    path=all_path, email_path=email_path)
    row = read_contacts(all_path)[0]
    assert row["account_tier"] == "A" and row["call_brief"] == "Line one\nLine two"
    # The tier is derived, so it drops when the facts stop holding.
    update_contacts([{"apollo_id": "1", "verified": "fail"}], path=all_path, email_path=email_path)
    assert read_contacts(all_path)[0]["account_tier"] == ""


def _load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_apply_review_caps_intent_and_folds_briefs_and_deals(tmp_path, monkeypatch):
    mod = _load_script(".claude/skills/lead-gen/scripts/apply_review.py", "apply_review")
    research = tmp_path / "research.json"
    research.write_text(json.dumps([{"apollo_id": "1", "intent": "high", "buying_state": "solved"}]))
    briefs = tmp_path / "briefs.json"
    briefs.write_text(json.dumps([{"apollo_id": "2", "brief": "A brief"}]))
    deals = tmp_path / "deals.json"
    deals.write_text(json.dumps([{"apollo_id": "3", "call_notes": "Notes", "proposal": "Draft",
                                  "deal_check": ["known: budget", "missing: approval path"]}]))
    got = {}
    monkeypatch.setattr(mod, "WORK", tmp_path / "none")
    monkeypatch.setattr(mod, "update_contacts", lambda ups: got.update({u["apollo_id"]: u for u in ups}) or len(ups))
    monkeypatch.setattr(mod, "read_contacts", lambda: [])
    monkeypatch.setattr(sys, "argv", ["apply_review.py", "--research", str(research),
                                      "--briefs", str(briefs), "--deals", str(deals)])
    mod.main()
    assert got["1"]["intent"] == "low" and got["1"]["buying_state"] == "solved"
    assert got["2"]["call_brief"] == "A brief"
    assert got["3"]["call_notes"] == "Notes" and got["3"]["proposal_draft"] == "Draft"
    assert got["3"]["deal_check"] == "known: budget | missing: approval path"


def test_weekly_stats_count_per_segment():
    mod = _load_script(".claude/skills/weekly-review/scripts/weekly_stats.py", "weekly_stats")
    stats = mod.segment_stats([
        {"segment": "s1", "fit": "a", "intent": "high", "buying_state": "open", "account_tier": "A"},
        {"segment": "s1", "fit": "b", "intent": "low", "buying_state": "solved", "verified": "fail"},
        {"segment": "s2"},
    ])
    assert stats["s1"] == {"leads": 2, "fit_a": 1, "high_intent": 1, "open_need": 1,
                           "tier_a": 1, "check_failed": 1}
    assert stats["s2"]["leads"] == 1 and stats["s2"]["fit_a"] == 0


def test_cloud_run_refuses_without_a_restored_ledger(tmp_path, monkeypatch):
    import sellbird.fetch as fetch

    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(fetch, "make_ledger",
                        lambda: fetch.FileLedger(tmp_path / "missing.json"))
    report = asyncio.run(fetch.run_fetch(None, None, segment="mix", credits=5))
    assert report.stopped.startswith("no ledger")
    assert report.credits_used == 0


def test_helpers():
    assert domain_of("https://www.example.com/about") == "example.com"
    assert email_status("verified") == "verified"
    assert email_status("extrapolated") == "guess"

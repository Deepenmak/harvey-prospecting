"""harvey fetch: candidate choice and segment rotation (no network)."""

from harvey.fetch import (choose_candidates, domain_of, email_status,
                          pick_segment, title_priority)

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


def test_helpers():
    assert domain_of("https://www.example.com/about") == "example.com"
    assert email_status("verified") == "verified"
    assert email_status("extrapolated") == "guess"

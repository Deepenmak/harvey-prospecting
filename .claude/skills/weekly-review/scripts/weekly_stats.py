"""Print lead quality per segment, and credits used, from the contact file and
the ledger. Reads only; nothing is written and nothing is spent.

Usage: weekly_stats.py
"""

import json
from collections import Counter

from harvey.fetch import LOCAL_LEDGER, load_segments, read_contacts


def segment_stats(rows: list[dict]) -> dict[str, dict]:
    """Counts per segment: leads, strong fits, high intent, open need, tier A
    accounts, and fact checks that failed."""
    stats: dict[str, Counter] = {}
    for r in rows:
        c = stats.setdefault(r.get("segment") or "unknown", Counter())
        c["leads"] += 1
        c["fit_a"] += r.get("fit") == "a"
        c["high_intent"] += r.get("intent") == "high"
        c["open_need"] += r.get("buying_state") == "open"
        c["tier_a"] += r.get("account_tier") == "A"
        c["check_failed"] += r.get("verified") == "fail"
    return {k: dict(v) for k, v in sorted(stats.items())}


def main() -> None:
    rows = read_contacts()
    print(f"{'segment':<16}{'leads':>6}{'fit a':>7}{'high':>6}{'open':>6}{'tier A':>8}{'failed':>8}")
    for seg, c in segment_stats(rows).items():
        print(f"{seg:<16}{c['leads']:>6}{c['fit_a']:>7}{c['high_intent']:>6}"
              f"{c['open_need']:>6}{c['tier_a']:>8}{c['check_failed']:>8}")
    ledger = json.loads(LOCAL_LEDGER.read_text(encoding="utf-8")) if LOCAL_LEDGER.exists() else {}
    spec = load_segments()
    used = sum(r.get("credits_used", 0) for r in ledger.get("runs", []))
    per_run, session_max = spec.get("per_run_credits", 0), spec.get("max_credits_per_session", 0)
    print(f"\nCredits: {used} used in total, {per_run} per run by default, "
          f"{session_max} max per session.")
    print(f"Companies skipped so far: {len(ledger.get('skipped_orgs', []))}")
    print("Replies: none tracked yet (sending hasn't started).")


if __name__ == "__main__":
    main()

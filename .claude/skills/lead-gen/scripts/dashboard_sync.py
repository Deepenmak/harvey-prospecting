"""Move Harvey's contacts and ledger to and from the leads dashboard.

The dashboard (a private claude.ai artifact) is the lasting record: a cloud
run starts from a fresh clone, so it restores from the dashboard before
fetching and exports back after.

Usage:
  dashboard_sync.py export <dir>   write <dir>/contacts.json and <dir>/runs.json
  dashboard_sync.py restore <dir>  rebuild data/fetch_ledger.json and the two
                                   contact files from those two files

contacts.json is every contact, best first. runs.json is the ledger's runs
(credits, segment, the people enriched, the companies skipped), from which
the rest of the ledger is rebuilt.
"""

import json
import sys
from pathlib import Path

from harvey.fetch import (CONTACT_COLUMNS, LOCAL_LEDGER, merge_contacts,
                          read_contacts, save_contacts)

NUMERIC = ("score", "employees", "open_roles")


def _num(v: str):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def export(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    contacts = [{k: (_num(r.get(k, "")) if k in NUMERIC else r.get(k, ""))
                 for k in CONTACT_COLUMNS} for r in read_contacts()]
    runs = []
    if LOCAL_LEDGER.exists():
        runs = json.loads(LOCAL_LEDGER.read_text(encoding="utf-8")).get("runs", [])
    (out / "contacts.json").write_text(json.dumps(contacts, indent=1), encoding="utf-8")
    (out / "runs.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")
    credits = sum(r.get("credits_used", 0) for r in runs)
    print(f"Exported {len(contacts)} contacts and {len(runs)} runs "
          f"({credits} credits) to {out}")


def restore(src: Path) -> None:
    contacts = json.loads((src / "contacts.json").read_text(encoding="utf-8"))
    runs = json.loads((src / "runs.json").read_text(encoding="utf-8"))
    rows = [{k: "" if r.get(k) is None else str(r.get(k, "")) for k in CONTACT_COLUMNS}
            for r in contacts]
    # Merge into whatever is here, so a restore never loses local rows.
    save_contacts(merge_contacts(read_contacts(), rows))

    seen_ids = {r["apollo_id"] for r in rows if r["apollo_id"]}
    skipped = set()
    for run in runs:
        seen_ids |= set(run.get("seen_ids") or [])
        skipped |= set(run.get("skipped_orgs") or [])
    ledger = {
        "runs": runs,
        "seen_ids": sorted(seen_ids),
        "seen_orgs": sorted({r["company"].lower() for r in rows if r["company"]}),
        "skipped_orgs": sorted(skipped),
    }
    LOCAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    credits = sum(r.get("credits_used", 0) for r in runs)
    print(f"Restored {len(rows)} contacts and a ledger of {len(runs)} runs "
          f"({credits} credits used, {len(skipped)} companies skipped)")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "restore"):
        print(__doc__)
        sys.exit(2)
    (export if sys.argv[1] == "export" else restore)(Path(sys.argv[2]))

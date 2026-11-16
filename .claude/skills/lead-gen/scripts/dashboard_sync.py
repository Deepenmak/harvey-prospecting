"""Move Sellbird's contacts and ledger to and from the leads dashboard.

The dashboard (a private claude.ai artifact) is the lasting record: a cloud
run starts from a fresh clone, so it restores from the dashboard before
fetching and exports back after.

Usage:
  dashboard_sync.py export <dir>   write <dir>/contacts.json and <dir>/runs.json
  dashboard_sync.py restore <dir>  rebuild data/fetch_ledger.json and the two
                                   contact files from those two files

A restore takes the files as the dashboard hands them over: when <dir> has no
contacts.json or runs.json, every *.json in it is sorted out by shape, so an
asset saved under its blob id (0c1f....json) needs no copy and no rename. That
matters in a cloud run: nothing has to pass the file's contents through the
conversation, which costs the whole list of contacts every run.

contacts.json is every contact, best first. runs.json is the ledger's runs
(credits, segment, the people enriched, the companies skipped), from which
the rest of the ledger is rebuilt.
"""

import json
import sys
from pathlib import Path

from sellbird.fetch import (CONTACT_COLUMNS, LOCAL_LEDGER, merge_contacts,
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
    # Minified: this file is read by scripts and by the dashboard page, and a
    # cloud run pays for every byte it has to carry.
    (out / "contacts.json").write_text(
        json.dumps(contacts, separators=(",", ":")), encoding="utf-8")
    (out / "runs.json").write_text(json.dumps(runs, indent=1), encoding="utf-8")
    credits = sum(r.get("credits_used", 0) for r in runs)
    print(f"Exported {len(contacts)} contacts and {len(runs)} runs "
          f"({credits} credits) to {out}")


def _classify(src: Path) -> tuple[list, list]:
    """Find the contacts and the runs in ``src``, whatever the files are named.

    A dashboard asset saved by the Artifact tool is named after its blob id,
    so the two files are told apart by what they hold: contacts carry an
    ``apollo_id``, a run carries ``credits_used`` or ``ran_at``.
    """
    named = {}
    for kind, name in (("contacts", "contacts.json"), ("runs", "runs.json")):
        if (src / name).exists():
            named[kind] = json.loads((src / name).read_text(encoding="utf-8"))
    if len(named) == 2:
        return named["contacts"], named["runs"]

    unknown = []
    for path in sorted(src.glob("*.json")):
        if path.name in ("contacts.json", "runs.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, list):
            continue
        keys = data[0].keys() if data and isinstance(data[0], dict) else ()
        kind = ("contacts" if "apollo_id" in keys
                else "runs" if {"credits_used", "ran_at"} & set(keys) else "")
        # A file named contacts.json or runs.json wins over a blob-id file.
        if kind and kind not in named:
            named[kind] = data
        elif not kind:
            # An empty list has no shape to go on: it can still be placed if
            # it is the only file left and only one kind is missing (the first
            # run of a phase has no runs yet).
            unknown.append(data)

    missing = [k for k in ("contacts", "runs") if k not in named]
    if len(missing) == 1 and len(unknown) == 1:
        named[missing[0]] = unknown[0]
        missing = []
    if missing:
        raise SystemExit(
            f"No {' or '.join(missing)} in {src}: expected {src}/contacts.json and "
            f"{src}/runs.json, or the dashboard's two JSON assets saved there.")
    return named["contacts"], named["runs"]


def restore(src: Path) -> None:
    contacts, runs = _classify(src)
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
    # ROUTINE.md step 0 runs --help to prove the ledger can be saved later, so
    # help is a success; a wrong command is still an error.
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "restore"):
        print(__doc__)
        sys.exit(2)
    (export if sys.argv[1] == "export" else restore)(Path(sys.argv[2]))

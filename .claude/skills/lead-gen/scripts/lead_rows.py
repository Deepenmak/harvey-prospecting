"""Compact lead payloads for subagents, without reading a whole contact file.

exports/contacts_all.csv holds every contact ever fetched, so it grows with
every run. Reading it into the conversation to pull out this run's ten leads
is the biggest avoidable cost in a scheduled run: it spends the whole list,
every run, and the list only gets longer. This prints just the rows a phase
needs, with just the fields that phase's agent is given.

Usage:
  lead_rows.py --ids <id,id,...> [--for research|writer|call] [--limit N]
  lead_rows.py --new [--for ...]          this run's contacts, from the ledger
  lead_rows.py --ids <...> --fields a,b   an explicit field list

One minified JSON array goes to stdout and nothing else, so the output can be
handed straight to a subagent. Unknown ids are reported on stderr.
"""

import argparse
import json
import sys
from pathlib import Path

from sellbird.fetch import LOCAL_LEDGER, read_contacts

# What each agent is given. Researchers get the company, not the person: no
# email, no LinkedIn URL (.claude/agents/lead-researcher.md).
PRESETS = {
    "research": ["apollo_id", "company", "domain", "title", "segment", "industry",
                 "employees", "city", "country", "fit", "fit_reason"],
    "writer": ["apollo_id", "first_name", "title", "company", "segment",
               "fit", "fit_reason", "angle"],
    "call": ["apollo_id", "first_name", "last_name", "title", "company", "domain",
             "segment", "industry", "employees", "city", "country", "fit",
             "fit_reason", "intent", "buying_state", "signals", "red_flags",
             "verified", "angle"],
}


def new_ids() -> list[str]:
    """The ids the most recent ledger run touched."""
    if not LOCAL_LEDGER.exists():
        return []
    runs = json.loads(LOCAL_LEDGER.read_text(encoding="utf-8")).get("runs", [])
    return list(runs[-1].get("seen_ids") or []) if runs else []


def rows_for(ids: list[str], fields: list[str], limit: int = 0) -> tuple[list[dict], list[str]]:
    wanted = [i.strip() for i in ids if i.strip()]
    by_id = {r["apollo_id"]: r for r in read_contacts()}
    # Keep the contact file's own order (best first), not the order asked for.
    picked = [by_id[i] for i in by_id if i in set(wanted)]
    if limit:
        picked = picked[:limit]
    missing = [i for i in wanted if i not in by_id]
    return [{k: r.get(k, "") for k in fields} for r in picked], missing


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    ap.add_argument("--ids", default="", help="comma-separated apollo_ids")
    ap.add_argument("--new", action="store_true",
                    help="use the ids of the most recent ledger run")
    ap.add_argument("--for", dest="preset", default="research", choices=sorted(PRESETS),
                    help="which agent the rows are for (default: research)")
    ap.add_argument("--fields", default="", help="explicit comma-separated field list")
    ap.add_argument("--limit", type=int, default=0, help="keep at most N rows")
    args = ap.parse_args()

    ids = new_ids() if args.new else args.ids.split(",")
    if not [i for i in ids if i.strip()]:
        print("No ids: pass --ids or --new.", file=sys.stderr)
        return 2
    fields = ([f.strip() for f in args.fields.split(",") if f.strip()]
              if args.fields else PRESETS[args.preset])
    rows, missing = rows_for(ids, fields, args.limit)
    if missing:
        print(f"Not in {Path('exports/contacts_all.csv')}: {', '.join(missing)}",
              file=sys.stderr)
    # Minified on purpose: this output is read by a model, not a human.
    print(json.dumps(rows, separators=(",", ":"), ensure_ascii=False))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())

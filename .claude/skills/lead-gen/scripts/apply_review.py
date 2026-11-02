"""Fold research, fact checks and drafts into the two contact files
(exports/contacts_all.csv and exports/contacts_with_email.csv), which
stay ranked best first. Nothing new is written per run.

Usage: apply_review.py [--research F] [--verify F] [--drafts F] [--briefs F] [--deals F]

Each defaults to exports/work/<name>.json when that file exists. Shapes are
in ../reference.md. A value that's empty in the JSON never erases a stored one.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from harvey.fetch import EXPORT_DIR, capped_intent, read_contacts, update_contacts

WORK = EXPORT_DIR / "work"


def _load(path: str, name: str) -> list[dict]:
    p = Path(path) if path else WORK / f"{name}.json"
    if not p.exists():
        if path:
            raise SystemExit(f"Not found: {p}")
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _joined(items: list[str]) -> str:
    return " | ".join(i for i in items if i)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--research", default="")
    ap.add_argument("--verify", default="")
    ap.add_argument("--drafts", default="")
    ap.add_argument("--briefs", default="")
    ap.add_argument("--deals", default="")
    a = ap.parse_args()

    updates: dict[str, dict] = {}

    def up(pid: str) -> dict:
        return updates.setdefault(pid, {"apollo_id": pid})

    for e in _load(a.research, "research"):
        u = up(e["apollo_id"])
        u["buying_state"] = e.get("buying_state", "")
        u["intent"] = capped_intent(e.get("intent", ""), u["buying_state"])
        u["signals"] = _joined(
            [f"{s.get('fact', '')} ({s.get('url', '')}{', ' + s['date'] if s.get('date') else ''})"
             for s in e.get("signals", [])]
            + [f"Hiring: {h.get('role', '')} ({h.get('url', '')})" for h in e.get("hiring", [])])
        u["red_flags"] = _joined(e.get("red_flags", []))
        u["angle"] = e.get("angle", "")
    for e in _load(a.verify, "verify"):
        u = up(e["apollo_id"])
        u["verified"], u["verify_notes"] = e.get("status", ""), e.get("notes", "")
    for e in _load(a.drafts, "drafts"):
        u = up(e["apollo_id"])
        u["draft_subject"], u["draft_body"] = e.get("subject", ""), e.get("body", "")
    for e in _load(a.briefs, "briefs"):
        up(e["apollo_id"])["call_brief"] = e.get("brief", "")
    for e in _load(a.deals, "deals"):
        u = up(e["apollo_id"])
        u["call_notes"] = e.get("call_notes", "")
        u["deal_check"] = _joined(e.get("deal_check", []))
        u["proposal_draft"] = e.get("proposal", "")

    if not updates:
        print("Nothing to apply.")
        return
    matched = update_contacts(list(updates.values()))
    rows = read_contacts()
    intents = Counter(r.get("intent") or "unresearched" for r in rows)
    print(f"Updated {matched} of {len(updates)} contact(s); "
          f"{len(rows)} contacts, {sum(1 for r in rows if r.get('email'))} with email. "
          f"Intent: {', '.join(f'{k} {v}' for k, v in intents.items())}")
    if matched < len(updates):
        print("Some ids weren't in exports/contacts_all.csv: check the apollo_ids.")


if __name__ == "__main__":
    main()

"""Write vetter verdicts into a fetch plan CSV.

Usage: apply_verdicts.py [verdicts.json] [plan.csv]

Defaults: exports/work/verdicts.json and exports/plan.csv.

verdicts.json is a list of {"apollo_id", "verdict", "reason"}, verdict one of
a, b, skip. Rows without a verdict are left blank (harvey fetch --from-plan
ignores them) and listed, so nothing is silently dropped.
"""

import csv
import json
import sys
from collections import Counter

from harvey.fetch import EXPORT_DIR, PLAN_FILE

VERDICTS = {"a", "b", "skip"}


def main(verdicts_path: str, plan_path: str) -> int:
    with open(verdicts_path, encoding="utf-8") as f:
        verdicts = {v["apollo_id"]: v for v in json.load(f)}
    bad = sorted(k for k, v in verdicts.items()
                 if str(v.get("verdict", "")).strip().lower() not in VERDICTS)
    if bad:
        print(f"Invalid verdict for: {', '.join(bad)}")
        return 1

    with open(plan_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns, rows = reader.fieldnames, list(reader)
    missing = []
    for r in rows:
        v = verdicts.get(r["apollo_id"])
        if v is None:
            missing.append(f"{r['segment']}: {r['title']} at {r['company']}")
            continue
        r["verdict"] = v["verdict"].strip().lower()
        r["reason"] = (v.get("reason") or "").strip()
    with open(plan_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)

    counts = Counter((r["segment"], r["verdict"] or "unreviewed") for r in rows)
    for seg in dict.fromkeys(r["segment"] for r in rows):
        parts = [f"{v} {counts[(seg, v)]}" for v in ("a", "b", "skip", "unreviewed")
                 if counts[(seg, v)]]
        print(f"{seg}: {', '.join(parts)}")
    if missing:
        print(f"\nNo verdict for {len(missing)} row(s):")
        for m in missing:
            print(f"  {m}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) > 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(args[0] if args else str(EXPORT_DIR / "work" / "verdicts.json"),
                  args[1] if len(args) > 1 else str(PLAN_FILE)))

"""What Claude makes of an imported file, and what to do when it can't.

The analysis answers three questions a person would ask of a strange CSV:
which column is which, what is actually usable in here, and what is the
cheapest way to turn it into contacts. Claude is good at the first and third;
the second is arithmetic.

Every answer has a deterministic fallback. The brain may be logged out, out
of quota, or simply wrong about JSON, and an import that cannot proceed
without it would be an import that cannot proceed.
"""

from __future__ import annotations

import json
import logging

from sellbird import apollo_ops
from sellbird.import_staging import map_headers

logger = logging.getLogger("sellbird.imports")

FIELDS = ("email", "first_name", "last_name", "title", "company", "domain",
          "linkedin_url", "full_name")


def counts_for(rows: list[dict]) -> dict:
    """What is actually in the staged rows. Arithmetic, not judgement."""
    with_email = sum(1 for r in rows if (r.get("email") or "").strip())
    with_company = sum(1 for r in rows if (r.get("company") or "").strip()
                       or (r.get("domain") or "").strip())
    with_name = sum(1 for r in rows if (r.get("first_name") or "").strip())
    return {
        "rows": len(rows),
        "with_email": with_email,
        "with_company": with_company,
        "with_name": with_name,
        "incomplete": sum(1 for r in rows if not (r.get("email") or "").strip()
                          and not (r.get("company") or "").strip()),
    }


def fallback(rows: list[dict], headers: list[str], mapping: dict,
             duplicates: int = 0, reason: str = "") -> dict:
    """The analysis Sellbird can always produce on its own."""
    counts = counts_for(rows)
    recommended = apollo_ops.recommend(rows)
    best = recommended[0] if recommended else None
    parts = [f"{counts['rows']} rows, {counts['with_email']} with an address"]
    if duplicates:
        parts.append(f"{duplicates} already in your pipeline")
    if best:
        op = apollo_ops.OPS[best["op"]]
        parts.append(f"best route is {op.label.lower()} at {best['credits']} credits")
    summary = ", ".join(parts) + "."
    warnings = []
    if counts["incomplete"]:
        warnings.append(f"{counts['incomplete']} rows have neither an address nor "
                        "a company, so nothing can be looked up for them.")
    if reason:
        warnings.append(reason)
    return {"mapping": mapping, "summary": summary, "quality": counts,
            "duplicates": duplicates, "recommended": recommended,
            "warnings": warnings, "by": "sellbird"}


def _ops_text() -> str:
    lines = []
    for key in apollo_ops.MENU_ORDER:
        op = apollo_ops.OPS[key]
        price = ("nothing" if op.credits_per_unit == 0
                 else f"{op.credits_per_unit} credit"
                      f"{'s' if op.credits_per_unit > 1 else ''} per {op.unit}")
        note = (f" Charged once per {op.unit}, however many rows share it."
                if op.charges_per == "group" else "")
        lines.append(f"- `{op.key}` — {op.label}. Needs {op._needs_text()}. "
                     f"Costs {price}.{note} Gives {op.gives.lower()}. "
                     f"Writes {', '.join(op.produces) or 'nothing of its own'}.")
    return "\n".join(lines)


def _clean_mapping(value, headers: list[str], guess: dict) -> dict:
    """Keep only real columns mapped to real fields; fall back to the guess."""
    if not isinstance(value, dict):
        return guess
    out = {}
    for column, field in value.items():
        if column in headers and isinstance(field, str) and field in FIELDS:
            out[column] = field
    return out or guess


def _clean_recommended(value, rows: list[dict]) -> list[dict]:
    """Keep only operations that exist, and re-price them ourselves.

    Claude proposes the route; the credit figure always comes from the
    operation registry, so no answer can quote a price we would not charge.
    """
    if not isinstance(value, list):
        return apollo_ops.recommend(rows)
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = item.get("op")
        op = apollo_ops.OPS.get(key) if isinstance(key, str) else None
        if op is None or op.danger:
            continue
        eligible = op.eligible(rows)
        if not eligible:
            # Recommending an operation with nothing to do reads as a cost
            # the list has not got. Rows that already hold what it produces
            # are not eligible, so this is also what stops Claude proposing
            # a second route to an answer already bought.
            continue
        units = op.charge_units(rows)
        out.append({"op": op.key, "rows": len(eligible),
                    "charged": len(units),
                    "credits": len(units) * op.credits_per_unit,
                    "why": " ".join(str(item.get("why") or "").split())[:300]})
    return out or apollo_ops.recommend(rows)


async def analyse(brain, *, name: str, fmt: str, headers: list[str],
                  sample: list[dict], rows: list[dict],
                  duplicates: int = 0) -> dict:
    """Claude's read of the file, with Sellbird's arithmetic kept authoritative.

    Returns the same shape whether Claude answered or not, so callers never
    branch on it.
    """
    guess = map_headers(headers)
    counts = counts_for(rows)
    if brain is None:
        return fallback(rows, headers, guess, duplicates)

    prompt = brain.load_prompt(
        "importer", name=name or "(pasted)", format=fmt,
        row_count=len(rows), headers=", ".join(headers[:40]),
        sample=json.dumps(sample, indent=1)[:6000],
        mapping=json.dumps(guess, indent=1),
        counts=json.dumps(counts, indent=1),
        ops=_ops_text())
    if not prompt:
        return fallback(rows, headers, guess, duplicates,
                        "The importer prompt is missing, so this is Sellbird's "
                        "own reading of the file.")
    try:
        answer = await brain.think_json(prompt, agent="importer",
                                        task="analyse_import")
    except Exception as e:  # a brain failure must not lose the staged rows
        logger.warning("import analysis failed: %s", e)
        answer = None
    if not isinstance(answer, dict):
        return fallback(rows, headers, guess, duplicates,
                        "Claude could not read this file, so the routes below "
                        "are Sellbird's own reading of it.")

    quality = answer.get("quality")
    summary = " ".join(str(answer.get("summary") or "").split())[:600]
    warnings = [" ".join(str(w).split())[:300]
                for w in (answer.get("warnings") or [])
                if isinstance(w, (str, int, float))][:6]
    return {
        "mapping": _clean_mapping(answer.get("mapping"), headers, guess),
        "summary": summary or fallback(rows, headers, guess, duplicates)["summary"],
        # Claude's count is a reading of ten sample rows; ours counts every
        # staged row, so ours wins and theirs is kept for comparison.
        "quality": counts,
        "claude_quality": quality if isinstance(quality, dict) else {},
        "duplicates": duplicates,
        "recommended": _clean_recommended(answer.get("recommended"), rows),
        "warnings": warnings,
        "by": "claude",
    }

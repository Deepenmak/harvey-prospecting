"""Import a contact list that was produced somewhere else.

``sellbird fetch`` writes the contact files and the database together, so a
locally fetched contact is visible to the dashboard, the Writer and the
Sender at once. A list produced elsewhere -- a cloud run, the leads
dashboard, another machine -- arrives as JSON and has to land in both places
or the pipeline only half sees it.

This module is that one-way door: JSON in, contact files, ledger and
database out. It spends nothing, sends nothing and never deletes: an
existing contact is updated field by field (a non-empty incoming value wins,
an empty one never erases what is there), and a contact that is already in
the database keeps its id, its status and its conversation history.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sellbird import fetch
from sellbird.fetch import CONTACT_COLUMNS
from sellbird.models.company import Company
from sellbird.models.prospect import Prospect

logger = logging.getLogger(__name__)

# A paste or a file this big is a mistake, not a contact list.
MAX_CONTACTS = 20_000
MAX_RUNS = 5_000
# One contact's field, after whitespace is collapsed. Angles and briefs are
# the long ones; past this it is the wrong file.
MAX_FIELD = 20_000
MAX_NOTES = 1_500

# What goes into a prospect's personalization notes, in the order the Writer
# should meet it: why they would buy, then the evidence, then the warnings.
_NOTE_FIELDS = (
    ("angle", ""),
    ("fit_reason", "Fit"),
    ("signals", "Signals"),
    ("red_flags", "Careful"),
    ("hiring_roles", "Hiring"),
    ("tech_stack", "Stack"),
)


class ImportError_(ValueError):
    """The payload isn't a contact list Sellbird can import."""


@dataclass
class ImportReport:
    contacts: int = 0           # rows in the payload, after validation
    new_contacts: int = 0       # not in the contact files before
    runs: int = 0               # ledger runs restored
    credits: int = 0            # credits those runs record
    companies_added: int = 0
    prospects_added: int = 0
    prospects_updated: int = 0
    no_email: int = 0           # kept in the files, no prospect row
    files_written: bool = False
    ledger_written: bool = False
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (f"{self.contacts} contacts ({self.new_contacts} new), "
                f"{self.companies_added} companies and {self.prospects_added} "
                f"prospects added, {self.prospects_updated} updated")


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    return str(value).strip()[:MAX_FIELD]


def _contact_id(row: dict) -> str:
    """The key the contact files dedup on. Apollo's id when there is one, so
    a re-import of the same list updates rather than duplicates; otherwise
    the email, which is the only other stable identity a contact has."""
    apollo_id = _text(row.get("apollo_id"))
    if apollo_id:
        return apollo_id[:200]
    email = _text(row.get("email")).lower()
    return f"email:{email}"[:200] if email else ""


def parse_payload(raw) -> tuple[list[dict], list[dict]]:
    """Validate what was pasted or loaded, and return (contacts, runs).

    Accepts the two shapes the leads dashboard holds: a bare list of
    contacts, or an object with ``contacts`` and (optionally) ``runs``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ImportError_(f"that isn't JSON ({e.msg}, line {e.lineno})")
    runs: list = []
    if isinstance(raw, dict):
        for key in ("contacts", "rows", "data"):
            if isinstance(raw.get(key), list):
                runs = raw.get("runs") if isinstance(raw.get("runs"), list) else []
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise ImportError_('expected a list of contacts, or {"contacts": [...]}')
    if len(raw) > MAX_CONTACTS:
        raise ImportError_(f"{len(raw):,} contacts is more than the {MAX_CONTACTS:,} limit")
    if len(runs) > MAX_RUNS:
        raise ImportError_(f"{len(runs):,} runs is more than the {MAX_RUNS:,} limit")

    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ImportError_("every contact must be an object with named fields")
        row = {c: _text(item.get(c)) for c in CONTACT_COLUMNS}
        row["apollo_id"] = _contact_id(item)
        if not row["apollo_id"]:
            continue  # nothing to key it on: no id and no email
        row["email"] = row["email"].lower()
        row["domain"] = row["domain"].lower()
        rows.append(row)
    if not rows:
        raise ImportError_("no contact in that list has an id or an email address")
    return rows, [r for r in runs if isinstance(r, dict)]


def notes_for(row: dict) -> str:
    """One contact's research, as the Writer reads it."""
    parts = []
    for key, label in _NOTE_FIELDS:
        value = " ".join((row.get(key) or "").split())
        if not value:
            continue
        parts.append(f"{label}: {value}" if label else value)
    return " | ".join(parts)[:MAX_NOTES]


def _score(row: dict) -> int:
    try:
        return max(0, min(100, int(float(row.get("score") or 0))))
    except (TypeError, ValueError):
        return 0


def rebuild_ledger(rows: list[dict], runs: list[dict], path=None) -> int:
    """Write the fetch ledger from the imported runs, so a later fetch knows
    what was already seen and what it has already spent. Returns the credits
    those runs record."""
    seen_ids = {r["apollo_id"] for r in rows if r["apollo_id"]
                and not r["apollo_id"].startswith("email:")}
    skipped: set[str] = set()
    for run in runs:
        seen_ids |= {str(i) for i in (run.get("seen_ids") or []) if i}
        skipped |= {str(o) for o in (run.get("skipped_orgs") or []) if o}
    path = fetch.LOCAL_LEDGER if path is None else path
    ledger = {
        "runs": runs,
        "seen_ids": sorted(seen_ids),
        "seen_orgs": sorted({r["company"].lower() for r in rows if r["company"]}),
        "skipped_orgs": sorted(skipped),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    credits = 0
    for run in runs:
        try:
            credits += int(run.get("credits_used") or 0)
        except (TypeError, ValueError):
            pass
    return credits


async def _store_row(state, row: dict, report: ImportReport) -> None:
    """One contact into the database: its company, then the person."""
    company_name = row["company"]
    domain = row["domain"]
    if not company_name and not domain:
        return
    location = ", ".join(x for x in (row["city"], row["state"], row["country"]) if x)
    # A company with no website still has to dedup across imports, so it is
    # keyed by its name the way a discovery provider keys one by its own id.
    external_id = "" if domain else f"import:{company_name.lower()}"
    before = (await state.get_company_by_domain(domain) if domain
              else await state.get_company_by_external_id(external_id))
    company_id = await state.add_company(Company(
        name=company_name, domain=domain,
        website=f"https://{domain}" if domain else "",
        industry=row["industry"], company_size=row["employees"],
        location=location, source="import", source_url=row["linkedin_url"],
        external_id=external_id,
    ))
    if before is None:
        report.companies_added += 1

    if not row["email"]:
        report.no_email += 1
        return
    prospect = Prospect(
        company_id=company_id, first_name=row["first_name"], last_name=row["last_name"],
        email=row["email"], email_status=row["email_status"],
        email_verified=row["email_status"] == "verified",
        linkedin_url=row["linkedin_url"], title=row["title"],
        seniority=row["seniority"], source="import", source_url=row["linkedin_url"],
        score=_score(row), personalization_notes=notes_for(row),
        company=company_name, industry=row["industry"], company_size=row["employees"],
    )
    known = await state.get_prospect_by_email(row["email"])
    stored_id = await state.add_prospect(prospect)
    if known is None:
        report.prospects_added += 1
        return
    # Already known: refresh what research adds, never the status or the id.
    await _refresh_prospect(state, stored_id, prospect, company_id)
    report.prospects_updated += 1


async def _refresh_prospect(state, prospect_id: str, incoming: Prospect,
                            company_id: str) -> None:
    """Update the fields an import can improve. An empty incoming value
    leaves the stored one alone, and status, id and history are never
    touched."""
    updates = {
        "company_id": company_id,
        "first_name": incoming.first_name, "last_name": incoming.last_name,
        "title": incoming.title, "seniority": incoming.seniority,
        "linkedin_url": incoming.linkedin_url, "email_status": incoming.email_status,
        "personalization_notes": incoming.personalization_notes,
        "company": incoming.company, "industry": incoming.industry,
        "company_size": incoming.company_size,
    }
    sets = {k: v for k, v in updates.items() if v}
    if incoming.email_status:
        sets["email_verified"] = int(incoming.email_verified)
    if incoming.score:
        sets["score"] = incoming.score
    if not sets:
        return
    sets["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    clause = ", ".join(f"{k} = ?" for k in sets)
    async with state._connect() as db:
        await db.execute(f"UPDATE prospects SET {clause} WHERE id = ?",
                         (*sets.values(), prospect_id))
        await db.commit()


async def import_contacts(state, payload, *, write_files: bool = True,
                          write_db: bool = True) -> ImportReport:
    """Import ``payload`` (JSON text, a list, or {"contacts", "runs"}).

    Writes the two contact files, rebuilds the fetch ledger when the payload
    carries runs, and folds every contact into the database.
    """
    rows, runs = parse_payload(payload)
    report = ImportReport(contacts=len(rows), runs=len(runs))

    existing = fetch.read_contacts(fetch.CONTACTS_FILE)
    known = {r.get("apollo_id") for r in existing}
    report.new_contacts = sum(1 for r in rows if r["apollo_id"] not in known)
    if write_files:
        fetch.save_contacts(fetch.merge_contacts(existing, rows),
                            fetch.CONTACTS_FILE, fetch.EMAIL_CONTACTS_FILE)
        report.files_written = True
        if runs:
            report.credits = rebuild_ledger(rows, runs)
            report.ledger_written = True

    if write_db:
        await state.init_db()
        for row in rows:
            try:
                await _store_row(state, row, report)
            except Exception as e:  # one bad row must not lose the rest
                logger.warning("import: %s (%s) not stored: %s",
                               row.get("company"), row.get("email"), e)
                report.problems.append(
                    f"{row.get('company') or row.get('email') or 'a contact'}: {e}")
        await state.log_action(
            action_type="contacts_imported", agent="you",
            details={"contacts": report.contacts, "new": report.new_contacts,
                     "prospects_added": report.prospects_added,
                     "prospects_updated": report.prospects_updated})
    return report

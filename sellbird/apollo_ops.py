"""Apollo operations as data: what each one needs, gives, and costs.

Apollo is the only thing in this pipeline that spends money, and every one of
its endpoints has a different price. Burying those prices inside call sites
means nobody can answer "what will this cost?" before pressing the button, so
each operation is declared here instead — its inputs, its outputs, its credits
per unit — and the dashboard, the CLI and the estimator all read the same
declaration.

This mirrors ``DiscoveryProvider`` (sellbird/collectors/discover.py), where
the same approach already keeps discovery honest about spend.

Credits, per Apollo's own API pricing:
  - search endpoints return no email and are treated as free here
  - people/match costs 1 credit when Apollo finds data, 0 when it finds nothing
  - a mobile number costs 8 more, so it is off unless asked for by name
  - organization enrichment is 1 credit a company, job postings 1 a page
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

logger = logging.getLogger("sellbird.apollo_ops")

# How many people one bulk_match call carries.
BULK_SIZE = 10
# Apollo rate limits per minute; a small pause keeps a long run inside them.
PAUSE_SECONDS = 0.2


def already_run(row: dict) -> set[str]:
    """Which operations have already run on this row (its ``route``)."""
    return {part for part in (row.get("route") or "").split(",") if part}


def row_field(row: dict, name: str) -> str:
    """One field of a staged row, wherever it lives.

    A row starts with what the file carried and grows what an earlier
    operation found — a company's Apollo id, say, which is what makes its job
    postings readable. Both are the row, so both answer here.
    """
    value = row.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    enriched = row.get("enriched_json")
    if isinstance(enriched, str):
        try:
            enriched = json.loads(enriched or "{}")
        except (json.JSONDecodeError, TypeError):
            enriched = {}
    if isinstance(enriched, dict):
        found = enriched.get(name)
        if isinstance(found, str) and found.strip():
            return found.strip()
        if isinstance(found, (int, float)) and found:
            return str(found)
    return ""


@dataclass
class OpResult:
    """What one operation did to one staged row."""
    row_id: str
    found: bool = False
    credits: int = 0
    person: dict | None = None
    organization: dict | None = None
    error: str = ""


@dataclass(frozen=True)
class ApolloOp:
    key: str
    label: str
    blurb: str
    credits_per_unit: int
    unit: str                              # contact | company | page
    requires: tuple[str, ...] = ()         # a row needs all of these
    requires_any: tuple[tuple[str, ...], ...] = ()   # ...and one of each group
    gives: str = ""
    plan_note: str = ""
    danger: bool = False                   # render loudly: this one is expensive
    default: bool = False                  # the usual choice for a staged list

    def eligible(self, rows: Iterable[dict]) -> list[dict]:
        """The rows this operation can act on.

        A row it has already run on is not one of them, whatever that run
        found: paying twice for the same answer is the mistake this whole
        module exists to prevent. A row missing what the operation needs is
        left alone rather than sent to Apollo to fail at a credit's risk.
        """
        out = []
        for row in rows:
            if self.key in already_run(row):
                continue
            if any(not row_field(row, f) for f in self.requires):
                continue
            if any(not any(row_field(row, f) for f in group)
                   for group in self.requires_any):
                continue
            out.append(row)
        return out

    def estimate(self, rows: Iterable[dict]) -> int:
        """Credits this would cost at most, before anything is called.

        At most, not exactly: Apollo charges nothing for a person it cannot
        find, so a run usually comes in under its estimate. Quoting the
        ceiling is the honest way round.
        """
        return len(self.eligible(rows)) * self.credits_per_unit

    def as_dict(self, rows: Iterable[dict] | None = None) -> dict:
        rows = list(rows or [])
        eligible = self.eligible(rows)
        return {
            "key": self.key, "label": self.label, "blurb": self.blurb,
            "credits_per_unit": self.credits_per_unit, "unit": self.unit,
            "gives": self.gives, "plan_note": self.plan_note,
            "danger": self.danger, "default": self.default,
            "free": self.credits_per_unit == 0,
            "eligible": len(eligible),
            "estimated_credits": len(eligible) * self.credits_per_unit,
            "needs": self._needs_text(),
        }

    def _needs_text(self) -> str:
        parts = [f.replace("_", " ") for f in self.requires]
        parts += [" or ".join(f.replace("_", " ") for f in group)
                  for group in self.requires_any]
        return ", ".join(parts) or "nothing"


OPS: dict[str, ApolloOp] = {
    "people_match_email": ApolloOp(
        key="people_match_email",
        label="Find each person by email",
        blurb="Hands Apollo the address and gets the whole record back: name, "
              "title, company, LinkedIn, and whether the address is deliverable.",
        credits_per_unit=1, unit="contact",
        requires=("email",),
        gives="Full contact record and a verified/guess email status",
        default=True,
    ),
    "people_match_name_domain": ApolloOp(
        key="people_match_name_domain",
        label="Find each person by name and company",
        blurb="For rows with a name and a company but no address. Apollo "
              "returns the work address when it has one.",
        credits_per_unit=1, unit="contact",
        requires=("first_name",),
        requires_any=(("domain", "company"),),
        gives="Work email address and the full contact record",
    ),
    "org_enrich": ApolloOp(
        key="org_enrich",
        label="Enrich each company",
        blurb="Company-level detail Apollo holds: tech stack, headcount and "
              "its growth, industry, latest funding.",
        credits_per_unit=1, unit="company",
        requires=("domain",),
        gives="Tech stack, headcount growth, funding detail",
    ),
    "org_job_postings": ApolloOp(
        key="org_job_postings",
        label="Read each company's open roles",
        blurb="Who a company is hiring, straight from Apollo. The strongest "
              "in-market signal there is, and it covers companies whose job "
              "board Sellbird cannot read for free.",
        credits_per_unit=1, unit="page",
        requires=("apollo_org_id",),
        gives="Open roles with titles, departments and posting dates",
        plan_note="Paid plans only",
    ),
    "people_search": ApolloOp(
        key="people_search",
        label="Find people at each company",
        blurb="Free: lists who holds your target titles at a company. No "
              "addresses — matching them costs a credit each, afterwards.",
        credits_per_unit=0, unit="contact",
        requires_any=(("domain", "company"),),
        gives="Candidate names and titles, no addresses",
    ),
    "org_search": ApolloOp(
        key="org_search",
        label="Look up each company",
        blurb="Free: industry, headcount, website, tech stack and funding "
              "stage for a company you only have a name for.",
        credits_per_unit=0, unit="company",
        requires_any=(("domain", "company"),),
        gives="Industry, headcount, domain, tech stack, funding stage",
    ),
    "phone_reveal": ApolloOp(
        key="phone_reveal",
        label="Reveal mobile numbers",
        blurb="A mobile number costs eight credits on top of the one the "
              "contact already cost. Nine credits a person.",
        credits_per_unit=8, unit="contact",
        requires=("apollo_id",),
        gives="Direct mobile number",
        danger=True,
    ),
}

# The order the menu shows them in: what you will probably want, then the free
# lookups, then the costly one.
MENU_ORDER = ("people_match_email", "people_match_name_domain", "org_enrich",
              "org_job_postings", "people_search", "org_search", "phone_reveal")


def menu(rows: Iterable[dict] | None = None) -> list[dict]:
    """The whole operation menu, priced against these rows."""
    rows = list(rows or [])
    return [OPS[key].as_dict(rows) for key in MENU_ORDER if key in OPS]


def recommend(rows: Iterable[dict]) -> list[dict]:
    """Which operations actually fit this list, best first.

    MENU_ORDER is the ranking: turning rows into contactable people comes
    before company facts, and a free lookup that produces no address is a
    supplement, not a route. Sorting by coverage instead would recommend the
    free company lookup over matching addresses, which is cheaper and useless.

    Deterministic, and used both as Claude's starting point and as the answer
    when Claude is unavailable.
    """
    rows = list(rows)
    out = []
    for key in MENU_ORDER:
        op = OPS[key]
        if op.danger:
            continue
        eligible = op.eligible(rows)
        if not eligible:
            continue
        out.append({"op": key, "rows": len(eligible),
                    "credits": len(eligible) * op.credits_per_unit,
                    "why": f"{len(eligible)} of {len(rows)} rows have "
                           f"{op._needs_text()}."})
    return out


# ── Running one, inside a cap ──


@dataclass
class SpendReport:
    op: str = ""
    attempted: int = 0
    found: int = 0
    credits: int = 0
    stopped: str = ""
    results: list[OpResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"op": self.op, "attempted": self.attempted, "found": self.found,
                "credits": self.credits, "stopped": self.stopped}


async def run_op(op: ApolloOp, client, rows: list[dict], *, max_credits: int,
                 pause: float = PAUSE_SECONDS) -> SpendReport:
    """Run one operation over staged rows, stopping dead at ``max_credits``.

    The cap is checked before every call, not after, so the ceiling the user
    approved is the ceiling that is spent — the same rule discovery applies
    to dollars between batches.
    """
    report = SpendReport(op=op.key)
    eligible = op.eligible(rows)
    if not eligible:
        report.stopped = "no row has what this needs"
        return report
    if max_credits <= 0 and op.credits_per_unit > 0:
        report.stopped = "no credits approved"
        return report

    runner = _RUNNERS.get(op.key)
    if runner is None:
        report.stopped = f"{op.key} cannot be run yet"
        return report

    batch_size = BULK_SIZE if op.key == "people_match_email" else 1
    for start in range(0, len(eligible), batch_size):
        if op.credits_per_unit and report.credits + op.credits_per_unit > max_credits:
            report.stopped = f"stopped at the {max_credits} credit cap"
            break
        chunk = eligible[start:start + batch_size]
        if op.credits_per_unit:
            room = (max_credits - report.credits) // op.credits_per_unit
            chunk = chunk[:max(1, room)]
        try:
            results = await runner(client, chunk)
        except Exception as e:  # one bad call must not lose the whole run
            logger.warning("apollo %s failed: %s", op.key, e)
            results = [OpResult(row_id=r["id"], error=str(e)[:200]) for r in chunk]
        for result in results:
            report.attempted += 1
            if result.found:
                report.found += 1
                report.credits += result.credits
            report.results.append(result)
        if pause:
            await asyncio.sleep(pause)
    if report.attempted < len(eligible) and not report.stopped:
        # The cap was reached mid-batch: say so, rather than let a partial
        # run read as a complete one.
        report.stopped = (f"stopped at the {max_credits} credit cap, "
                          f"{len(eligible) - report.attempted} rows untouched")
    return report


# Each runner takes (client, rows) and returns one OpResult per row.

async def _run_match_email(client, rows: list[dict]) -> list[OpResult]:
    if len(rows) == 1:
        person = await client.match(email=rows[0]["email"])
        return [_person_result(rows[0], person)]
    people = await client.bulk_match([{"email": r["email"]} for r in rows])
    return [_person_result(row, person) for row, person in zip(rows, people)]


async def _run_match_name(client, rows: list[dict]) -> list[OpResult]:
    out = []
    for row in rows:
        person = await client.match(
            first_name=row.get("first_name", ""), last_name=row.get("last_name", ""),
            domain=row.get("domain", ""), organization_name=row.get("company", ""))
        out.append(_person_result(row, person))
    return out


async def _run_org_enrich(client, rows: list[dict]) -> list[OpResult]:
    out = []
    for row in rows:
        org = await client.enrich_organization(row_field(row, "domain"))
        out.append(OpResult(row_id=row["id"], found=bool(org),
                            credits=1 if org else 0, organization=org))
    return out


async def _run_job_postings(client, rows: list[dict]) -> list[OpResult]:
    """A company's open roles. One credit a page; one page is read."""
    out = []
    for row in rows:
        postings = await client.job_postings(row_field(row, "apollo_org_id"))
        out.append(OpResult(row_id=row["id"], found=True, credits=1,
                            organization={"job_postings": postings}))
    return out


async def _run_people_search(client, rows: list[dict]) -> list[OpResult]:
    out = []
    for row in rows:
        data = await client.search_organizations(
            {"q_organization_name": row.get("company") or row.get("domain")})
        orgs = (data or {}).get("organizations") or []
        out.append(OpResult(row_id=row["id"], found=bool(orgs), credits=0,
                            organization=orgs[0] if orgs else None))
    return out


_RUNNERS: dict[str, Callable[..., Awaitable[list[OpResult]]]] = {
    "people_match_email": _run_match_email,
    "people_match_name_domain": _run_match_name,
    "org_enrich": _run_org_enrich,
    "org_search": _run_people_search,
    "org_job_postings": _run_job_postings,
}


def org_fields(org: dict) -> dict:
    """An Apollo organization record, in the contact vocabulary.

    The Apollo id travels with it, because it is what later makes that
    company's job postings readable.
    """
    postings = org.get("job_postings")
    if isinstance(postings, list):
        titles = [str(p.get("title") or "").strip() for p in postings
                  if isinstance(p, dict)]
        titles = [t for t in titles if t]
        return {"hiring_roles": "; ".join(titles[:5]),
                "open_roles": str(len(postings))}
    return {
        "apollo_org_id": org.get("id") or "",
        "company": org.get("name") or "",
        "industry": org.get("industry") or "",
        "employees": str(org.get("estimated_num_employees") or ""),
        "tech_stack": ", ".join((org.get("technology_names") or [])[:12]),
    }


def _person_result(row: dict, person: dict | None) -> OpResult:
    """Apollo charges for a person it finds, and nothing for one it doesn't."""
    if not person:
        return OpResult(row_id=row["id"], found=False, credits=0)
    return OpResult(row_id=row["id"], found=True, credits=1, person=person)


def contact_from_person(person: dict, row: dict | None = None) -> dict:
    """One Apollo person record, in the contact shape the rest of Sellbird
    speaks (sellbird.fetch.CONTACT_COLUMNS)."""
    from sellbird.fetch import SENIORITY, domain_of, email_status

    org = person.get("organization") or {}
    row = row or {}
    domain = domain_of(org.get("website_url") or org.get("primary_domain") or "") \
        or (row.get("domain") or "")
    return {
        "apollo_id": person.get("id") or "",
        "first_name": person.get("first_name") or row.get("first_name") or "",
        "last_name": person.get("last_name") or row.get("last_name") or "",
        "title": person.get("title") or row.get("title") or "",
        "seniority": SENIORITY.get(person.get("seniority") or "", ""),
        "email": (person.get("email") or row.get("email") or "").lower(),
        "email_status": email_status(person.get("email_status")),
        "linkedin_url": person.get("linkedin_url") or row.get("linkedin_url") or "",
        "company": org.get("name") or row.get("company") or "",
        "domain": domain,
        "industry": org.get("industry") or "",
        "employees": str(org.get("estimated_num_employees") or ""),
        "city": person.get("city") or "", "state": person.get("state") or "",
        "country": person.get("country") or "",
        "tech_stack": ", ".join((org.get("technology_names") or [])[:12]),
    }


def summarise(report: SpendReport) -> str:
    """One line a person can read."""
    op = OPS.get(report.op)
    label = op.label.lower() if op else report.op
    line = (f"{label}: {report.found} of {report.attempted} found, "
            f"{report.credits} credit{'s' if report.credits != 1 else ''} spent")
    return f"{line} ({report.stopped})" if report.stopped else line


def parse_rows_json(value) -> dict:
    """Small helper for stored JSON blobs that may be malformed."""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

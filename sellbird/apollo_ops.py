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


def plural(word: str, count: int) -> str:
    """"company" and "companies", not "companys". It is on screen."""
    if count == 1:
        return word
    return word[:-1] + "ies" if word.endswith("y") else word + "s"


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
    # The row fields this operation actually writes. A row that already holds
    # every one of them has nothing left to buy, so it is not eligible -- and
    # because three different operations all produce ("email", "email_status"),
    # that one rule also makes them mutually exclusive. Buying an address with
    # people_enrich_id used to make the row newly eligible for
    # people_match_email, which charged a second credit for the same answer.
    produces: tuple[str, ...] = ()
    # What one credit buys. "row" is a credit a row; "group" is a credit per
    # distinct ``group_by`` value, which is how Apollo really bills a company
    # record -- once, however many of its people are on your list.
    charges_per: str = "row"
    group_by: str = ""
    gives: str = ""
    plan_note: str = ""
    danger: bool = False                   # render loudly: this one is expensive
    default: bool = False                  # the usual choice for a staged list

    def eligible(self, rows: Iterable[dict]) -> list[dict]:
        """The rows this operation can act on.

        Three reasons a row is left alone, all of them the same reason:
        nobody should pay twice for one answer.

        - This operation has already run on it, whatever that run found.
        - It already holds everything this operation produces, even if some
          other operation is what put it there.
        - It is missing what this operation needs, so sending it to Apollo
          would risk a credit on a call that cannot work.
        """
        out = []
        for row in rows:
            if self.key in already_run(row):
                continue
            if self.produces and all(row_field(row, f) for f in self.produces):
                continue
            if any(not row_field(row, f) for f in self.requires):
                continue
            if any(not any(row_field(row, f) for f in group)
                   for group in self.requires_any):
                continue
            out.append(row)
        return out

    def charge_units(self, rows: Iterable[dict]) -> list[list[dict]]:
        """What one credit buys, as groups of rows.

        A person costs a credit a person, so each unit is one row. A company
        record costs a credit a company however many of its people are on the
        list, so the unit is every row sharing that company: five colleagues
        at one firm is one credit, and all five rows get the answer. Charging
        that per row was pure waste, and the ``unit`` field said as much
        while the arithmetic did the opposite.
        """
        eligible = self.eligible(rows)
        if self.charges_per != "group" or not self.group_by:
            return [[row] for row in eligible]
        groups: dict[str, list[dict]] = {}
        for row in eligible:
            key = row_field(row, self.group_by).strip().lower()
            # A row with no key of its own is its own group: it still needs
            # the answer, and it cannot share anyone else's.
            groups.setdefault(key or f"row:{row.get('id')}", []).append(row)
        return list(groups.values())

    def estimate(self, rows: Iterable[dict]) -> int:
        """Credits this would cost at most, before anything is called.

        At most, not exactly: Apollo charges nothing for a person it cannot
        find, so a run usually comes in under its estimate. Quoting the
        ceiling is the honest way round.
        """
        return len(self.charge_units(rows)) * self.credits_per_unit

    def as_dict(self, rows: Iterable[dict] | None = None) -> dict:
        rows = list(rows or [])
        eligible = self.eligible(rows)
        units = self.charge_units(rows)
        return {
            "key": self.key, "label": self.label, "blurb": self.blurb,
            "credits_per_unit": self.credits_per_unit, "unit": self.unit,
            "gives": self.gives, "plan_note": self.plan_note,
            "danger": self.danger, "default": self.default,
            "free": self.credits_per_unit == 0,
            "eligible": len(eligible),
            "charged": len(units),
            "shared": len(eligible) - len(units),
            "estimated_credits": len(units) * self.credits_per_unit,
            "needs": self._needs_text(),
            "available": self.key in _RUNNERS,
            # Only rows skipped because they already hold what this writes.
            # Counting every skipped row here told people that five rows with
            # no address at all "already have this", which is the opposite of
            # true and the opposite of useful.
            "already_have": self._already_have(rows),
        }

    def _already_have(self, rows: Iterable[dict]) -> int:
        if not self.produces:
            return 0
        return sum(1 for row in rows
                   if all(row_field(row, f) for f in self.produces))

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
        produces=("email", "email_status"),
        gives="Full contact record and a verified/guess email status",
        default=True,
    ),
    "people_enrich_id": ApolloOp(
        key="people_enrich_id",
        label="Get the addresses for these candidates",
        blurb="Apollo's free search names the people but withholds their "
              "addresses. This buys them: the work email, whether it is "
              "deliverable, and the rest of the record.",
        credits_per_unit=1, unit="contact",
        requires=("apollo_id",),
        produces=("email", "email_status"),
        gives="Work email address, seniority, location and company detail",
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
        produces=("email", "email_status"),
        gives="Work email address and the full contact record",
    ),
    "org_enrich": ApolloOp(
        key="org_enrich",
        label="Enrich each company",
        blurb="Company-level detail Apollo holds: tech stack, headcount and "
              "its growth, industry, latest funding.",
        credits_per_unit=1, unit="company",
        requires=("domain",),
        produces=("tech_stack", "employees", "industry"),
        charges_per="group", group_by="domain",
        gives="Tech stack, headcount growth, funding detail",
    ),
    "org_job_postings": ApolloOp(
        key="org_job_postings",
        label="Read each company's open roles",
        blurb="Who a company is hiring, straight from Apollo. The strongest "
              "in-market signal there is, and it covers companies whose job "
              "board Sellbird cannot read for free. One page of roles per "
              "company, which is one credit however many of its people are "
              "on your list.",
        credits_per_unit=1, unit="company",
        requires=("apollo_org_id",),
        produces=("open_roles",),
        charges_per="group", group_by="apollo_org_id",
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
        produces=("industry", "employees", "domain"),
        charges_per="group", group_by="domain",
        gives="Industry, headcount, domain, tech stack, funding stage",
    ),
    "phone_reveal": ApolloOp(
        key="phone_reveal",
        label="Reveal mobile numbers",
        blurb="A mobile number costs eight credits on top of the one the "
              "contact already cost. Nine credits a person.",
        credits_per_unit=8, unit="contact",
        requires=("apollo_id",),
        produces=("phone",),
        gives="Direct mobile number",
        danger=True,
    ),
}

# The order the menu shows them in: what you will probably want, then the free
# lookups, then the costly one.
MENU_ORDER = ("people_enrich_id", "people_match_email", "people_match_name_domain",
              "org_enrich", "org_job_postings", "people_search", "org_search",
              "phone_reveal")


def menu(rows: Iterable[dict] | None = None) -> list[dict]:
    """The whole operation menu, priced against these rows."""
    rows = list(rows or [])
    return [OPS[key].as_dict(rows) for key in MENU_ORDER if key in OPS]


def runnable(key: str) -> bool:
    """Whether this operation has an implementation behind it.

    Two are declared and not wired: ``people_search`` returns new people
    rather than filling in the rows it was given, which needs its own
    staging path, and ``phone_reveal`` needs Apollo's asynchronous reveal
    with a webhook. They stay in the registry because their prices are part
    of the answer to "what can Apollo do and for how much", but nothing
    should offer a button that cannot work.
    """
    return key in _RUNNERS


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
    covered: set[tuple[str, ...]] = set()
    for key in MENU_ORDER:
        op = OPS[key]
        if op.danger or key not in _RUNNERS:
            continue
        eligible = op.eligible(rows)
        if not eligible:
            continue
        # One route to an answer, not two. Three operations return a work
        # address and its status, and listing them all as recommendations
        # reads as "do all three", at a credit a row each time.
        if op.produces and tuple(op.produces) in covered:
            continue
        covered.add(tuple(op.produces))
        units = op.charge_units(rows)
        why = (f"{len(eligible)} of {len(rows)} rows have {op._needs_text()}.")
        if len(units) < len(eligible):
            why += (f" {len(eligible)} rows share {len(units)} "
                    f"{plural(op.unit, len(units))}, so that is what it costs.")
        out.append({"op": key, "rows": len(eligible),
                    "charged": len(units),
                    "credits": len(units) * op.credits_per_unit,
                    "why": why})
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
        report.stopped = "no row needs this, or no row has what it needs"
        return report
    if max_credits <= 0 and op.credits_per_unit > 0:
        report.stopped = "no credits approved"
        return report

    runner = _RUNNERS.get(op.key)
    if runner is None:
        report.stopped = f"{op.key} cannot be run yet"
        return report

    # One unit is one charge. For a person that is one row; for a company it
    # is every row at that company, which is called for once and written to
    # all of them.
    units = op.charge_units(rows)
    batch_size = BULK_SIZE if op.key == "people_match_email" else 1
    for start in range(0, len(units), batch_size):
        if op.credits_per_unit and report.credits + op.credits_per_unit > max_credits:
            report.stopped = f"stopped at the {max_credits} credit cap"
            break
        chunk = units[start:start + batch_size]
        if op.credits_per_unit:
            room = (max_credits - report.credits) // op.credits_per_unit
            chunk = chunk[:max(1, room)]
        leads = [unit[0] for unit in chunk]
        try:
            results = await runner(client, leads)
        except Exception as e:  # one bad call must not lose the whole run
            logger.warning("apollo %s failed: %s", op.key, e)
            results = [OpResult(row_id=r["id"], error=str(e)[:200]) for r in leads]
        by_lead = {r.row_id: r for r in results}
        for unit in chunk:
            answer = by_lead.get(unit[0]["id"])
            if answer is None:
                continue
            report.credits += answer.credits
            for row in unit:
                report.attempted += 1
                if answer.found:
                    report.found += 1
                report.results.append(OpResult(
                    row_id=row["id"], found=answer.found,
                    # The charge belongs to the row that was called for. The
                    # others share the answer at no cost, and their row
                    # credits must say so or the batch total double-counts.
                    credits=answer.credits if row["id"] == unit[0]["id"] else 0,
                    person=answer.person, organization=answer.organization,
                    error=answer.error))
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


async def _run_enrich_id(client, rows: list[dict]) -> list[OpResult]:
    """Candidates from Apollo's own free search, bought one at a time."""
    out = []
    for row in rows:
        person = await client.enrich(row_field(row, "apollo_id"))
        out.append(_person_result(row, person))
    return out


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
    "people_enrich_id": _run_enrich_id,
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
    charged = sum(1 for r in report.results if r.credits)
    if op and op.charges_per == "group" and report.attempted > charged:
        line += (f" ({report.attempted} rows shared {charged} "
                 f"{plural(op.unit, charged)})")
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

"""FETCH: source decision-makers from Apollo on a budget, then profile them.

One run = one segment from segments.yaml:

1. Search Apollo (free) for people matching the segment.
2. Drop anyone already fetched (the ledger), keep one person per company,
   and rank by the segment's title order.
3. Enrich the top N (1 credit each when an email is found), never past
   ``phase_credit_cap`` across all runs.
4. Store companies and prospects in Harvey's database, read each company's
   own site (free) for hiring and tech-stack signals, and score.
5. Write a CSV, and append rows plus a run record to the Google Sheet.

The ledger is the Google Sheet when SHEET_WEBHOOK_URL is set (so a cloud
run with a fresh clone still knows what earlier runs spent and fetched),
and data/fetch_ledger.json otherwise.
"""

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from harvey.integrations.apollo import ApolloClient, apollo_key
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.fetch")

SEGMENTS_FILE = PROJECT_ROOT / "segments.yaml"
LOCAL_LEDGER = PROJECT_ROOT / "data" / "fetch_ledger.json"
EXPORT_DIR = PROJECT_ROOT / "exports"
MAX_SEARCH_PAGES = 5

ROW_COLUMNS = [
    "fetched_at", "segment", "apollo_id", "first_name", "last_name", "title",
    "seniority", "email", "email_status", "linkedin_url", "company", "domain",
    "industry", "employees", "city", "state", "country", "hiring_roles",
    "open_roles", "tech_stack", "score", "angle", "status",
]

# Apollo's seniority vocabulary → Harvey's.
SENIORITY = {"owner": "c_suite", "founder": "c_suite", "c_suite": "c_suite",
             "partner": "c_suite", "vp": "vp", "head": "director",
             "director": "director", "manager": "manager"}


def load_segments(path: Path = SEGMENTS_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("segments"):
        raise ValueError(f"{path} has no segments")
    return data


def pick_segment(spec: dict, requested: str, last_segment: str) -> dict:
    """``auto`` rotates to the segment after the last one run."""
    segments = spec["segments"]
    if requested and requested != "auto":
        for seg in segments:
            if seg["key"] == requested:
                return seg
        raise ValueError(f"Unknown segment {requested!r}. "
                         f"Known: {', '.join(s['key'] for s in segments)}")
    keys = [s["key"] for s in segments]
    if last_segment in keys:
        return segments[(keys.index(last_segment) + 1) % len(segments)]
    return segments[0]


def search_filters(seg: dict) -> dict:
    filters = {
        "person_titles": seg.get("titles", []),
        "organization_locations": seg.get("locations", []),
        "organization_num_employees_ranges": seg.get("employees", []),
    }
    if seg.get("seniorities"):
        filters["person_seniorities"] = seg["seniorities"]
    if seg.get("keyword_tags"):
        filters["q_organization_keyword_tags"] = seg["keyword_tags"]
    return filters


def title_priority(title: str, titles: list[str]) -> int:
    """Index of the first segment title whose words all appear in ``title``."""
    low = (title or "").lower()
    for i, wanted in enumerate(titles):
        if all(w in low for w in wanted.lower().split()):
            return i
    return len(titles)


def choose_candidates(people: list[dict], titles: list[str], seen_ids: set,
                      seen_orgs: set, limit: int) -> list[dict]:
    """One person per company, best title first, nobody fetched before."""
    best: dict[str, dict] = {}
    for p in people:
        org = ((p.get("organization") or {}).get("name") or "").strip()
        if not org or not p.get("has_email") or p.get("id") in seen_ids:
            continue
        if org.lower() in seen_orgs:
            continue
        rank = title_priority(p.get("title", ""), titles)
        current = best.get(org.lower())
        if current is None or rank < current["_rank"]:
            best[org.lower()] = dict(p, _rank=rank)
    ranked = sorted(best.values(), key=lambda p: p["_rank"])
    return ranked[:limit]


def domain_of(url: str) -> str:
    url = (url or "").lower().strip()
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    url = url.split("/")[0]
    return url[4:] if url.startswith("www.") else url


def email_status(apollo_status: str) -> str:
    """Only Apollo-verified addresses count as deliverable to Harvey."""
    return "verified" if (apollo_status or "").lower() == "verified" else "guess"


def score(row: dict, seg: dict) -> int:
    """Deterministic fit score, 1-100. The routine's angle adds the judgement."""
    s = 40
    rank = title_priority(row["title"], seg.get("titles", []))
    s += max(0, 25 - rank * 3)
    if row["email_status"] == "verified":
        s += 10
    if row["hiring_roles"]:
        s += 15
    try:
        emp = int(row["employees"] or 0)
    except ValueError:
        emp = 0
    if 200 <= emp <= 5000:
        s += 10
    return max(1, min(100, s))


# ── Ledgers ──────────────────────────────────────────────────────────


@dataclass
class LedgerState:
    seen_ids: set = field(default_factory=set)
    seen_orgs: set = field(default_factory=set)
    credits_used: int = 0
    last_segment: str = ""


class FileLedger:
    """Local fallback when no sheet is configured."""

    def __init__(self, path: Path = LOCAL_LEDGER):
        self.path = path

    def _read(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"runs": [], "seen_ids": [], "seen_orgs": []}

    async def load(self) -> LedgerState:
        d = self._read()
        return LedgerState(
            seen_ids=set(d["seen_ids"]), seen_orgs=set(d["seen_orgs"]),
            credits_used=sum(r.get("credits_used", 0) for r in d["runs"]),
            last_segment=d["runs"][-1]["segment"] if d["runs"] else "")

    async def record(self, run: dict, rows: list[dict]):
        d = self._read()
        d["runs"].append(run)
        d["seen_ids"] = sorted(set(d["seen_ids"]) | set(run["seen_ids"]))
        d["seen_orgs"] = sorted(set(d["seen_orgs"])
                                | {r["company"].lower() for r in rows if r["company"]})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")

    async def push_angles(self, angles: dict[str, str]):
        pass  # the CSV is the record locally


class SheetLedger:
    """Google Sheet via its Apps Script web app (scripts/sheet_webhook.gs)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    async def _call(self, method: str, body: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            if method == "GET":
                r = await c.get(self.url, params={"token": self.token})
            else:
                r = await c.post(self.url, content=json.dumps(dict(body, token=self.token)),
                                 headers={"Content-Type": "text/plain"})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Sheet webhook: {data['error']}")
        return data

    async def load(self) -> LedgerState:
        d = await self._call("GET")
        return LedgerState(seen_ids=set(d.get("seen_ids", [])),
                           seen_orgs={o.lower() for o in d.get("seen_orgs", [])},
                           credits_used=int(d.get("credits_used", 0)),
                           last_segment=d.get("last_segment", ""))

    async def record(self, run: dict, rows: list[dict]):
        await self._call("POST", {"columns": ROW_COLUMNS, "rows": rows,
                                  "run": dict(run, seen_ids=",".join(run["seen_ids"]))})

    async def push_angles(self, angles: dict[str, str]):
        await self._call("POST", {"angles": angles})


def make_ledger():
    url = (os.getenv("SHEET_WEBHOOK_URL") or "").strip()
    if url:
        return SheetLedger(url, (os.getenv("SHEET_TOKEN") or "").strip())
    return FileLedger()


# ── The run ──────────────────────────────────────────────────────────


@dataclass
class FetchReport:
    segment: str = ""
    searched: int = 0
    candidates: int = 0
    enriched: int = 0
    credits_used: int = 0
    credits_left: int = 0
    rows: list[dict] = field(default_factory=list)
    csv_path: str = ""
    ledger: str = ""
    stopped: str = ""

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "rows"}
        d["prospects"] = len(self.rows)
        return d


async def _signals_for(state, company_ids: list[str]) -> dict[str, dict]:
    """Latest HIRING_ROLE / OPEN_ROLES / TECH_STACK per company."""
    import aiosqlite

    out: dict[str, dict] = {cid: {} for cid in company_ids}
    if not company_ids:
        return out
    ph = ",".join("?" for _ in company_ids)
    sql = f"""SELECT company_id, signal_code, value_num, value_text, observed_at
              FROM observations WHERE company_id IN ({ph})
                AND signal_code IN ('HIRING_ROLE', 'OPEN_ROLES', 'TECH_STACK')
              ORDER BY observed_at"""
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(sql, company_ids) as cur:
            for cid, code, num, text, _ in await cur.fetchall():
                sig = out[cid]
                if code == "HIRING_ROLE":
                    roles = sig.setdefault("hiring", [])
                    if text and (num is None or num > 0) and text not in roles:
                        roles.append(text)
                elif code == "OPEN_ROLES":
                    sig["open_roles"] = int(num or 0)
                else:
                    sig["tech"] = text or ""
    return out


async def run_fetch(state, config, segment: str = "auto", credits: int | None = None,
                    dry_run: bool = False) -> FetchReport:
    spec = load_segments()
    ledger = make_ledger()
    led = await ledger.load()
    seg = pick_segment(spec, segment, led.last_segment)
    cap = int(spec.get("phase_credit_cap", 0))
    budget = credits if credits is not None else int(spec.get("per_run_credits", 10))
    left = max(0, cap - led.credits_used)
    budget = min(budget, left)

    report = FetchReport(segment=seg["key"], credits_left=left,
                         ledger=type(ledger).__name__)
    if budget <= 0:
        report.stopped = f"credit cap reached ({led.credits_used}/{cap} used)"
        return report

    titles = seg.get("titles", [])
    async with ApolloClient(apollo_key()) as apollo:
        people: list[dict] = []
        for page in range(1, MAX_SEARCH_PAGES + 1):
            res = await apollo.search_people(search_filters(seg), page=page)
            batch = res.get("people") or []
            people.extend(batch)
            picks = choose_candidates(people, titles, led.seen_ids, led.seen_orgs, budget)
            if len(picks) >= budget or len(batch) < 100:
                break
        report.searched = len(people)
        picks = choose_candidates(people, titles, led.seen_ids, led.seen_orgs, budget)
        report.candidates = len(picks)
        if dry_run:
            report.rows = [{"first_name": p.get("first_name", ""), "title": p.get("title", ""),
                            "company": p["organization"]["name"]} for p in picks]
            return report

        enriched: list[tuple[dict, dict]] = []
        seen_now: list[str] = []
        for p in picks:
            if report.credits_used >= budget:
                break
            person = await apollo.enrich(p["id"])
            seen_now.append(p["id"])
            if person is None:
                continue
            # Charged when Apollo returns data: count it, conservatively.
            report.credits_used += 1
            report.enriched += 1
            enriched.append((p, person))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    rows, company_ids = [], []
    for p, person in enriched:
        org = person.get("organization") or {}
        domain = domain_of(org.get("website_url") or org.get("primary_domain") or "")
        location = ", ".join(x for x in (org.get("city"), org.get("state"),
                                         org.get("country")) if x)
        company_id = await state.add_company(Company(
            name=org.get("name") or p["organization"]["name"], domain=domain,
            website=org.get("website_url") or "", industry=org.get("industry") or "",
            company_size=str(org.get("estimated_num_employees") or ""),
            location=location, source="apollo",
            external_id=f"apollo:{org['id']}" if org.get("id") else "",
        ))
        company_ids.append(company_id)
        row = {
            "fetched_at": now, "segment": seg["key"], "apollo_id": p["id"],
            "first_name": person.get("first_name") or "",
            "last_name": person.get("last_name") or "",
            "title": person.get("title") or p.get("title") or "",
            "seniority": SENIORITY.get(person.get("seniority") or "", ""),
            "email": person.get("email") or "",
            "email_status": email_status(person.get("email_status")),
            "linkedin_url": person.get("linkedin_url") or "",
            "company": org.get("name") or p["organization"]["name"],
            "domain": domain, "industry": org.get("industry") or "",
            "employees": str(org.get("estimated_num_employees") or ""),
            "city": person.get("city") or "", "state": person.get("state") or "",
            "country": person.get("country") or "",
            "hiring_roles": "", "open_roles": "", "tech_stack": "",
            "score": 0, "angle": "", "status": "new", "_company_id": company_id,
        }
        rows.append(row)

    # Free: read each company's own site for hiring and tech signals.
    from harvey.signals import seed_signal_catalog
    await seed_signal_catalog(state)
    for code in spec.get("confirm_signals", []):
        await state.set_signal_status(code, "confirmed")
    targets = [{"id": r["_company_id"], "domain": r["domain"], "name": r["company"]}
               for r in rows if r["domain"]]
    if targets:
        from harvey.collectors.profile import profile_companies
        try:
            await profile_companies(state, targets, config)
        except Exception as e:  # signals are a bonus, never a reason to lose rows
            logger.warning("fetch: profiling failed: %s", e)
    signals = await _signals_for(state, company_ids)

    for row in rows:
        company_id = row.pop("_company_id")
        sig = signals.get(company_id, {})
        row["hiring_roles"] = "; ".join(sig.get("hiring", [])[:5])
        row["open_roles"] = str(sig.get("open_roles", "")) if "open_roles" in sig else ""
        row["tech_stack"] = sig.get("tech", "")
        row["score"] = score(row, seg)
        if row["email"]:
            await state.add_prospect(Prospect(
                first_name=row["first_name"], last_name=row["last_name"],
                email=row["email"], email_status=row["email_status"],
                email_verified=row["email_status"] == "verified",
                linkedin_url=row["linkedin_url"], title=row["title"],
                seniority=row["seniority"], source="apollo",
                source_url=row["linkedin_url"], score=row["score"],
                company=row["company"], industry=row["industry"],
                company_size=row["employees"], company_id=company_id,
            ))
    rows.sort(key=lambda r: r["score"], reverse=True)
    report.rows = rows

    EXPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    path = EXPORT_DIR / f"fetch-{stamp}-{seg['key']}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ROW_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    report.csv_path = str(path)

    run = {"ran_at": now, "segment": seg["key"], "searched": report.searched,
           "enriched": report.enriched, "credits_used": report.credits_used,
           "prospects": len(rows), "seen_ids": seen_now}
    await ledger.record(run, rows)
    report.credits_left = left - report.credits_used
    return report


async def push_angles(csv_path: str) -> int:
    """Send the ``angle`` column of a fetch CSV to the sheet, keyed by apollo_id."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        angles = {r["apollo_id"]: r["angle"] for r in csv.DictReader(f)
                  if r.get("apollo_id") and r.get("angle")}
    if angles:
        await make_ledger().push_angles(angles)
    return len(angles)

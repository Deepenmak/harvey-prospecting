"""FETCH: source decision-makers from Apollo on a budget, then profile them.

One run = one segment from segments.yaml, or every segment (``mix``):

1. Search Apollo (free) for people matching the segment.
2. Drop anyone already fetched or skipped (the ledger), keep one person per
   company, and rank by the segment's title order.
3. Enrich the top N (1 credit each when an email is found), never past
   ``max_credits_per_session`` in one run (there is no lifetime cap). With ``mix`` the credits go round
   the segments in turn, so one run covers every segment.
4. Store companies and prospects in Harvey's database, read each company's
   own site (free) for hiring and tech-stack signals, and score.
5. Fold the rows into the two contact files (exports/contacts_all.csv and
   exports/contacts_with_email.csv; no file per run), and append rows plus a
   run record to the Google Sheet.

Free search shows only a company's name, never its industry or size, so a
keyword tag alone lets misfits through. The judgement step sits between 2
and 3: ``--plan`` writes the candidates to exports/plan.csv, Claude marks each one
``a`` (strong fit), ``b`` (fit) or ``skip`` against optywise_icp.md, and
``--from-plan`` spends credits on the kept rows only. Skipped companies go
in the ledger so no later search offers them again.

The ledger is the Google Sheet when SHEET_WEBHOOK_URL is set (so a cloud
run with a fresh clone still knows what earlier runs spent and fetched),
and data/fetch_ledger.json otherwise.
"""

import csv
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from harvey.collectors.ats import Posting, matching_roles, role_keywords
from harvey.integrations.apollo import ApolloClient, apollo_key
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.fetch")

SEGMENTS_FILE = PROJECT_ROOT / "segments.yaml"
LOCAL_LEDGER = PROJECT_ROOT / "data" / "fetch_ledger.json"
EXPORT_DIR = PROJECT_ROOT / "exports"
# Two files, updated in place by every run: every contact ever fetched, and
# the same rows narrowed to those with an email. Best first in both.
CONTACTS_FILE = EXPORT_DIR / "contacts_all.csv"
EMAIL_CONTACTS_FILE = EXPORT_DIR / "contacts_with_email.csv"
# The current candidate plan, replaced by each --plan run.
PLAN_FILE = EXPORT_DIR / "plan.csv"
MAX_SEARCH_PAGES = 5
# A plan lists this many candidates per credit, so every skip has a stand-in.
PLAN_RESERVE = 3
PLAN_COLUMNS = ["segment", "apollo_id", "first_name", "title", "company",
                "title_rank", "verdict", "reason"]
# Plan verdicts that get a credit. "a" is a strong fit and scores higher.
KEEP_VERDICTS = ("a", "b")

ROW_COLUMNS = [
    "fetched_at", "segment", "apollo_id", "first_name", "last_name", "title",
    "seniority", "email", "email_status", "linkedin_url", "company", "domain",
    "industry", "employees", "city", "state", "country", "hiring_roles",
    "open_roles", "tech_stack", "score", "angle", "status",
]
# Judgement added to a contact after it's fetched: the plan reviewer's fit,
# web research, its fact check, a first-touch draft (never sent), and what
# the call-prep and proposal skills write once a lead turns into a meeting.
REVIEW_COLUMNS = ["fit", "fit_reason", "intent", "buying_state", "account_tier",
                  "signals", "red_flags", "verified", "verify_notes",
                  "draft_subject", "draft_body", "call_brief", "call_notes",
                  "deal_check", "proposal_draft"]
CONTACT_COLUMNS = ROW_COLUMNS + REVIEW_COLUMNS
_FIT_ORDER = {"a": 0, "b": 1, "skip": 3}
_INTENT_ORDER = {"high": 0, "medium": 1, "low": 2}
_CHECK_ORDER = {"pass": 0, "partial": 1, "fail": 2}
# The highest intent a buying state allows. A company that already solved the
# workflow isn't a hot lead however much AI news it has.
_INTENT_CAP = {"solved": "low", "builds_own": "low", "partly_solved": "medium"}

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


# Spellings of the same title, folded to one before comparing. Order matters:
# "Senior Vice President" becomes "senior vp", then "vp".
_TITLE_FOLD = tuple((re.compile(p, re.I), r) for p, r in (
    (r"\bvice[\s-]+president\b", "vp"),
    (r"\bsr\b\.?", "senior"),
    (r"\b(?:senior|executive|assistant|group)\s+vp\b|\b[sea]vp\b", "vp"),
    (r"\bchief\s+executive\s+officer\b", "ceo"),
    (r"\bchief\s+technology\s+officer\b", "cto"),
    (r"\bchief\s+information\s+officer\b", "cio"),
    (r"\bchief\s+operating\s+officer\b", "coo"),
    (r"\bartificial\s+intelligence\b", "ai"),
))
_TITLE_FILLER = frozenset({"of", "the", "and", "for", "a", "an", "to"})


def _title_words(text: str) -> list[str]:
    for pattern, replacement in _TITLE_FOLD:
        text = pattern.sub(replacement, text or "")
    return [w for w in re.findall(r"[a-z0-9+#]+", text.lower()) if w not in _TITLE_FILLER]


def title_priority(title: str, titles: list[str]) -> int:
    """Index of the first segment title found in ``title`` as a run of whole
    words: "VP of Claims" is "VP Claims", but "Head of Claims" isn't "Head of
    AI" and "Head Office Claims Advisor" isn't "Head of Claims"."""
    words = _title_words(title)
    for i, wanted in enumerate(titles):
        w = _title_words(wanted)
        if w and any(words[j:j + len(w)] == w for j in range(len(words) - len(w) + 1)):
            return i
    return len(titles)


def interleave(groups: list[list]) -> list:
    """Round-robin across groups: one from each in turn, so a budget spent
    from the front is shared evenly, and a short group's share goes to the rest."""
    out = []
    for i in range(max((len(g) for g in groups), default=0)):
        out.extend(g[i] for g in groups if i < len(g))
    return out


def plan_picks(rows: list[dict], order: list[str], seen_ids: set,
               seen_orgs: set) -> tuple[list[list[dict]], list[str]]:
    """Kept plan rows grouped by segment (in ``order``), strong fits ("a")
    first, and the skipped orgs. Rows with no verdict wait for review.
    """
    groups: dict[str, list[dict]] = {key: [] for key in order}
    skipped: list[str] = []
    for r in rows:
        verdict = (r.get("verdict") or "").strip().lower()
        org = (r.get("company") or "").strip()
        if verdict == "skip" and org:
            skipped.append(org.lower())
        elif (verdict in KEEP_VERDICTS and r.get("segment") in groups
              and r.get("apollo_id") not in seen_ids and org.lower() not in seen_orgs):
            groups[r["segment"]].append(r)
    for g in groups.values():
        g.sort(key=lambda r: r["verdict"].strip().lower() != "a")
    return [g for g in groups.values() if g], skipped


def relevant_roles(texts: list[str], keywords: list[str]) -> list[str]:
    """Hiring roles that name one of ``keywords`` (icp.hiring_signals).

    A careers page with no job board is read for any heading that looks like
    a job ("How to Apply", "Billing Representative"); for an Optywise lead only
    an AI or automation role is a buying signal.
    """
    if not keywords:
        return texts
    # Board rows read "Title · Department · Location": the title is first.
    return [t for t in texts
            if matching_roles([Posting(title=t.split(" · ")[0])], keywords)]


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


def score(row: dict, seg: dict, verdict: str = "") -> int:
    """Fit score, 1-100: title, deliverability, hiring and size, plus the
    plan reviewer's verdict ("a" = strong fit). The angle explains it."""
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
    # 200-2,000 staff spend $60-150K without a board vote and rarely have
    # their own ML team; the top of the range is a slower sale.
    if 200 <= emp <= 2000:
        s += 10
    elif 2000 < emp <= 5000:
        s += 5
    if verdict.strip().lower() == "a":
        s += 10
    return max(1, min(100, s))


# ── Contact files ────────────────────────────────────────────────────


def contact_rank(row: dict) -> tuple:
    """Best first: reviewer's fit, researched intent, fact check, then score."""
    try:
        s = int(row.get("score") or 0)
    except ValueError:
        s = 0
    return (_FIT_ORDER.get((row.get("fit") or "").lower(), 2),
            _INTENT_ORDER.get((row.get("intent") or "").lower(), 3),
            _CHECK_ORDER.get((row.get("verified") or "").lower(), 1), -s)


def capped_intent(intent: str, buying_state: str) -> str:
    """The researcher's intent, lowered to what the buying state allows."""
    cap = _INTENT_CAP.get((buying_state or "").strip().lower())
    if cap and _INTENT_ORDER.get((intent or "").strip().lower(), 3) < _INTENT_ORDER[cap]:
        return cap
    return intent


def account_tier(row: dict) -> str:
    """"A" marks an account worth 2-3 contacts: strong fit, high intent, an
    open need, and facts that held up. It's advice: fetch stays at one person
    per company, so it never spends a credit."""
    def get(k):
        return (row.get(k) or "").strip().lower()
    return "A" if (get("fit") == "a" and get("intent") == "high"
                   and get("buying_state") == "open"
                   and get("verified") in ("pass", "partial")) else ""


def read_contacts(path: Path = CONTACTS_FILE) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def merge_contacts(existing: list[dict], updates: list[dict],
                   add_new: bool = True) -> list[dict]:
    """Rows keyed by apollo_id. A non-empty value in an update replaces the
    stored one; an empty value never erases what's there."""
    by_id = {r["apollo_id"]: dict(r) for r in existing if r.get("apollo_id")}
    for u in updates:
        pid = u.get("apollo_id")
        if not pid or (pid not in by_id and not add_new):
            continue
        row = by_id.setdefault(pid, {c: "" for c in CONTACT_COLUMNS})
        for k in CONTACT_COLUMNS:
            v = u.get(k)
            if v not in (None, ""):
                row[k] = str(v)
    return sorted(by_id.values(), key=contact_rank)


def save_contacts(rows: list[dict], path: Path = CONTACTS_FILE,
                  email_path: Path = EMAIL_CONTACTS_FILE):
    """Write every contact, and the ones with an email, best first."""
    rows = sorted(({**r, "account_tier": account_tier(r)} for r in rows),
                  key=contact_rank)
    path.parent.mkdir(parents=True, exist_ok=True)
    for target, subset in ((path, rows), (email_path, [r for r in rows if r.get("email")])):
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CONTACT_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(subset)


def update_contacts(updates: list[dict], add_new: bool = False,
                    path: Path = CONTACTS_FILE,
                    email_path: Path = EMAIL_CONTACTS_FILE) -> int:
    """Fold judgement (fit, research, fact check, drafts) into the contact
    files. Returns how many stored contacts the updates matched."""
    existing = read_contacts(path)
    known = {r["apollo_id"] for r in existing}
    save_contacts(merge_contacts(existing, updates, add_new), path, email_path)
    return sum(1 for u in updates if u.get("apollo_id") in known)


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
        return {"runs": [], "seen_ids": [], "seen_orgs": [], "skipped_orgs": []}

    async def load(self) -> LedgerState:
        d = self._read()
        return LedgerState(
            seen_ids=set(d["seen_ids"]),
            seen_orgs=set(d["seen_orgs"]) | set(d.get("skipped_orgs", [])),
            credits_used=sum(r.get("credits_used", 0) for r in d["runs"]),
            last_segment=d["runs"][-1]["segment"] if d["runs"] else "")

    async def record(self, run: dict, rows: list[dict]):
        d = self._read()
        d["runs"].append(run)
        d["seen_ids"] = sorted(set(d["seen_ids"]) | set(run["seen_ids"]))
        d["seen_orgs"] = sorted(set(d["seen_orgs"])
                                | {r["company"].lower() for r in rows if r["company"]})
        d["skipped_orgs"] = sorted(set(d.get("skipped_orgs", []))
                                   | set(run.get("skipped_orgs", [])))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")

    async def push_angles(self, angles: dict[str, str]):
        pass  # the CSV is the record locally


class SheetLedger:
    """Google Sheet via its Apps Script web app (scripts/sheet_webhook.gs)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    async def _call(self, body: dict) -> dict:
        # The token rides in the POST body only, never the URL, so it can't
        # leak into logs or error messages.
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.post(self.url, content=json.dumps(dict(body, token=self.token)),
                             headers={"Content-Type": "text/plain"})
        if r.status_code != 200:
            hint = (" (is the web app deployed with access: Anyone?)"
                    if r.status_code in (401, 403) else "")
            raise RuntimeError(f"Sheet webhook returned HTTP {r.status_code}{hint}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("Sheet webhook did not return JSON (check the "
                               "deployment URL ends in /exec)") from None
        if data.get("error"):
            raise RuntimeError(f"Sheet webhook: {data['error']}")
        return data

    async def load(self) -> LedgerState:
        d = await self._call({"action": "load"})
        orgs = d.get("seen_orgs", []) + d.get("skipped_orgs", [])
        return LedgerState(seen_ids=set(d.get("seen_ids", [])),
                           seen_orgs={o.lower() for o in orgs},
                           credits_used=int(d.get("credits_used", 0)),
                           last_segment=d.get("last_segment", ""))

    async def record(self, run: dict, rows: list[dict]):
        # Company names hold commas ("Example Holdings, LLC"), so "|" joins them.
        await self._call({"columns": ROW_COLUMNS, "rows": rows,
                          "run": dict(run, seen_ids=",".join(run["seen_ids"]),
                                      skipped_orgs="|".join(run.get("skipped_orgs", [])))})

    async def push_angles(self, angles: dict[str, str]):
        await self._call({"angles": angles})


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
        # Which rows of the contact files this run added or refreshed.
        d["new_ids"] = [r["apollo_id"] for r in self.rows if r.get("apollo_id")]
        return d


async def _signals_for(state, company_ids: list[str],
                       keywords: list[str] | None = None) -> dict[str, dict]:
    """Latest HIRING_ROLE / OPEN_ROLES / TECH_STACK per company. Hiring roles
    are kept only when they name one of ``keywords``."""
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
    for sig in out.values():
        if "hiring" in sig:
            sig["hiring"] = relevant_roles(sig["hiring"], keywords or [])
    return out


async def _search(apollo, seg: dict, led: LedgerState, limit: int) -> tuple[list[dict], int]:
    """Up to ``limit`` new candidates for one segment, and how many people
    were searched. Pages on (free) until every pick has the segment's first
    title or the results run out, so title order beats Apollo's own order."""
    titles = seg.get("titles", [])
    people: list[dict] = []
    for page in range(1, MAX_SEARCH_PAGES + 1):
        res = await apollo.search_people(search_filters(seg), page=page)
        batch = res.get("people") or []
        people.extend(batch)
        picks = choose_candidates(people, titles, led.seen_ids, led.seen_orgs, limit)
        if len(batch) < 100 or (len(picks) >= limit
                                and all(p["_rank"] == 0 for p in picks)):
            break
    picks = choose_candidates(people, titles, led.seen_ids, led.seen_orgs, limit)
    return [dict(p, _segment=seg["key"]) for p in picks], len(people)


def write_plan(groups: list[list[dict]], path: Path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLUMNS)
        w.writeheader()
        for group in groups:
            for p in group:
                w.writerow({"segment": p["_segment"], "apollo_id": p["id"],
                            "first_name": p.get("first_name", ""),
                            "title": p.get("title", ""),
                            "company": p["organization"]["name"],
                            "title_rank": p["_rank"], "verdict": "", "reason": ""})


async def run_fetch(state, config, segment: str = "auto", credits: int | None = None,
                    dry_run: bool = False, plan: bool = False,
                    from_plan: str = "") -> FetchReport:
    """One fetch run. ``segment`` is a key, ``auto`` (the next in rotation) or
    ``mix`` (all of them, credits shared in turn). ``plan`` writes candidates
    for review and spends nothing; ``from_plan`` enriches a reviewed plan."""
    spec = load_segments()
    ledger = make_ledger()
    if isinstance(ledger, FileLedger) and os.getenv("GITHUB_ACTIONS"):
        # A CI checkout is fresh every run: a local ledger would forget what
        # was spent and fetched, so the credit cap would mean nothing.
        return FetchReport(segment=segment, ledger="none",
                           stopped="SHEET_WEBHOOK_URL is not set; refusing to "
                                   "spend credits without the sheet ledger")
    if (isinstance(ledger, FileLedger) and os.getenv("CLAUDE_CODE_REMOTE")
            and not ledger.path.exists()):
        # A Claude cloud run starts from a fresh clone too, but it can bring
        # the ledger back from the leads dashboard before fetching.
        return FetchReport(segment=segment, ledger="none",
                           stopped="no ledger in this cloud checkout; restore it "
                                   "from the leads dashboard first "
                                   "(dashboard_sync.py restore)")
    led = await ledger.load()
    by_key = {s["key"]: s for s in spec["segments"]}
    segs = (spec["segments"] if segment == "mix"
            else [pick_segment(spec, segment, led.last_segment)])
    # No lifetime cap: each session (one run) may spend at most
    # max_credits_per_session, whatever was asked for.
    session_max = int(spec.get("max_credits_per_session", 25))
    budget = credits if credits is not None else int(spec.get("per_run_credits", 10))
    budget = min(budget, session_max)
    left = budget

    report = FetchReport(segment="mix" if len(segs) > 1 else segs[0]["key"],
                         credits_left=left, ledger=type(ledger).__name__)
    if budget <= 0:
        report.stopped = f"session credit limit is {session_max}; nothing to spend"
        return report

    skipped: list[str] = []
    if from_plan:
        with open(from_plan, newline="", encoding="utf-8") as f:
            groups, skipped = plan_picks(list(csv.DictReader(f)), list(by_key),
                                         led.seen_ids, led.seen_orgs)
        if not groups:
            report.stopped = f"no rows marked {' or '.join(KEEP_VERDICTS)} in {from_plan}"
            return report
        report.segment = "mix" if len(groups) > 1 else groups[0][0]["segment"]
        # Plan rows carry what enrichment needs, in the search result's shape.
        groups = [[{"id": r["apollo_id"], "title": r["title"], "_segment": r["segment"],
                    "_verdict": r["verdict"].strip().lower(),
                    "_reason": (r.get("reason") or "").strip(),
                    "organization": {"name": r["company"]}} for r in g] for g in groups]

    async with ApolloClient(apollo_key()) as apollo:
        if not from_plan:
            # A plan lists reserves for each segment's share; a run needs
            # enough per segment to cover the whole budget if others run dry.
            share = math.ceil(budget / len(segs))
            limit = share * PLAN_RESERVE if plan else budget
            groups = []
            for seg in segs:
                picks, searched = await _search(apollo, seg, led, limit)
                report.searched += searched
                groups.append(picks)
        picks = interleave(groups)
        report.candidates = len(picks)

        if plan:
            EXPORT_DIR.mkdir(exist_ok=True)
            write_plan(groups, PLAN_FILE)
            report.csv_path = str(PLAN_FILE)
            report.rows = [{"segment": p["_segment"], "title": p.get("title", ""),
                            "company": p["organization"]["name"]}
                           for g in groups for p in g]
            return report
        if dry_run:
            report.rows = [{"segment": p["_segment"], "first_name": p.get("first_name", ""),
                            "title": p.get("title", ""),
                            "company": p["organization"]["name"]} for p in picks[:budget]]
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
        seg = by_key[p["_segment"]]
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
            "fit": p.get("_verdict", ""), "fit_reason": p.get("_reason", ""),
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
    signals = await _signals_for(state, company_ids,
                                 role_keywords(getattr(config, "icp", None)))

    for row in rows:
        company_id = row.pop("_company_id")
        sig = signals.get(company_id, {})
        row["hiring_roles"] = "; ".join(sig.get("hiring", [])[:5])
        row["open_roles"] = str(sig.get("open_roles", "")) if "open_roles" in sig else ""
        row["tech_stack"] = sig.get("tech", "")
        row["score"] = score(row, by_key[row["segment"]], row["fit"])
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

    save_contacts(merge_contacts(read_contacts(), rows))
    report.csv_path = str(CONTACTS_FILE)

    run = {"ran_at": now, "segment": report.segment, "searched": report.searched,
           "enriched": report.enriched, "credits_used": report.credits_used,
           "prospects": len(rows), "seen_ids": seen_now,
           "skipped_orgs": sorted(set(skipped))}
    await ledger.record(run, rows)
    report.credits_left = left - report.credits_used
    return report


async def push_angles(csv_path: str = str(CONTACTS_FILE)) -> tuple[int, bool]:
    """Send the ``angle`` column of the contacts file to the sheet, keyed by
    apollo_id. When that's contacts_all.csv, first re-rank it and rewrite
    contacts_with_email.csv from it, so hand edits reach both files.
    Returns (angles found, whether a sheet received them)."""
    if Path(csv_path).resolve() == CONTACTS_FILE.resolve():
        save_contacts(read_contacts())
    with open(csv_path, newline="", encoding="utf-8") as f:
        angles = {r["apollo_id"]: r["angle"] for r in csv.DictReader(f)
                  if r.get("apollo_id") and r.get("angle")}
    ledger = make_ledger()
    if angles:
        await ledger.push_angles(angles)
    return len(angles), isinstance(ledger, SheetLedger) and bool(angles)

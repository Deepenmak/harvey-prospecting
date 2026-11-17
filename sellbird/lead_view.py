"""Everything Sellbird knows about a lead, in one row.

A lead's story is spread across three places. Apollo's record
(``apollo_contacts``) holds who they are, what they cost and every judgement
a skill has written since -- the reviewer's fit, the researcher's intent and
buying state, the fact check, the angle, the first-touch draft. The pipeline
(``prospects``) holds whether they were ever contacted. The conversation and
the outbox hold what happened next.

Read one of those alone and you get a partial answer: the Contacts tab used
to show name, company, email, score and status, which is the thinnest slice
of what the pipeline actually collected. This module joins them, so one row
answers "who is this, why would they buy, and where do they stand".

The join is the email address, lowercased, because that is the only field
both sides are sure of -- ``prospects`` has no Apollo id. A contact Apollo
returned that was never promoted still appears, marked as fetched and not in
the pipeline; a prospect from somewhere else (the web Scout, an import)
appears with its judgement columns empty rather than being dropped.

Nothing here spends a credit or calls Apollo. It is a read.
"""

from __future__ import annotations

import logging
import re

from sellbird.apollo_store import ApolloStore
from sellbird.fetch import CONTACT_COLUMNS, capped_intent, contact_rank

logger = logging.getLogger("sellbird.lead_view")

# The reviewer's fit, in the words the dashboard shows. "skip" is kept and
# shown rather than hidden: knowing a company was ruled out, and why, is what
# stops it being fetched again.
FIT_LABELS = {"a": "Strong", "b": "Fit", "skip": "Ruled out"}
FIT_TONES = {"a": "good", "b": "waiting", "skip": "bad"}
INTENT_LABELS = {"high": "High", "medium": "Medium", "low": "Low",
                 "unknown": "Unknown"}
INTENT_TONES = {"high": "good", "medium": "waiting", "low": "bad",
                "unknown": "idle"}
# What the fact-checker made of the research behind a lead.
CHECK_LABELS = {"pass": "Held up", "partial": "Partly held up",
                "fail": "Did not hold up"}
CHECK_TONES = {"pass": "good", "partial": "waiting", "fail": "bad"}
BUYING_LABELS = {"open": "Open need", "partly_solved": "Partly solved",
                 "solved": "Already solved", "builds_own": "Builds its own",
                 "unknown": "Unknown"}

# A row that is in Apollo's record but not in the pipeline. Not a prospect
# status: nothing writes it to the database.
NOT_IN_PIPELINE = "fetched"

# The outbox statuses that mean an email is still coming.
_QUEUED = ("pending_review", "approved", "scheduled", "sending")


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def _key(email) -> str:
    return _text(email).strip().lower()


def _lower(value) -> str:
    return _text(value).strip().lower()


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def fit_label(fit) -> str:
    return FIT_LABELS.get(_lower(fit), "Unreviewed" if not _lower(fit) else _text(fit))


def intent_label(intent) -> str:
    return INTENT_LABELS.get(_lower(intent),
                             "Not researched" if not _lower(intent) else _text(intent))


def check_label(verified) -> str:
    return CHECK_LABELS.get(_lower(verified), "")


def buying_label(state) -> str:
    return BUYING_LABELS.get(_lower(state), _text(state))


def short_segment(key: str, label: str = "") -> str:
    """A segment name narrow enough for a table cell.

    segments.yaml labels are sentences ("US P&C and specialty carriers, MGAs,
    TPAs and insurtechs") because they also brief the model. A column needs
    "US insurance", so one is built from the key, which is already short.
    """
    parts = [p for p in re.split(r"[_\-\s]+", _text(key)) if p]
    if not parts:
        return _text(label)
    words = [parts[0].upper() if len(parts[0]) <= 3 else parts[0].capitalize()]
    words += [p.upper() if len(p) <= 3 else p.lower() for p in parts[1:]]
    return " ".join(words)


def _location(row: dict) -> str:
    parts = [_text(row.get(k)).strip() for k in ("city", "state", "country")]
    return ", ".join(p for p in parts if p)


class LeadStore(ApolloStore):
    """Apollo's record, joined to the pipeline around it.

    It extends ``ApolloStore`` rather than wrapping it because the lead view
    needs both sides of the same database: the contacts and credits Apollo's
    record holds, and the prospects, conversations and outbox rows the rest of
    Sellbird writes.
    """

    async def prospect_rows(self) -> list[dict]:
        return await self._rows(
            "SELECT id, first_name, last_name, email, email_verified, email_status,"
            " phone, phone_verified, linkedin_url, title, seniority, company,"
            " company_size, industry, source, status, score,"
            " personalization_notes, created_at, updated_at, timezone"
            " FROM prospects ORDER BY created_at")

    async def stages(self) -> dict[str, dict]:
        """The newest conversation per prospect: how far the talk has got."""
        out: dict[str, dict] = {}
        for row in await self._rows(
                "SELECT prospect_id, stage, status, intent, updated_at"
                " FROM conversations WHERE prospect_id != ''"
                " ORDER BY updated_at, created_at"):
            out[row["prospect_id"]] = row    # last row read is the newest
        return out

    async def touches(self) -> dict[str, dict]:
        """When each prospect was last emailed, and when the next one is due."""
        placeholders = ",".join("?" for _ in _QUEUED)
        return {row["prospect_id"]: row for row in await self._rows(
            "SELECT prospect_id,"
            " MAX(CASE WHEN status = 'sent' THEN COALESCE(sent_at, send_at) END)"
            "   AS last_touch,"
            f" MIN(CASE WHEN status IN ({placeholders}) THEN send_at END)"
            "   AS next_send,"
            " SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count"
            " FROM outbox WHERE prospect_id != '' GROUP BY prospect_id", _QUEUED)}

    async def undone_opt_outs(self) -> set[str]:
        """Prospects whose opt-out a human undid, so the panel stays reachable."""
        return {row["pid"] for row in await self._rows(
            "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid"
            " FROM actions WHERE action_type = 'opt_out_undone'"
            " AND json_valid(details_json)") if row["pid"]}

    async def leads(self, segment_labels: dict[str, str] | None = None) -> list[dict]:
        """Every lead, best first: fit, then intent, then the fact check, then score."""
        contacts = await self.contacts()
        prospects = await self.prospect_rows()
        stages = await self.stages()
        touches = await self.touches()
        undone = await self.undone_opt_outs()

        by_email: dict[str, dict] = {}
        for prospect in prospects:
            key = _key(prospect.get("email"))
            if key:
                by_email.setdefault(key, prospect)

        rows: list[dict] = []
        claimed: set[str] = set()
        for contact in contacts:
            prospect = by_email.get(_key(contact.get("email")))
            if prospect:
                claimed.add(prospect["id"])
            rows.append(_row(contact, prospect, stages, touches, undone,
                             segment_labels))
        for prospect in prospects:
            if prospect["id"] in claimed:
                continue
            rows.append(_row({}, prospect, stages, touches, undone, segment_labels))
        rows.sort(key=contact_rank)
        return rows


def _row(contact: dict, prospect: dict | None, stages: dict, touches: dict,
         undone: set[str], segment_labels: dict[str, str] | None) -> dict:
    """One lead: Apollo's row, what the pipeline added, and plain labels."""
    row = {column: _text(contact.get(column)) for column in CONTACT_COLUMNS}
    person = prospect or {}
    # Apollo's answer wins where it has one; the pipeline fills the gaps,
    # because a contact imported or scouted elsewhere has no Apollo row at all.
    for column, field in (("first_name", "first_name"), ("last_name", "last_name"),
                          ("title", "title"), ("seniority", "seniority"),
                          ("company", "company"), ("industry", "industry"),
                          ("email", "email"), ("email_status", "email_status"),
                          ("linkedin_url", "linkedin_url")):
        if not row.get(column) and person.get(field):
            row[column] = _text(person[field])
    if not row.get("employees") and person.get("company_size"):
        row["employees"] = _text(person["company_size"])

    # The researcher's intent, never read higher than the buying state allows.
    intent = capped_intent(row.get("intent", ""), row.get("buying_state", ""))
    row["intent"] = intent

    stage = stages.get(person.get("id") or "", {})
    touch = touches.get(person.get("id") or "", {})
    segment = row.get("segment", "")
    row.update({
        "apollo_id": _text(contact.get("apollo_id")),
        "run_id": _text(contact.get("run_id")),
        "id": _text(person.get("id")),
        "in_pipeline": bool(person),
        "name": " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p),
        "location": _location(row),
        "score": _int(row.get("score")) or _int(person.get("score")),
        "status": _text(person.get("status")) or NOT_IN_PIPELINE,
        "source": _text(person.get("source")) or ("apollo" if contact else ""),
        "segment_label": (segment_labels or {}).get(segment, segment),
        "segment_short": short_segment(segment,
                                       (segment_labels or {}).get(segment, "")),
        "fit_label": fit_label(row.get("fit")),
        "fit_tone": FIT_TONES.get(_lower(row.get("fit")), "idle"),
        "intent_label": intent_label(intent),
        "intent_tone": INTENT_TONES.get(_lower(intent), "idle"),
        "check_label": check_label(row.get("verified")),
        "check_tone": CHECK_TONES.get(_lower(row.get("verified")), "idle"),
        "buying_label": buying_label(row.get("buying_state")),
        "notes": _text(person.get("personalization_notes")),
        "phone": _text(person.get("phone")),
        "phone_verified": _int(person.get("phone_verified")),
        "email_verified": _int(person.get("email_verified")),
        "created_at": _text(person.get("created_at")) or _text(row.get("fetched_at")),
        "stage": _text(stage.get("stage")),
        "conversation_status": _text(stage.get("status")),
        "last_touch": _text(touch.get("last_touch")),
        "next_send": _text(touch.get("next_send")),
        "sent_count": _int(touch.get("sent_count")),
        "opt_out_undone": _text(person.get("id")) in undone,
    })
    return row


def summary(rows: list[dict], credits_used: int = 0) -> dict:
    """The figures above the table. Every one of them is a count of rows
    below it, so a reader can check any of them by filtering."""
    def count(test) -> int:
        return sum(1 for row in rows if test(row))
    return {
        "contacts": len(rows),
        "with_email": count(lambda r: r.get("email")),
        # An address is only verified if there is an address: a row whose
        # email never came back can carry a stale status from an earlier
        # merge, and counting it would overstate what is sendable.
        "verified": count(lambda r: r.get("email")
                          and _lower(r.get("email_status")) == "verified"),
        "strong": count(lambda r: _lower(r.get("fit")) == "a"),
        "ruled_out": count(lambda r: _lower(r.get("fit")) == "skip"),
        "researched": count(lambda r: _lower(r.get("intent"))),
        "intent": count(lambda r: _lower(r.get("intent")) in ("high", "medium")),
        "checked": count(lambda r: _lower(r.get("verified")) in ("pass", "partial")),
        "drafted": count(lambda r: r.get("draft_body")),
        "in_pipeline": count(lambda r: r.get("in_pipeline")),
        "candidates": count(lambda r: not r.get("in_pipeline")),
        "credits_used": _int(credits_used),
    }


def segments(rows: list[dict], spec: dict | None = None) -> list[dict]:
    """Contacts per sourcing segment, and how many of them are worth having.

    Driven by the rows rather than by ``segment_counts()`` so the strong-fit
    and intent tallies come from the same data the table shows.
    """
    order: list[str] = []
    labels: dict[str, str] = {}
    for entry in (spec or {}).get("segments") or []:
        key = _text(entry.get("key"))
        if key:
            order.append(key)
            labels[key] = _text(entry.get("label")) or key
    for row in rows:
        key = _text(row.get("segment"))
        if key and key not in order:
            order.append(key)

    out = []
    for key in order:
        in_segment = [r for r in rows if _text(r.get("segment")) == key]
        if not in_segment and key not in labels:
            continue
        out.append({
            "key": key,
            "label": labels.get(key, key),
            "contacts": len(in_segment),
            "with_email": sum(1 for r in in_segment if r.get("email")),
            "strong": sum(1 for r in in_segment if _lower(r.get("fit")) == "a"),
            "intent": sum(1 for r in in_segment
                          if _lower(r.get("intent")) in ("high", "medium")),
        })
    return out


def segment_labels(spec: dict | None) -> dict[str, str]:
    return {_text(s.get("key")): _text(s.get("label")) or _text(s.get("key"))
            for s in (spec or {}).get("segments") or [] if s.get("key")}

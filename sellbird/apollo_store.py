"""Apollo's record, in the database.

Every contact Apollo has ever returned, every run that returned it, and every
person and company already spoken for, in three tables inside
``data/sellbird.db``.

Why this exists. The fetch ledger used to be ``data/fetch_ledger.json`` and
the contacts used to be ``exports/contacts_all.csv``, both rewritten whole on
every run. Two things went wrong with that. A deployment whose volume was
replaced came back with no memory of which Apollo people had already been
paid for, so the next free search offered them again and the next enrichment
charged for them again. And a container stopped mid-write left a truncated
file, which is the same loss by a shorter route.

So the database is the record. It is the one thing that is migrated, mounted
on the persistent volume and carried through a redeploy with everything else.
The two CSVs and the JSON ledger are still written -- the lead-gen skills and
the dashboard read them -- but they are now **derived**: if they go missing or
come back thinner than the database, ``sellbird.fetch.sync_exports`` rebuilds
them from here.

The split between columns and ``row_json`` is deliberate. The fields the UI
filters and sorts on are columns; the whole row is kept verbatim in
``row_json``, so the judgement columns a skill adds later (fit, verified,
call_brief, proposal_draft -- the list has grown four times) need no
migration. Same reasoning as ``observations``: facts are rows, not columns.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from sellbird.state import StateManager

logger = logging.getLogger("sellbird.apollo_store")

# apollo_seen.kind: an Apollo person id, or a company name lowercased.
KINDS = ("person", "org")
# Why something is in apollo_seen. "fetched" means a credit was spent on it;
# "missed" means Apollo was asked and had nobody, which costs nothing;
# "skipped" means a reviewer ruled it out; "staged" means it is waiting on
# Imports. Any of them stops a later search offering it again, but only
# "fetched" means "we already own this", which is what the duplicate check
# acts on.
REASONS = ("fetched", "missed", "skipped", "staged")


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ", "seconds")


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _text(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value)


def _public(row: dict) -> dict:
    """The row as it belongs in a CSV: no private plumbing keys."""
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


class ApolloStore(StateManager):
    """The ``apollo_runs``, ``apollo_contacts`` and ``apollo_seen`` tables."""

    async def _rows(self, query: str, args: tuple = ()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _scalar(self, query: str, args: tuple = (), default=0):
        async with self._connect() as db:
            async with db.execute(query, args) as cur:
                row = await cur.fetchone()
        return default if row is None or row[0] is None else row[0]

    # -- Runs --

    async def record_run(self, run: dict) -> str:
        """One row per Apollo run, including the free ones. Returns its id."""
        run_id = uuid.uuid4().hex
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO apollo_runs
                   (id, ran_at, segment, mode, searched, candidates, enriched,
                    credits_used, contacts, stopped, ledger)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, _text(run.get("ran_at")) or _now(),
                 _text(run.get("segment")), _text(run.get("mode")) or "fetch",
                 _int(run.get("searched")), _int(run.get("candidates")),
                 _int(run.get("enriched")), _int(run.get("credits_used")),
                 _int(run.get("prospects") or run.get("contacts")),
                 _text(run.get("stopped")), _text(run.get("ledger"))))
            await db.commit()
        return run_id

    async def runs(self, limit: int = 50) -> list[dict]:
        return await self._rows(
            "SELECT * FROM apollo_runs ORDER BY ran_at DESC, created_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),))

    async def run_count(self) -> int:
        return _int(await self._scalar("SELECT COUNT(*) FROM apollo_runs"))

    async def credits_used(self) -> int:
        """Every credit this workspace has ever spent through fetch."""
        return _int(await self._scalar("SELECT SUM(credits_used) FROM apollo_runs"))

    async def last_segment(self) -> str:
        """The segment the last paid run used, for the auto rotation.

        A free search never moves the rotation -- looking is not buying --
        but a run adopted from an older install does, or the rotation would
        start again at the first segment after an upgrade and fetch the one
        segment twice.
        """
        return _text(await self._scalar(
            "SELECT segment FROM apollo_runs WHERE segment != ''"
            " AND mode IN ('fetch', 'adopted')"
            " ORDER BY ran_at DESC, created_at DESC LIMIT 1", (), ""))

    # -- Contacts --

    async def upsert_contacts(self, run_id: str, rows: list[dict]) -> int:
        """Store contacts, merging field by field.

        A non-empty incoming value replaces the stored one; an empty value
        never erases what is there. That is exactly ``fetch.merge_contacts``'s
        rule, so a row can travel out to the files and back without losing
        the judgement written on the way.
        """
        rows = [r for r in rows if r.get("apollo_id")]
        if not rows:
            return 0
        ids = [_text(r["apollo_id"]) for r in rows]
        stored: dict[str, dict] = {}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            for row in await self._rows(
                    "SELECT apollo_id, run_id, row_json FROM apollo_contacts"
                    f" WHERE apollo_id IN ({placeholders})", tuple(chunk)):
                stored[row["apollo_id"]] = row

        now = _now()
        payload = []
        for row in rows:
            apollo_id = _text(row["apollo_id"])
            was = stored.get(apollo_id)
            merged: dict = {}
            if was:
                try:
                    merged = json.loads(was["row_json"]) or {}
                except (TypeError, ValueError):
                    merged = {}
            for key, value in _public(row).items():
                if value not in (None, ""):
                    merged[key] = value
            merged["apollo_id"] = apollo_id
            # A run id is only ever set, never replaced: a later merge (a
            # skill folding judgement back in) must not reattribute the
            # contact to a run that never fetched it.
            keep_run = (was or {}).get("run_id") or _text(run_id)
            payload.append((
                apollo_id, keep_run, _text(merged.get("segment")),
                _text(merged.get("fetched_at")) or now,
                _text(merged.get("first_name")), _text(merged.get("last_name")),
                _text(merged.get("title")), _text(merged.get("email")),
                _text(merged.get("email_status")), _text(merged.get("company")),
                _text(merged.get("domain")), _int(merged.get("score")),
                _text(merged.get("status")) or "new",
                json.dumps(merged, ensure_ascii=False), now))

        async with self._connect() as db:
            await db.executemany(
                """INSERT INTO apollo_contacts
                     (apollo_id, run_id, segment, fetched_at, first_name, last_name,
                      title, email, email_status, company, domain, score, status,
                      row_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(apollo_id) DO UPDATE SET
                     run_id = excluded.run_id, segment = excluded.segment,
                     fetched_at = excluded.fetched_at,
                     first_name = excluded.first_name, last_name = excluded.last_name,
                     title = excluded.title, email = excluded.email,
                     email_status = excluded.email_status, company = excluded.company,
                     domain = excluded.domain, score = excluded.score,
                     status = excluded.status, row_json = excluded.row_json,
                     updated_at = excluded.updated_at""", payload)
            await db.commit()
        return len(payload)

    async def contacts(self, segment: str = "", limit: int = 0) -> list[dict]:
        """Every stored contact, best first, in the contact-file shape."""
        query = "SELECT apollo_id, run_id, row_json FROM apollo_contacts"
        args: tuple = ()
        if segment:
            query += " WHERE segment = ?"
            args = (segment,)
        query += " ORDER BY score DESC"
        if limit:
            query += " LIMIT ?"
            args = args + (max(1, int(limit)),)
        out = []
        for row in await self._rows(query, args):
            try:
                data = json.loads(row["row_json"]) or {}
            except (TypeError, ValueError):
                data = {}
            data["apollo_id"] = row["apollo_id"]
            out.append(data)
        from sellbird.fetch import contact_rank  # late: fetch imports this module
        return sorted(out, key=contact_rank)

    async def contact_count(self) -> int:
        return _int(await self._scalar("SELECT COUNT(*) FROM apollo_contacts"))

    async def with_email_count(self) -> int:
        return _int(await self._scalar(
            "SELECT COUNT(*) FROM apollo_contacts WHERE email != ''"))

    # -- The ledger proper --

    async def mark_seen(self, kind: str, values, reason: str = "fetched",
                        run_id: str = "") -> int:
        """Record people and companies that must never be offered again.

        ``INSERT OR IGNORE``: the first reason a thing was seen is the one
        that stands, so a later free search cannot downgrade "we paid for
        this person" to "we merely looked at them".
        """
        if kind not in KINDS:
            raise ValueError(f"not a seen kind: {kind!r}")
        if reason not in REASONS:
            raise ValueError(f"not a seen reason: {reason!r}")
        clean = sorted({_text(v).strip().lower() if kind == "org" else _text(v).strip()
                        for v in (values or []) if _text(v).strip()})
        if not clean:
            return 0
        async with self._connect() as db:
            await db.executemany(
                "INSERT OR IGNORE INTO apollo_seen (kind, value, reason, run_id)"
                " VALUES (?, ?, ?, ?)",
                [(kind, value, reason, _text(run_id)) for value in clean])
            await db.commit()
        return len(clean)

    async def seen(self) -> tuple[set[str], set[str]]:
        """(Apollo person ids, lowercased company names) already spoken for."""
        people: set[str] = set()
        orgs: set[str] = set()
        for row in await self._rows("SELECT kind, value FROM apollo_seen"):
            (people if row["kind"] == "person" else orgs).add(row["value"])
        return people, orgs

    async def seen_count(self) -> int:
        return _int(await self._scalar("SELECT COUNT(*) FROM apollo_seen"))

    async def segment_counts(self) -> dict[str, dict]:
        """How many contacts each segment has produced, and how many are
        emailable. The per-segment tally the Apollo tab shows."""
        out: dict[str, dict] = {}
        for row in await self._rows(
                "SELECT segment, COUNT(*) AS contacts,"
                " SUM(CASE WHEN email != '' THEN 1 ELSE 0 END) AS with_email"
                " FROM apollo_contacts GROUP BY segment"):
            out[_text(row["segment"])] = {"contacts": _int(row["contacts"]),
                                          "with_email": _int(row["with_email"])}
        return out

    async def summary(self) -> dict:
        """What the dashboard shows above the Apollo ledger."""
        people, orgs = await self.seen()
        return {"runs": await self.run_count(),
                "contacts": await self.contact_count(),
                "with_email": await self.with_email_count(),
                "credits_used": await self.credits_used(),
                "people_seen": len(people), "companies_seen": len(orgs),
                "last_segment": await self.last_segment()}

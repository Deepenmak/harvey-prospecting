"""The staging tables: a contact list that has arrived but cost nothing yet.

A file from outside — a CSV someone exported, a Sellbird export, the free
half of a scheduled Apollo search — lands in ``import_batches`` and
``import_rows`` and stops there. Nothing reaches ``prospects``, nothing is
enriched and no credit moves until a human picks a route and approves it.
That is the same discipline the outbox applies to sending, for the same
reason: the expensive, irreversible step belongs to a person.

Every column name written here is fixed and checked against a whitelist, so
a field name that came from a request body can never reach SQL.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from sellbird.state import StateManager

logger = logging.getLogger("sellbird.imports")

# A batch's life. "analysing" is Claude reading the file; "review" is waiting
# for the user; "enriching" is a run in flight; a batch ends "imported" (its
# rows are prospects) or "discarded".
BATCH_STATUSES = ("analysing", "review", "enriching", "imported", "discarded")
# A row's life. Only "enriched" and "staged" rows can be promoted, and only
# "staged" rows can be enriched, so a retry can never spend twice.
ROW_STATUSES = ("staged", "enriched", "imported", "duplicate", "skipped", "failed")

# The contact fields a staged row carries, in the shape the rest of Sellbird
# already speaks (sellbird.fetch.CONTACT_COLUMNS uses the same names).
ROW_FIELDS = ("email", "first_name", "last_name", "company", "domain", "title",
              "linkedin_url")

_BATCH_COLUMNS = frozenset({"name", "source", "status", "mapping_json",
                            "analysis_json", "credits_spent", "error"})
_ROW_COLUMNS = frozenset({*ROW_FIELDS, "status", "route", "credits", "apollo_id",
                          "email_status", "enriched_json", "error", "prospect_id",
                          "raw_json"})

MAX_FIELD = 500


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _clean(value) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return " ".join(str(value).split())[:MAX_FIELD]


class ImportStore(StateManager):
    """The `import_batches` and `import_rows` tables."""

    async def _rows(self, query: str, args: tuple = ()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, args) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def _one(self, query: str, args: tuple) -> dict | None:
        rows = await self._rows(query, args)
        return rows[0] if rows else None

    # ── Batches ──

    async def create_batch(self, name: str, source: str = "csv",
                           status: str = "analysing") -> str:
        batch_id = uuid.uuid4().hex
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO import_batches (id, name, source, status) VALUES (?, ?, ?, ?)",
                (batch_id, _clean(name)[:200], _clean(source)[:40],
                 status if status in BATCH_STATUSES else "analysing"))
            await db.commit()
        return batch_id

    async def update_batch(self, batch_id: str, **fields) -> None:
        unknown = set(fields) - _BATCH_COLUMNS
        if unknown:
            raise ValueError(f"not a batch column: {', '.join(sorted(unknown))}")
        if not fields:
            return
        clause = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE import_batches SET {clause}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), batch_id))
            await db.commit()

    async def add_credits(self, batch_id: str, credits: int) -> None:
        """Record what a run actually spent. Never decreases."""
        if credits <= 0:
            return
        async with self._connect() as db:
            await db.execute(
                "UPDATE import_batches SET credits_spent = credits_spent + ?, "
                "updated_at = ? WHERE id = ?", (int(credits), _now(), batch_id))
            await db.commit()

    async def get_batch(self, batch_id: str) -> dict | None:
        batch = await self._one("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        if batch is None:
            return None
        batch["counts"] = await self.batch_counts(batch_id)
        for key in ("mapping_json", "analysis_json"):
            try:
                batch[key[:-5]] = json.loads(batch.get(key) or "{}")
            except (json.JSONDecodeError, TypeError):
                batch[key[:-5]] = {}
        return batch

    async def list_batches(self, limit: int = 50) -> list[dict]:
        batches = await self._rows(
            "SELECT id, name, source, status, credits_spent, error, created_at, updated_at "
            "FROM import_batches ORDER BY created_at DESC LIMIT ?", (int(limit),))
        for batch in batches:
            batch["counts"] = await self.batch_counts(batch["id"])
        return batches

    async def batch_counts(self, batch_id: str) -> dict[str, int]:
        rows = await self._rows(
            "SELECT status, COUNT(*) AS n FROM import_rows WHERE batch_id = ? "
            "GROUP BY status", (batch_id,))
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    async def delete_batch(self, batch_id: str) -> bool:
        async with self._connect() as db:
            await db.execute("DELETE FROM import_rows WHERE batch_id = ?", (batch_id,))
            cursor = await db.execute("DELETE FROM import_batches WHERE id = ?", (batch_id,))
            await db.commit()
            return cursor.rowcount > 0

    # ── Rows ──

    async def add_rows(self, batch_id: str, rows: list[dict]) -> int:
        """Stage rows. A repeated address inside one batch is dropped by the
        unique index rather than staged twice. Returns how many were kept."""
        kept = 0
        async with self._connect() as db:
            for row in rows:
                values = {f: _clean(row.get(f)) for f in ROW_FIELDS}
                values["email"] = values["email"].lower()
                values["domain"] = values["domain"].lower()
                # A candidate from Apollo's own search arrives with its id and
                # no address: the id is what later buys the address.
                cursor = await db.execute(
                    """INSERT OR IGNORE INTO import_rows
                       (id, batch_id, raw_json, email, first_name, last_name,
                        company, domain, title, linkedin_url, apollo_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex, batch_id,
                     json.dumps(row.get("_raw", row), ensure_ascii=False)[:20_000],
                     values["email"], values["first_name"], values["last_name"],
                     values["company"], values["domain"], values["title"],
                     values["linkedin_url"], _clean(row.get("apollo_id"))))
                kept += cursor.rowcount or 0
            await db.commit()
        return kept

    async def get_rows(self, batch_id: str, status: str | None = None,
                       row_ids: list[str] | None = None,
                       limit: int = 5_000) -> list[dict]:
        query = "SELECT * FROM import_rows WHERE batch_id = ?"
        args: list = [batch_id]
        if status:
            query += " AND status = ?"
            args.append(status)
        if row_ids is not None:
            if not row_ids:
                return []
            query += f" AND id IN ({','.join('?' for _ in row_ids)})"
            args.extend(row_ids)
        query += " ORDER BY rowid LIMIT ?"
        args.append(int(limit))
        return await self._rows(query, tuple(args))

    async def update_row(self, row_id: str, **fields) -> None:
        unknown = set(fields) - _ROW_COLUMNS
        if unknown:
            raise ValueError(f"not a row column: {', '.join(sorted(unknown))}")
        if not fields:
            return
        clause = ", ".join(f"{k} = ?" for k in fields)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE import_rows SET {clause}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), row_id))
            await db.commit()

    async def set_row_status(self, row_ids: list[str], status: str) -> int:
        if not row_ids or status not in ROW_STATUSES:
            return 0
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE import_rows SET status = ?, updated_at = ? "
                f"WHERE id IN ({','.join('?' for _ in row_ids)})",
                (status, _now(), *row_ids))
            await db.commit()
            return cursor.rowcount or 0

    async def mark_known_contacts(self, batch_id: str) -> int:
        """Mark every staged row whose address is already a prospect.

        Done before anything is enriched: paying Apollo for someone already
        in the pipeline is the easiest credit to waste.
        """
        async with self._connect() as db:
            cursor = await db.execute(
                """UPDATE import_rows SET status = 'duplicate', updated_at = ?
                   WHERE batch_id = ? AND status = 'staged' AND email != ''
                     AND email IN (SELECT email FROM prospects WHERE email != '')""",
                (_now(), batch_id))
            await db.commit()
            return cursor.rowcount or 0

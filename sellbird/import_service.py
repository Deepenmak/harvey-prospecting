"""Staging, enriching and promoting an imported list.

The four steps a file goes through, and the only places they happen:

    stage    parse the file, map its columns, put the rows in import_rows
    analyse  Claude reads the shape and recommends a route (free)
    enrich   one Apollo operation, inside a credit cap the user approved
    promote  enriched rows become prospects, through the existing importer

Each step is separately callable, so the dashboard, the CLI and a scheduled
run all drive the same code. Only ``enrich`` can spend, and it cannot spend
more than it was given.
"""

from __future__ import annotations

import json
import logging

from sellbird import apollo_ops
from sellbird.import_analysis import analyse, fallback
from sellbird.import_staging import (StagingError, map_headers, parse,
                                     rows_from, sample_for_analysis)
from sellbird.import_store import ROW_FIELDS, ImportStore

logger = logging.getLogger("sellbird.imports")

# A cap of 0 means "no daily cap"; the per-run cap still applies.
DAILY_CAP_KEY = "apollo_daily_credit_cap"
SPENT_TODAY_KEY = "apollo_credits_spent"


async def stage_file(store: ImportStore, name: str, text: str,
                     fmt: str = "") -> dict:
    """Parse and stage a file. Spends nothing; calls Apollo not at all.

    Returns as soon as the rows are safe in the database, carrying Sellbird's
    own reading of them. Claude's reading is a separate step
    (``analyse_batch``) because a file is staged in milliseconds and a model
    call takes seconds: holding the upload open for it would make a fast,
    free step feel like a slow one.
    """
    fmt, headers, raw_rows = parse(text, fmt, name)
    mapping = map_headers(headers)
    rows = rows_from(raw_rows, mapping)
    if not rows:
        raise StagingError("no row in that file has an address or a company")

    batch_id = await store.create_batch(name=name or "pasted", source=fmt)
    staged = await store.add_rows(batch_id, rows)
    duplicates = await store.mark_known_contacts(batch_id)

    stored = await store.get_rows(batch_id)
    analysis = fallback(stored, headers, mapping, duplicates)
    analysis["pending"] = True          # until Claude has had its turn
    await store.update_batch(
        batch_id, status="review", mapping_json=json.dumps(mapping),
        analysis_json=json.dumps(analysis))
    return {"batch_id": batch_id, "format": fmt, "staged": staged,
            "duplicates": duplicates, "analysis": analysis}


async def stage_from_apollo(store: ImportStore, config, segment: str = "mix",
                            limit: int = 25) -> dict:
    """Ask Apollo who exists, and stage the answer. Spends nothing.

    Apollo's people search is free and returns names, titles and companies
    with the addresses withheld. That is exactly a staged list: real
    candidates, no credit spent, and a priced way to buy the addresses when
    a human decides they are worth it. The paid half of ``sellbird fetch``
    is never reached from here.
    """
    import csv

    from sellbird import fetch

    report = await fetch.run_fetch(store, config, segment=segment,
                                   credits=max(1, int(limit)), plan=True)
    if report.stopped:
        raise StagingError(report.stopped)
    path = fetch.PLAN_FILE
    if not path.exists():
        raise StagingError("Apollo returned no candidates for that segment")
    with open(path, newline="", encoding="utf-8") as f:
        plan = list(csv.DictReader(f))
    if not plan:
        raise StagingError("Apollo returned no candidates for that segment")

    rows = []
    for candidate in plan:
        website = (candidate.get("website") or "").strip()
        rows.append({
            "apollo_id": candidate.get("apollo_id", ""),
            "first_name": candidate.get("first_name", ""),
            "title": candidate.get("title", ""),
            "company": candidate.get("company", ""),
            "domain": website.replace("https://", "").replace("http://", "")
                             .split("/")[0].removeprefix("www."),
            "_raw": candidate,
        })

    label = f"Apollo: {report.segment}"
    batch_id = await store.create_batch(name=label, source="apollo")
    staged = await store.add_rows(batch_id, rows)
    duplicates = await store.mark_known_contacts(batch_id)
    stored = await store.get_rows(batch_id)
    analysis = fallback(stored, ["apollo"], {}, duplicates)
    analysis["pending"] = False
    analysis["summary"] = (
        f"{staged} candidates from Apollo's free search across "
        f"{report.segment}. Their addresses are withheld until bought: "
        f"{len(apollo_ops.OPS['people_enrich_id'].eligible(stored))} of them "
        "at 1 credit each.")
    await store.update_batch(batch_id, status="review",
                             analysis_json=json.dumps(analysis))
    return {"batch_id": batch_id, "staged": staged, "duplicates": duplicates,
            "segment": report.segment, "searched": report.searched,
            "analysis": analysis}


async def analyse_batch(store: ImportStore, batch_id: str, brain) -> dict:
    """Claude's reading of a staged batch, folded into what is already there.

    Safe to skip, safe to fail: the batch is already usable with Sellbird's
    own reading, so this only ever improves the answer.
    """
    batch = await store.get_batch(batch_id)
    if batch is None:
        return {}
    rows = await store.get_rows(batch_id)
    raw_rows = [_json(r.get("raw_json")) for r in rows]
    headers: list[str] = []
    for raw in raw_rows[:50]:
        for key in raw:
            if key not in headers:
                headers.append(key)
    duplicates = (batch.get("counts") or {}).get("duplicate", 0)
    try:
        analysis = await analyse(
            brain, name=batch.get("name") or "", fmt=batch.get("source") or "",
            headers=headers, sample=sample_for_analysis(raw_rows, headers),
            rows=rows, duplicates=duplicates)
    except Exception as e:  # a staged batch must survive a bad analysis
        logger.warning("import analysis failed: %s", e)
        return {}
    analysis["pending"] = False
    await store.update_batch(
        batch_id, analysis_json=json.dumps(analysis),
        mapping_json=json.dumps(analysis.get("mapping") or batch.get("mapping") or {}))
    # A better mapping is worth applying, but never over an enriched row.
    if analysis.get("mapping") and analysis["mapping"] != (batch.get("mapping") or {}):
        await remap(store, batch_id, analysis["mapping"])
    return analysis


async def remap(store: ImportStore, batch_id: str, mapping: dict) -> int:
    """Re-read the staged rows with a corrected column mapping.

    The source row was kept verbatim, so a wrong guess costs nothing but a
    click. Rows that have already been enriched or imported are left alone:
    their credits are spent and their answer is real.
    """
    rows = await store.get_rows(batch_id)
    changed = 0
    for row in rows:
        if row["status"] not in ("staged", "duplicate"):
            continue
        raw = _json(row.get("raw_json"))
        remapped = rows_from([raw], mapping)
        if not remapped:
            continue
        fields = {f: remapped[0].get(f, "") for f in ROW_FIELDS}
        if any(fields[f] != (row.get(f) or "") for f in ROW_FIELDS):
            await store.update_row(row["id"], **fields)
            changed += 1
    await store.update_batch(batch_id, mapping_json=json.dumps(mapping))
    await store.mark_known_contacts(batch_id)
    return changed


async def estimate(store: ImportStore, batch_id: str, op_key: str,
                   row_ids: list[str] | None = None) -> dict:
    """What an operation would cost on this batch. Calls nothing."""
    op = apollo_ops.OPS.get(op_key)
    if op is None:
        raise StagingError(f"{op_key!r} is not an Apollo operation")
    rows = await _targets(store, batch_id, row_ids, op.key)
    eligible = op.eligible(rows)
    return {"op": op.key, "label": op.label, "rows": len(eligible),
            "skipped": len(rows) - len(eligible),
            "credits": len(eligible) * op.credits_per_unit,
            "free": op.credits_per_unit == 0,
            "needs": op._needs_text()}


async def enrich(store: ImportStore, batch_id: str, op_key: str,
                 row_ids: list[str] | None = None, max_credits: int = 0,
                 client=None) -> dict:
    """Run one Apollo operation over staged rows, inside ``max_credits``.

    The only function in Sellbird that spends an Apollo credit outside a
    fetch. It refuses to start without an explicit cap, records what each row
    cost, and stops the moment the cap is reached.
    """
    op = apollo_ops.OPS.get(op_key)
    if op is None:
        raise StagingError(f"{op_key!r} is not an Apollo operation")
    rows = await _targets(store, batch_id, row_ids, op.key)
    if op.credits_per_unit and max_credits <= 0:
        raise StagingError("approve a credit cap before enriching")

    allowed = await _cap_for(store, max_credits, op)
    if op.credits_per_unit and allowed <= 0:
        return {"op": op.key, "attempted": 0, "found": 0, "credits": 0,
                "stopped": "today's Apollo credit cap is already used up"}

    await store.update_batch(batch_id, status="enriching")
    owned = client is None
    if owned:
        from sellbird.integrations.apollo import (ApolloClient, ApolloError,
                                                  apollo_key)
        try:
            client = ApolloClient(apollo_key())
        except ApolloError as e:
            await store.update_batch(batch_id, status="review")
            raise StagingError(str(e))
    try:
        if owned:
            async with client as open_client:
                report = await apollo_ops.run_op(op, open_client, rows,
                                                 max_credits=allowed)
        else:
            report = await apollo_ops.run_op(op, client, rows, max_credits=allowed)
    finally:
        await store.update_batch(batch_id, status="review")

    await _record(store, batch_id, op, report)
    return {**report.as_dict(), "summary": apollo_ops.summarise(report)}


async def promote(store: ImportStore, batch_id: str,
                  row_ids: list[str] | None = None,
                  include_unenriched: bool = True) -> dict:
    """Turn finished rows into real contacts.

    Everything goes through ``import_contacts.import_contacts``, so the
    contact files, the ledger and the database are written by one piece of
    code whatever the contacts came from.
    """
    from sellbird.fetch import CONTACT_COLUMNS
    from sellbird.import_contacts import import_contacts

    rows = await store.get_rows(batch_id, row_ids=row_ids)
    wanted = [r for r in rows if r["status"] == "enriched"
              or (include_unenriched and r["status"] == "staged" and r["email"])]
    if not wanted:
        return {"promoted": 0, "message": "nothing in this batch is ready to import"}

    payload = []
    for row in wanted:
        raw = _json(row.get("raw_json"))
        # A Sellbird export already speaks the contact vocabulary: keep every
        # column it carried, including the research the skills wrote.
        contact = {k: v for k, v in raw.items() if k in CONTACT_COLUMNS}
        contact.update({k: row[k] for k in ROW_FIELDS if row.get(k)})
        contact.update({k: v for k, v in _json(row.get("enriched_json")).items() if v})
        if row.get("apollo_id"):
            contact["apollo_id"] = row["apollo_id"]
        if not contact.get("email_status"):
            # Honest by default: an address nobody verified is a guess, and a
            # guess is never sent (sellbird/fetch.py email_status).
            contact["email_status"] = "guess"
        contact.setdefault("segment", "import")
        payload.append(contact)

    report = await import_contacts(store, payload)
    await store.set_row_status([r["id"] for r in wanted], "imported")
    counts = await store.batch_counts(batch_id)
    # Finished when nothing is left that could still be acted on. Rows that
    # were already yours, or that failed, are not work in progress.
    waiting = counts.get("staged", 0) + counts.get("enriched", 0)
    await store.update_batch(batch_id, status="review" if waiting else "imported")
    return {"promoted": len(wanted), **report.as_dict(),
            "message": f"Imported {len(wanted)} contacts. {report.summary()}."}


# ── internals ──


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _targets(store: ImportStore, batch_id: str,
                   row_ids: list[str] | None,
                   op_key: str = "") -> list[dict]:
    """The rows an operation may touch.

    A row already in the pipeline is finished, and a contact you already have
    is skipped unless the caller names it by id — which is a decision rather
    than an accident. Whether a particular operation has already run on a row
    is ``ApolloOp.eligible``'s business, so chaining works: a company lookup
    hands its Apollo id to a job-postings run without being charged again.
    """
    rows = await store.get_rows(batch_id, row_ids=row_ids)
    allowed = {"staged", "enriched"}
    if row_ids:
        allowed.add("duplicate")
    return [r for r in rows if r["status"] in allowed]


async def _cap_for(store: ImportStore, max_credits: int, op) -> int:
    """The smaller of what the user approved and what today's cap leaves."""
    if op.credits_per_unit == 0:
        return max(0, int(max_credits))
    try:
        daily = int(await store.get_setting(DAILY_CAP_KEY, "0") or 0)
    except (TypeError, ValueError):
        daily = 0
    if daily <= 0:
        return int(max_credits)
    spent = await _spent_today(store)
    return max(0, min(int(max_credits), daily - spent))


def _today_key() -> str:
    from datetime import date
    return f"{SPENT_TODAY_KEY}:{date.today().isoformat()}"


async def _spent_today(store: ImportStore) -> int:
    try:
        return int(await store.get_setting(_today_key(), "0") or 0)
    except (TypeError, ValueError):
        return 0


async def _record(store: ImportStore, batch_id: str, op, report) -> None:
    """Write what each row got, and what the batch spent."""
    touched = [r.row_id for r in report.results]
    stored = await store.get_rows(batch_id, row_ids=touched) if touched else []
    row_json = {r["id"]: r.get("enriched_json") for r in stored}
    row_route = {r["id"]: r.get("route") for r in stored}
    row_credits = {r["id"]: r.get("credits") or 0 for r in stored}
    for result in report.results:
        previous = row_route.get(result.row_id) or ""
        route = ",".join(sorted({*(p for p in previous.split(",") if p), op.key}))
        fields: dict = {"route": route,
                        "credits": (row_credits.get(result.row_id) or 0) + result.credits}
        if result.error:
            fields["status"] = "failed"
            fields["error"] = result.error[:300]
        elif result.person:
            contact = apollo_ops.contact_from_person(result.person)
            fields.update({
                "status": "enriched",
                "enriched_json": json.dumps(contact),
                "apollo_id": contact.get("apollo_id", ""),
                "email_status": contact.get("email_status", ""),
                "email": contact.get("email") or "",
            })
        elif result.organization:
            # Company facts add to what a row already knows rather than
            # replacing it: a person found earlier must survive an org lookup.
            known = _json(row_json.get(result.row_id))
            fields["status"] = "enriched"
            fields["enriched_json"] = json.dumps(
                {**known, **{k: v for k, v in
                             apollo_ops.org_fields(result.organization).items() if v}})
        else:
            fields["error"] = "Apollo found nobody, so nothing was charged"
        try:
            await store.update_row(result.row_id, **fields)
        except Exception as e:
            logger.warning("could not record row %s: %s", result.row_id, e)
    if report.credits:
        await store.add_credits(batch_id, report.credits)
        await store.set_setting(_today_key(),
                                str(await _spent_today(store) + report.credits))
        await store.log_action(action_type="apollo_credits_spent", agent="you",
                               details={"op": op.key, "credits": report.credits,
                                        "batch": batch_id, "found": report.found})

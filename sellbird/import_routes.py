"""The dashboard's Imports routes: staging, pricing, enriching, promoting.

A list that arrives from outside is staged first and costs nothing. The user
then sees what each Apollo operation would cost on these exact rows and
approves a cap before anything is called. None of these routes is on the
coding agent's allowlist (sellbird.agent_policy): spending a credit is the
user's decision, always.

The file itself is read in the browser and posted as text, so there is no
upload handling here and no new dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sellbird.background import jobs
from sellbird import apollo_ops, import_service
from sellbird.import_staging import MAX_TEXT_BYTES, StagingError
from sellbird.import_store import ImportStore

logger = logging.getLogger("sellbird.import_routes")

router = APIRouter()

# One enrichment run at a time, like discovery: two would race the same rows
# and the credit cap would mean nothing.
_run_task: asyncio.Task | None = None
_run_state: dict = {"state": "idle", "batch_id": "", "op": "", "message": "",
                    "report": None}
# Background analyses, held so the loop does not drop them mid-flight.
_analysis_tasks: list[asyncio.Task] = []


def _refuse(message: str, code: int = 400):
    return JSONResponse({"success": False, "message": message}, status_code=code)


def _store() -> ImportStore:
    from sellbird import dashboard
    return ImportStore(db_path=str(dashboard.DB_PATH))


def _apollo_store():
    """The Apollo record: runs, contacts and the ledger, in this workspace's
    database rather than in exports/ and data/fetch_ledger.json."""
    from sellbird import dashboard
    from sellbird.apollo_store import ApolloStore
    return ApolloStore(db_path=str(dashboard.DB_PATH))


def _brain(store):
    """The brain, when there is one. An import works without it."""
    try:
        from sellbird.brain import Brain
        return Brain(store)
    except Exception as e:
        logger.info("import analysis will run without Claude: %s", e)
        return None


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _row_ids(body: dict) -> list[str] | None:
    """`row_ids: [...]` narrows an action; "all" or nothing means the batch."""
    ids = body.get("row_ids")
    if ids in (None, "all", ""):
        return None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise StagingError('row_ids must be a list of ids, or "all"')
    return ids[:20_000]


@router.post("/api/imports")
async def create_import(request: Request):
    """Stage a file. Parses, maps its columns, reads it with Claude. No spend."""
    body = await _body(request)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _refuse("There is nothing in that file.")
    if len(text.encode("utf-8", "ignore")) > MAX_TEXT_BYTES:
        return _refuse("That file is larger than 8 MB. Split it and import the parts.")
    name = str(body.get("name") or "")[:200]
    fmt = str(body.get("format") or "")[:20]

    store = _store()
    await store.init_db()
    try:
        result = await import_service.stage_file(store, name, text, fmt)
    except StagingError as e:
        return _refuse(f"Not staged: {e}.")
    except Exception as e:
        logger.exception("staging failed")
        return _refuse(f"Could not stage that file: {type(e).__name__}", 500)

    # Claude reads the file after the rows are safe, not before: staging is
    # instant and free, and a model call is neither.
    brain = _brain(store)
    if brain is not None:
        _analysis_tasks.append(jobs.start(
            "import-analysis",
            import_service.analyse_batch(store, result["batch_id"], brain)))
        _analysis_tasks[:] = [t for t in _analysis_tasks if not t.done()]
    else:
        await store.update_batch(
            result["batch_id"],
            analysis_json=json.dumps({**result["analysis"], "pending": False}))
    counts = result["analysis"].get("quality") or {}
    return {"success": True, "batch_id": result["batch_id"],
            "staged": result["staged"], "duplicates": result["duplicates"],
            "format": result["format"], "counts": counts,
            "analysis": result["analysis"],
            "message": f"Staged {result['staged']} rows. Nothing has been spent."}


@router.get("/api/imports")
async def list_imports():
    store = _store()
    await store.init_db()
    return {"batches": await store.list_batches(),
            "run": {k: _run_state[k] for k in ("state", "batch_id", "op", "message")},
            "credits_today": await import_service._spent_today(store),
            "daily_cap": await store.get_setting(import_service.DAILY_CAP_KEY, "0")}


@router.get("/api/imports/{batch_id}")
async def get_import(batch_id: str, limit: int = 500):
    store = _store()
    await store.init_db()
    batch = await store.get_batch(batch_id)
    if batch is None:
        return _refuse("No import with that id.", 404)
    rows = await store.get_rows(batch_id, limit=min(int(limit), 5_000))
    # Price the whole menu against the rows an operation would really touch,
    # so every card shows what it would cost here rather than a generic rate.
    # Contacts already in the pipeline are not among them, and an operation
    # that has already run on a row does not count it again.
    targets = [r for r in rows if r["status"] in ("staged", "enriched")]
    return {"batch": batch, "rows": rows, "ops": apollo_ops.menu(targets),
            "run": {k: _run_state[k] for k in ("state", "batch_id", "op", "message")}}


@router.post("/api/imports/{batch_id}/mapping")
async def set_mapping(batch_id: str, request: Request):
    """Correct which column means what, and re-read the staged rows."""
    body = await _body(request)
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        return _refuse("Send a mapping of column to field.")
    store = _store()
    await store.init_db()
    if await store.get_batch(batch_id) is None:
        return _refuse("No import with that id.", 404)
    changed = await import_service.remap(store, batch_id, mapping)
    return {"success": True, "changed": changed,
            "message": f"Re-read {changed} rows with the new mapping."}


@router.post("/api/imports/{batch_id}/estimate")
async def estimate_import(batch_id: str, request: Request):
    """What an operation would cost on these rows. Calls nothing."""
    body = await _body(request)
    store = _store()
    await store.init_db()
    if await store.get_batch(batch_id) is None:
        return _refuse("No import with that id.", 404)
    try:
        return await import_service.estimate(store, batch_id,
                                             str(body.get("op") or ""),
                                             _row_ids(body))
    except StagingError as e:
        return _refuse(str(e))


@router.post("/api/imports/{batch_id}/enrich")
async def enrich_import(batch_id: str, request: Request):
    """Spend credits, up to the cap in the request, and no further."""
    global _run_task
    body = await _body(request)
    op_key = str(body.get("op") or "")
    try:
        row_ids = _row_ids(body)
        max_credits = int(body.get("max_credits") or 0)
    except (StagingError, TypeError, ValueError) as e:
        return _refuse(str(e) if isinstance(e, StagingError)
                       else "max_credits must be a whole number.")
    if _run_task is not None and not _run_task.done():
        return _refuse("An enrichment is already running. Wait for it to finish.", 409)

    store = _store()
    await store.init_db()
    if await store.get_batch(batch_id) is None:
        return _refuse("No import with that id.", 404)
    op = apollo_ops.OPS.get(op_key)
    if op is None:
        return _refuse(f"{op_key!r} is not an Apollo operation.")
    if op.credits_per_unit and max_credits <= 0:
        return _refuse("Approve a credit cap before enriching.")

    _run_state.update(state="running", batch_id=batch_id, op=op_key,
                      message="", report=None)

    async def _go():
        try:
            report = await import_service.enrich(store, batch_id, op_key,
                                                 row_ids, max_credits)
            _run_state.update(state="done", report=report,
                              message=report.get("summary", ""))
        except StagingError as e:
            _run_state.update(state="failed", message=str(e))
        except Exception as e:
            logger.exception("enrichment failed")
            _run_state.update(state="failed",
                              message=f"Enrichment failed: {type(e).__name__}")

    _run_task = jobs.start("apollo", _go())
    return {"success": True, "started": True, "op": op_key,
            "max_credits": max_credits}


@router.get("/api/imports/run/status")
async def run_status():
    return dict(_run_state)


@router.post("/api/imports/{batch_id}/import")
async def promote_import(batch_id: str, request: Request):
    """Turn finished rows into contacts. Spends nothing."""
    body = await _body(request)
    store = _store()
    await store.init_db()
    if await store.get_batch(batch_id) is None:
        return _refuse("No import with that id.", 404)
    try:
        result = await import_service.promote(
            store, batch_id, _row_ids(body),
            include_unenriched=bool(body.get("include_unenriched", True)))
    except StagingError as e:
        return _refuse(str(e))
    except Exception as e:
        logger.exception("promotion failed")
        return _refuse(f"Could not import those rows: {type(e).__name__}", 500)
    return {"success": True, **result}


@router.post("/api/imports/{batch_id}/discard")
async def discard_import(batch_id: str):
    store = _store()
    await store.init_db()
    if await store.get_batch(batch_id) is None:
        return _refuse("No import with that id.", 404)
    await store.update_batch(batch_id, status="discarded")
    return {"success": True, "message": "Discarded. The rows stay until you delete them."}


@router.delete("/api/imports/{batch_id}")
async def delete_import(batch_id: str):
    store = _store()
    await store.init_db()
    if not await store.delete_batch(batch_id):
        return _refuse("No import with that id.", 404)
    return {"success": True, "message": "Deleted."}


@router.get("/api/apollo/ops")
async def apollo_operations():
    """Every Apollo operation and what it costs, with nothing staged.

    This is the answer to "what can Apollo do for us, and for how much",
    independent of any one import.
    """
    return {"ops": apollo_ops.menu()}


@router.get("/api/apollo/segments")
async def apollo_segments():
    """Who Apollo is asked about, and what the record says has been spent.

    The record is the database, not data/fetch_ledger.json. That is the
    point of it: the database sits on the mounted volume and is carried
    through a redeploy, so every contact and every credit a past run spent
    is still here afterwards. Anything an older install left in the file or
    in exports/ is adopted the first time this is read, and if the files
    have gone thin while the database has not, they are rebuilt here.
    """
    from sellbird import fetch as fetch_module

    try:
        spec = fetch_module.load_segments()
    except Exception as e:
        return JSONResponse({"error": f"segments.yaml could not be read: {e}"},
                            status_code=500)
    apollo = _apollo_store()
    await apollo.init_db()
    repaired: dict = {}
    try:
        if not await fetch_module.exports_in_sync(apollo):
            repaired = await fetch_module.reconcile(apollo)
    except Exception as e:  # a tab must still open
        logger.warning("Apollo record not reconciled: %s", e)
    summary = await apollo.summary()
    per_segment = await apollo.segment_counts()
    segments = [{
        "key": s.get("key", ""), "label": s.get("label", ""),
        "locations": s.get("locations") or [],
        "titles": len(s.get("titles") or []),
        "employees": s.get("employees") or [],
        "fetched": per_segment.get(s.get("key", ""), {}).get("contacts", 0),
        "with_email": per_segment.get(s.get("key", ""), {}).get("with_email", 0),
    } for s in spec.get("segments") or []]
    store = _store()
    await store.init_db()
    return {
        "segments": segments,
        "per_run_credits": int(spec.get("per_run_credits", 10) or 10),
        "phase_credit_cap": int(spec.get("phase_credit_cap", 0) or 0),
        "credits_used": summary["credits_used"],
        "people_seen": summary["people_seen"],
        "companies_seen": summary["companies_seen"],
        "stored_contacts": summary["contacts"],
        "stored_with_email": summary["with_email"],
        "runs": summary["runs"],
        "repaired": bool(repaired),
        "credits_today": await import_service._spent_today(store),
        "key_set": bool(_apollo_key()),
    }


@router.get("/api/apollo/runs")
async def apollo_runs(limit: int = 25):
    """Every Apollo run this workspace has made, newest first.

    Free searches are in here too. exports/plan.csv is replaced by the next
    plan run, so without this row there would be no record that the search
    ever happened.
    """
    apollo = _apollo_store()
    await apollo.init_db()
    return {"runs": await apollo.runs(limit), "summary": await apollo.summary()}


@router.post("/api/apollo/sync")
async def apollo_sync():
    """Make the database and the two contact files agree, now.

    Nothing here calls Apollo and nothing costs a credit. It reads whatever
    the files hold into the database and writes the files again from it, so
    it is both the repair after a redeploy and the way judgement a skill
    wrote into contacts_all.csv becomes durable.
    """
    from sellbird.fetch import reconcile

    apollo = _apollo_store()
    await apollo.init_db()
    try:
        result = await reconcile(apollo)
    except Exception as e:
        logger.exception("Apollo sync failed")
        return _refuse(f"Could not sync the Apollo record: {e}", 500)
    adopted = result.get("adopted") or {}
    added = int(adopted.get("contacts") or 0)
    return {"success": True, "stored": result.get("stored", 0),
            "files": result.get("files", 0),
            "credits_used": result.get("credits_used", 0),
            "message": (f"{result.get('stored', 0)} contacts are in the database "
                        f"and in both files"
                        + (f"; {added} read in from exports" if added else "")
                        + ". Nothing was spent.")}


def _apollo_key() -> str:
    from sellbird.integrations.apollo import apollo_key
    try:
        return apollo_key()
    except Exception:
        return ""


@router.post("/api/apollo/candidates")
async def find_candidates(request: Request):
    """Apollo's free people search, staged for review. Spends nothing.

    The search returns names, titles and companies with the addresses
    withheld, which is the whole point: the list can be looked at, cut down
    and priced before a single credit is spent on it.
    """
    global _run_task
    body = await _body(request)
    segment = str(body.get("segment") or "mix")[:60]
    try:
        limit = int(body.get("limit") or 25)
    except (TypeError, ValueError):
        return _refuse("How many candidates? Give a whole number.")
    if not 1 <= limit <= 500:
        return _refuse("Ask for between 1 and 500 candidates.")
    if not _apollo_key():
        return _refuse("No Apollo key yet. Add it on the Settings tab.")
    if _run_task is not None and not _run_task.done():
        return _refuse("Something is already running. Wait for it to finish.", 409)

    store = _store()
    await store.init_db()
    _run_state.update(state="running", batch_id="", op="apollo_search",
                      message="", report=None)

    async def _go():
        try:
            from sellbird.config import load_config
            result = await import_service.stage_from_apollo(
                store, load_config(), segment, limit)
            _run_state.update(state="done", batch_id=result["batch_id"],
                              report=result,
                              message=f"{result['staged']} candidates staged. "
                                      "Nothing has been spent.")
        except StagingError as e:
            _run_state.update(state="failed", message=str(e))
        except Exception as e:
            logger.exception("apollo search failed")
            _run_state.update(state="failed",
                              message=f"The search failed: {type(e).__name__}")

    _run_task = jobs.start("apollo", _go())
    return {"success": True, "started": True, "segment": segment, "limit": limit}

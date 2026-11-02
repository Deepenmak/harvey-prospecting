---
name: lead-gen
description: Run Harvey's Apollo lead generation end to end with Claude as the judgement layer. Free search, parallel ICP vetting, a spend gate, enrichment, parallel web research for buying signals, fact verification, one ranked contact list. Use when the user asks to fetch, source or generate leads with Harvey and Apollo.
argument-hint: "[credits] [segment|mix] [--yes] [--no-research] [--drafts]"
disable-model-invocation: true
---

# Lead generation with Harvey + Apollo

Arguments: $ARGUMENTS

Harvey does the deterministic, money-touching work (search, enrich, ledger,
credit cap). You and your subagents do the judgement Harvey can't: whether a
company fits, whether it shows buying intent, and why it would buy.

Defaults when an argument is missing: segment `mix`, credits = ask the user.
`--yes` means the user already approved spending that many credits.
`--no-research` stops after enrichment. `--drafts` adds first-touch drafts.

## Files (fixed; no file per run)

| File | What it holds |
|---|---|
| `exports/contacts_all.csv` | Every contact ever fetched, best first. The one source of truth. |
| `exports/contacts_with_email.csv` | The same rows, only those with an email. Rewritten from the first. |
| `exports/plan.csv` | The current candidate plan, replaced by each `--plan`. |
| `exports/work/*.json` | This run's agent answers, cleared at the start of each run. |

The lasting record is the **Optywise Leads** dashboard
(https://claude.ai/artifact/1etfjcKyomZpVHJwVwJCR4): its `contacts.json` and
`runs.json` hold every contact and the ledger. `scripts/dashboard_sync.py`
moves them both ways; ROUTINE.md has the restore and upload steps. In a cloud
run, restore before Phase 0 and upload after Phase 7. On the laptop, restore
before Phase 0 too (a stale copy would drop rows from the dashboard), and upload
after Phase 7 so the dashboard stays current.

Fit, research, fact checks and drafts are columns on each contact's row
(fit, fit_reason, intent, buying_state, account_tier, signals, red_flags,
verified, verify_notes, angle, draft_subject, draft_body), so a lead's whole
story is in one place. The call-prep and proposal skills add call_brief,
call_notes, deal_check and proposal_draft. Never create another export file.

## Hard rules

- Only you (the orchestrator) run `harvey fetch`. Subagents never run Bash,
  never spend credits and never write files: the ledger has one writer.
- Never pass `--credits` above what the user approved, never edit
  `phase_credit_cap` or `segments.yaml` during a run, never touch
  `.github/workflows/` or repo secrets.
- Never send email, approve the Outbox or turn on autopilot. Drafts are text.
- Never print API keys or tokens, including inside URLs or errors.
- No real person's or company's name in anything committed. `exports/` and
  `data/` are gitignored.
- Any draft or angle: no asterisks, no arrows, no em dashes.

## Setup

Run from the project root. Python is `.venv/Scripts/python.exe` on Windows,
`.venv/bin/python` elsewhere; prefix `PYTHONIOENCODING=utf-8` in Git Bash.
Call it `PY` below. `H` means `PY -m harvey`. `S` means
`.claude/skills/lead-gen/scripts`.

ICP file: `optywise_icp.md` unless the user names another. Read it and
`segments.yaml` before Phase 2.

## Phase 0: preflight (free)

1. `PY -m pytest -q tests/test_fetch.py` must pass.
2. Empty `exports/work/` (create it if missing).
3. Ledger: the Google Sheet is retired; the ledger is `data/fetch_ledger.json`
   (restored from the dashboard in a cloud run). If `SHEET_WEBHOOK_URL` is
   set, prefix every `H fetch` with `SHEET_WEBHOOK_URL=` so the local ledger
   is used.
4. Credits left = `phase_credit_cap` minus the ledger's used credits. If the
   requested credits exceed that, say so and use the smaller number.

## Phase 1: plan (free)

`H fetch --segment <segment> --credits <N> --plan --json`

Writes `exports/plan.csv`: about three candidates per credit so every skip
has a stand-in. Columns: segment, apollo_id, first_name, title, company,
title_rank, verdict, reason.

## Phase 2: vet in parallel (free)

Free search shows only the company name, so this phase is the industry filter.

- Group plan rows by segment. Spawn one `lead-vetter` subagent per segment,
  all in one message so they run in parallel. Under 15 rows in total, vet
  them yourself instead: a subagent costs more than it saves. Read
  `.claude/agents/lead-vetter.md` first and apply its rubric exactly.
- Give each vetter: the ICP file path, the segment's key, label and
  locations from `segments.yaml`, and its rows as JSON (apollo_id, title,
  company, title_rank). Nothing else; the agent definition holds the rubric.
- Save the merged answers to `exports/work/verdicts.json`, then `PY S/apply_verdicts.py`.
- Spot-check: read every `skip` reason and every `a`. Overrule anything
  that contradicts the ICP. If a segment has fewer keeps than its share of
  credits, say so (the credits flow to the other segments).

## Phase 3: spend gate

`H fetch --from-plan --credits <N> --dry-run`

Show the user the picks (segment, title, company) and the credits. Wait for
an explicit yes unless `--yes` was given. Stop here if they decline.

## Phase 4: enrich (spends credits)

`H fetch --from-plan --credits <N> --json`

It folds the new contacts, with their fit and fit_reason, into both contact
files. Report enriched, credits used, credits left. The report's `new_ids`
are this run's contacts. If `--no-research`, skip to Phase 7.

## Phase 5: research in parallel (free, uses Claude quota)

This is where buying intent comes from: Apollo's free plan blocks hiring
filters, and mid-size carriers, banks and RCM firms rarely publish a job
board Harvey can read.

- Take the `new_ids` rows from `exports/contacts_all.csv`. Scale to the batch:
  up to 6 leads, research yourself or with one `lead-researcher` (if yourself,
  read `.claude/agents/lead-researcher.md` first and apply its rubric exactly).
  Above that, one researcher per segment (at most 5 agents, about 5-8 leads
  each), spawned in one message.
- Give each researcher its leads as JSON: apollo_id, company, domain, title,
  segment, industry, employees, city, country, fit, fit_reason. Leave out
  emails and LinkedIn URLs; the research is about the company, not the person.
- Save the merged answers to `exports/work/research.json`.

## Phase 6: verify (free, uses Claude quota)

Take the top researched leads (fit `a` with intent high or medium, at most
10) and spawn one `lead-verifier` with their research entries. It opens each
cited URL and checks each claim. Save its answer to `exports/work/verify.json`.
For any `fail`, rewrite that lead's angle in research.json from the facts that
passed, or from title and company alone.

## Phase 7: fold into the contact files

`PY S/apply_review.py` applies whatever is in `exports/work/` (research,
verify, drafts) to the matching rows and re-ranks both files: fit, then
intent, then the fact check, then score. It lowers intent to fit the buying
state and marks tier A accounts. Then upload `contacts.json` and
`runs.json` to the dashboard (ROUTINE.md, step 3).

## Phase 8: drafts (only with --drafts)

Spawn one `lead-writer` with the top new leads (at most 10): first name,
title, company, segment, and their research entry. Save its answer to
`exports/work/drafts.json` and run `PY S/apply_review.py --drafts exports/work/drafts.json`.
The drafts land in the draft_subject and draft_body columns. Nothing goes to
the Outbox.

## Report to the user

Plain prose, short. Credits spent and left; new contacts per segment; verified
email rate; how many show real intent and an open need; any tier A accounts;
the top 10 new leads with one line each on why they'd buy; companies skipped
and why (grouped); problems and
concrete changes to `segments.yaml` or the ICP. Point to
`exports/contacts_with_email.csv`.

See [reference.md](reference.md) for the JSON shapes and the reasoning behind
each phase.

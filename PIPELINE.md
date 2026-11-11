# Optywise pipeline: workspace map and build plan

This fork runs Sellbird as Optywise's sales pipeline, with Claude Code as the
judgement layer. Sellbird's code does everything that touches money, mailboxes
or saved state; Claude does every judgement; files and the database are the
contracts between them. The overview with diagrams is the "Optywise Sales
Pipeline" artifact on claude.ai.

Status as of 2026-11-02.

## Workspace map

| Path | What it is | Who reads it |
|---|---|---|
| `sellbird/` | Python package: fetch engine, adapters, send guards, heartbeat agents | Sellbird CLI, `sellbird run` |
| `optywise_icp.md` | Who we sell to and why: segments, buyers, disqualifiers | Claude agents, the lead-gen skill |
| `segments.yaml` | Apollo search targets, credits per run, the credit cap | `sellbird fetch` |
| `sellbird.yaml`, `sellbird.local.yaml` | Sender identity, limits, quiet hours, approval mode | Sellbird (local file wins) |
| `skills/*.md` | **Knowledge**: what to know (product, offer, signals, email rules, objections) | Sellbird's heartbeat agents via `brain.py`, and Claude agents by path |
| `prompts/*.md` | Instructions for Sellbird's heartbeat agents | `brain.py` |
| `.claude/skills/<name>/` | **Procedures**: how to do a job end to end (Claude Code skills) | Claude Code, by hand or in the cloud routine |
| `.claude/agents/*.md` | **Workers**: one narrow job each, JSON in and out, no files or money | Spawned by a procedure |
| `scripts/cloud_setup.sh` | Installs Sellbird when a cloud session starts (hook in `.claude/settings.json`) | Claude cloud |
| `ROUTINE.md` | The scheduled cloud task: settings and prompt | You, when editing the routine |
| `data/`, `exports/` | Local state and the two contact files (gitignored) | Sellbird, the lead-gen skill |
| `tests/test_fetch.py` | Safety tests for fetch; must pass before any spend | Preflight |

Outside this repo:

- `../agency-agents/`: reference library only. Ideas are adapted into our
  own agents and knowledge files; nothing is installed from it. Mined so far:
  Discovery Coach (call prep), Outbound Strategist (signal ranking,
  disqualifiers, account tiers), Proposal Strategist and Offer Strategist
  (proposals, entry offer).
- linkedin-skills: installed as a Claude Code
  plugin in step 2, not copied in.

### The three kinds of "skill"

Keep each fact in one place:

1. **Knowledge** lives in `skills/*.md`. Sellbird's Python agents load it by
   name (`brain.py` `skill_map`), and Claude agents read it by path. Write a
   fact once, here.
2. **Procedures** live in `.claude/skills/<name>/SKILL.md`. They orchestrate:
   run Sellbird's CLI for anything that spends or saves, spawn workers, fold
   their answers back in.
3. **Workers** live in `.claude/agents/<name>.md`. They read knowledge files,
   take JSON, return JSON, and never run commands, spend credits or write
   files.

Adding a knowledge file that Sellbird's heartbeat should use also needs an
entry in `skill_map` in `sellbird/brain.py`.

### Current procedures and workers

| Procedure | Workers | Status |
|---|---|---|
| `lead-gen` | `lead-vetter`, `lead-researcher`, `lead-verifier`, `lead-writer` | Works |
| `call-prep` | `call-prepper` | Built |
| `proposal` | `proposal-writer` | Built |
| `weekly-review` | none (reads results; `scripts/weekly_stats.py`) | Built, run by hand; no replies to learn from until sending starts |
| `linkedin-warmup` | linkedin-skills plugin | Step 2 |

## Rules that hold everywhere

- Judgement in Claude, guarantees in code: a cap, a limit or an approval is
  never left to a prompt.
- One writer per piece of state: only the orchestrator runs `sellbird fetch`;
  only Sellbird writes the ledger and the outbox.
- Fixed files, no file per run: `exports/contacts_all.csv` and
  `exports/contacts_with_email.csv`, updated in place. New per-lead output
  (drafts, briefs) becomes a column, not a new file.
- Humans gate anything outward: spending, sending, LinkedIn posts, proposals.
- No real person's or company's name in anything committed.
- Drafts and angles: no asterisks, no arrows, no em dashes.

## Build plan

Each task lists what changes, where, and how we know it is done.

### Step 1: unblock and sharpen (now, free)

| # | Task | Where | Done when |
|---|---|---|---|
| 1.1 | Done: routine environment's Network access reaches Apollo and company sites | claude.ai, by you | A cloud run reaches Apollo |
| 1.2 | Done: cloud setup hook pushed | `scripts/cloud_setup.sh`, `.claude/settings.json` | A cloud run logs "Cloud setup: Sellbird is installed" |
| 1.3 | Upload to the dashboard right after spending, not only at the end | `.claude/skills/lead-gen/SKILL.md`, routine prompt | A run killed after enrichment still shows its leads and credits on the dashboard |
| 1.4 | Done: "never closes" rules and buying states in the ICP; code lowers intent to fit the state and marks tier A accounts | `optywise_icp.md`, `.claude/agents/`, `sellbird/fetch.py` | A company that already solved the workflow can't rank as high intent |
| 1.5 | Low-commitment entry offer, agreed with you. Until then, drafts offer only a conversation | `skills/offer_strategy.md` | One paragraph you have signed off |
| 1.6 | Email drafts on in the daily run, shown on the dashboard | routine prompt (`--drafts`), dashboard page | Each new lead on the dashboard has a subject and body |
| 1.7 | Done: call-prep procedure and worker | `.claude/skills/call-prep/`, `.claude/agents/call-prepper.md` | "Prep my call with <lead>" returns a one-page brief from the lead's row and research, saved in a `call_brief` column |

Still open: 1.3, 1.5 and 1.6.

### Step 2: more sources, LinkedIn (this week)

| # | Task | Where | Done when |
|---|---|---|---|
| 2.1 | BetterContact adapter: free search, paid email lookup, balance | `sellbird/integrations/bettercontact.py` | Tests pass; a free search returns rows |
| 2.2 | Per-segment source and a separate credit cap per provider | `segments.yaml`, `sellbird/fetch.py` | Neither provider can spend the other's budget; no duplicate people across them |
| 2.3 | `source` column on every contact | `sellbird/fetch.py`, dashboard | Dashboard shows where each lead came from |
| 2.4 | Install linkedin-skills as a plugin | Claude Code `/plugin` | Its skills answer "draft a comment on this post" |
| 2.5 | `linkedin-warmup` procedure: comment drafts for the top 10 leads | `.claude/skills/linkedin-warmup/` | Drafts land in a `linkedin_comment` column; you post them |

### Step 3: ready to send (2 to 4 weeks of lead time)

| # | Task | Where | Done when |
|---|---|---|---|
| 3.1 | SQLite as the one store; contact files and dashboard become exports | `sellbird/state.py`, `sellbird/fetch.py`, `dashboard_sync.py` | Fetch writes prospects to `sellbird.db`; CSVs are generated from it |
| 3.2 | Bridge: approved leads and drafts into the outbox | new CLI command | `sellbird outbox` lists the approved drafts |
| 3.3 | Separate sending domain, Google Workspace, warm-up | You, then `sellbird gmail auth`, `sellbird trulyinbox connect` | `sellbird warmup` reports ready |
| 3.4 | Gmail sending with approval on, heartbeat on the laptop | `sellbird.local.yaml`, `sellbird run` | An approved test email to your own address arrives |

### Step 4: close the loop (after the first replies)

| # | Task | Where | Done when |
|---|---|---|---|
| 4.1 | Reply handling reviewed against our ICP and offer | `prompts/handler.md`, `skills/objection_handling.md` | Replies are sorted and answered as drafts for approval |
| 4.2 | Done: proposal procedure and worker | `.claude/skills/proposal/`, `.claude/agents/proposal-writer.md` | Call notes in, a deal checklist and proposal draft out |
| 4.3 | Weekly review: skill done; second cloud routine waits for replies | `.claude/skills/weekly-review/`, a second cloud routine | A weekly summary with suggested `segments.yaml` changes |
| 4.4 | Heartbeat on a small always-on server | server | Sending and reply checks run with the laptop off |

## Decisions waiting on you

- The entry offer (1.5): the small first step before the six-week engagement,
  for example a paid one- to two-week Probe or a free workflow teardown call,
  with what it includes and what it costs.
- "Commit and push" when you want changes to reach the cloud run.

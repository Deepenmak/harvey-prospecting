# Scheduled lead fetch (Claude cloud scheduled task)

Runs on Anthropic's cloud on a schedule, so it works with the laptop off.
The lasting record is the **Optywise Leads** dashboard, a private claude.ai
artifact: https://claude.ai/artifact/1etfjcKyomZpVHJwVwJCR4. Each run starts
from a fresh clone, so it restores Harvey's contacts and ledger from the
dashboard first and writes them back at the end. `harvey fetch` refuses to
run in the cloud when no ledger was restored, so a run can never forget what
earlier runs spent or fetched.

## Task settings (claude.ai/code/routines)

- Repository: `Deepenmak/harvey-prospecting`
- Environment: network access **Full** (Apollo, plus profiling and research
  read each company's own website); variable
  `APOLLO_HARVEY_KEY=<Apollo API key>`. Don't set `SHEET_WEBHOOK_URL`. No
  setup script needed: the repo's SessionStart hook (`.claude/settings.json`)
  runs `scripts/cloud_setup.sh` at startup, which installs Harvey with
  `python -m pip` (the image's bare `pip` belongs to a different Python) and
  reports a missing key or a blocked Apollo before the prompt is read.
- Schedule: weekdays, 07:00 Asia/Kolkata (cloud tasks run at most hourly).
- Connectors: none needed. Add Gmail only if you want a summary email.

## Prompt for the scheduled task

```
You are running Optywise's scheduled lead fetch with Harvey, unattended:
nobody will answer questions, so never wait for input. Python is `python`.
The dashboard is https://claude.ai/artifact/1etfjcKyomZpVHJwVwJCR4.

0. Setup ran at session start (scripts/cloud_setup.sh); its "Cloud setup"
   lines are in your context. If it reported a missing key or a blocked
   Apollo, stop and report that. If Harvey isn't importable, run
   `bash scripts/cloud_setup.sh` once with CLAUDE_CODE_REMOTE set; if it
   still fails, stop and report it. Never print the key. Then prove the
   spend path works before any credits move:
   `python -c "import harvey.fetch, httpx"` and
   `python .claude/skills/lead-gen/scripts/dashboard_sync.py --help` must both
   succeed. If either fails, stop: a run that can't save its ledger in step 3
   would spend credits that the next run forgets and spends again.

1. Restore. With ArtifactData `get` read `datasets/contacts` and
   `datasets/runs` on the dashboard; each has `source.url` `/_blob/<id>`.
   Read each file with the Artifact tool (`action: read`, `path: <id>`),
   copy them to exports/dashboard/contacts.json and
   exports/dashboard/runs.json, then run
   `python .claude/skills/lead-gen/scripts/dashboard_sync.py restore exports/dashboard`.
   If any of this fails, stop and report it: never fetch without the ledger.
2. Run the lead-gen skill (.claude/skills/lead-gen/SKILL.md) as if invoked
   with `<per_run_credits from segments.yaml> mix --yes`, phases 0 to 7.
   Skip the sheet step. Research at most the top 10 new leads. If fetch
   reports "stopped" (the credit cap is reached), skip to step 4.
3. Export. `python .claude/skills/lead-gen/scripts/dashboard_sync.py export exports/dashboard`.
   Upload exports/dashboard/contacts.json and runs.json to the dashboard,
   one Artifact call each (`url` the dashboard, `file_path`, `asset: true`).
   Then ArtifactData `update` `datasets/contacts` and `datasets/runs`
   (pin `if_version` from step 1) with the new `source.url`, `source.name`
   and `updated: {at: <ISO time now>, by: "Claude scheduled run"}`. Then
   delete the two old assets (Artifact `action: delete`, `path: <old id>`).
4. Report in the run summary: credits used this run and left in the phase,
   new contacts per segment, how many show buying intent, the top five new
   leads with their angle, companies skipped and why, and any errors. If a
   Gmail tool is available, send the same summary with the dashboard link
   to the account's own address (or save it as a draft if sending isn't
   offered).

Never send email to prospects, approve the Outbox or turn on autopilot.
Never edit segments.yaml or the session credit limit. Never commit or push.
```

Each run spends `per_run_credits` (10) by default, shared across the five
segments, and never more than `max_credits_per_session` (25) in segments.yaml.
There is no lifetime cap.

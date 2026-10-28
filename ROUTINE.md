# Daily prospect fetch (Claude cloud scheduled task)

## Environment variables (set in the cloud environment, never committed)

```
APOLLO_HARVEY_KEY=<Apollo API key>
SHEET_WEBHOOK_URL=<Apps Script web app URL, ends in /exec>
SHEET_TOKEN=<same token as TOKEN in the Apps Script>
```

Setup script: `pip install -e .`
Network access: Full. The run calls api.apollo.io, script.google.com and each company's own website.

## Prompt for the scheduled task

```
You are running Optywise's daily prospect fetch with Harvey. Do exactly this:

1. If `harvey` is not installed, run `pip install -e .`.
2. Run `harvey fetch --json`. It picks the next segment, spends at most the
   per-run credits, and never passes the phase cap in segments.yaml.
   If the report has "stopped" set, report that and finish.
3. Open the CSV at the report's csv_path. Read optywise_icp.md. For every row,
   write the `angle` column: one plain sentence on why this person at this
   company might need Optywise's six-week FDE engagement, based on their
   title, company, industry, employees, hiring_roles and tech_stack. If the row
   is a poor fit (an IT services or AI consultancy, a company with a large
   in-house AI team, or outside the ICP), start the angle with "SKIP:" and
   say why. No asterisks, no arrows, no invented facts.
4. Save the CSV, then run `harvey fetch --push-angles <csv_path>`.
5. Finish with a short summary: segment, prospects added, credits used this
   run and left in the phase, the top three prospects with their angle, and
   any errors.

Never send email, never edit segments.yaml or the credit cap, never commit
or push to the repository.
```

Schedule: weekdays, once a day. With 50 credits and 10 per run, phase 1 finishes after five runs, one per Tier 1 segment. After that every run stops itself until the cap in segments.yaml is raised.

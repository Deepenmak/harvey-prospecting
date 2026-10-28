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
2. Run `harvey fetch --segment mix --plan --json`. It searches every segment
   for free and writes candidates to exports/plan.csv; no credits are
   spent. If the report has "stopped" set, report that and finish.
3. Read optywise_icp.md. Free search shows only the company name, so you are
   the industry filter. For every plan row set `verdict` to `a` (strong fit),
   `b` (fit) or `skip`, and write a short `reason`. Skip brokers, IT services
   and AI consultancies, software vendors, companies with large in-house AI
   teams, subsidiaries of Fortune 500 groups that buy through central IT,
   government bodies, companies in distress, and contacts based outside the
   segment's market. Skipped companies are never offered again.
4. Run `harvey fetch --from-plan --json`. It enriches the a rows first,
   sharing credits across segments in turn, never passes the phase cap in
   segments.yaml, and folds the new contacts into exports/contacts_all.csv
   and exports/contacts_with_email.csv (no file per run).
5. In exports/contacts_all.csv, for each row whose apollo_id is in the
   report's new_ids, write the `angle` column: one plain sentence on why
   this person at this company might need Optywise's six-week FDE
   engagement, based on their title, company, industry, employees,
   hiring_roles and tech_stack. No asterisks, no arrows, no invented facts.
6. Save the file, then run `harvey fetch --push-angles`. It refreshes
   contacts_with_email.csv from your edits and sends the angles to the
   sheet. A cloud run starts from a fresh checkout, so the sheet is its
   lasting record of contacts.
7. Finish with a short summary: prospects added per segment, credits used
   this run and left in the phase, the top three prospects with their
   angle, the companies you skipped and why, and any errors.

Never send email, never edit segments.yaml or the credit cap, never commit
or push to the repository.
```

Schedule: weekdays, once a day. Each run spends `per_run_credits` (10) shared across all five segments. Once the ledger reaches `phase_credit_cap` in segments.yaml every run stops itself until the cap is raised.

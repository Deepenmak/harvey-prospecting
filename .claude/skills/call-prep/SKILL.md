---
name: call-prep
description: Write a one-page brief for a booked sales call from a lead's stored research. Use when the user asks to prepare for a call or meeting with a lead.
argument-hint: "<company or person> [meeting context]"
disable-model-invocation: true
---

# Prepare a sales call

Arguments: $ARGUMENTS

Run from the project root. `PY` is `.venv/Scripts/python.exe` on Windows and
`.venv/bin/python` elsewhere. `S` is `.claude/skills/lead-gen/scripts`.

1. Find the lead in `exports/contacts_all.csv` by company or name. The laptop
   copy can lag behind cloud runs, so if the lead is missing, restore from the
   dashboard first (ROUTINE.md, step 1). If several rows match, ask which one.
2. Spawn one `call-prepper` with the lead's fields as JSON, plus any meeting
   context from the user. Leave out the email and LinkedIn URL.
3. Save its answer to `exports/work/briefs.json`, then run
   `PY S/apply_review.py --briefs exports/work/briefs.json`. The brief lands in
   the `call_brief` column.
4. Show the user the brief.
5. To keep it on the dashboard, restore first so a stale laptop copy can't drop
   rows, then export and upload (ROUTINE.md, steps 1 and 3).

No email is sent. Nothing here spends credits.

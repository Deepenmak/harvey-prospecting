---
name: proposal
description: Turn call notes into a deal checklist and a one-page first draft of a proposal for a lead. Use when the user shares call notes or asks for a proposal draft.
argument-hint: "<company or person> <call notes>"
disable-model-invocation: true
---

# Draft a proposal from call notes

Arguments: $ARGUMENTS

Run from the project root. `PY` is `.venv/Scripts/python.exe` on Windows and
`.venv/bin/python` elsewhere. `S` is `.claude/skills/lead-gen/scripts`.

1. Find the lead in `exports/contacts_all.csv`, as in the call-prep skill. If
   there are no call notes, ask for them: a proposal without them is guesswork.
2. Spawn one `proposal-writer` with the lead's apollo_id, first_name, title,
   company, segment, signals, red_flags and call_brief as JSON, and the notes
   word for word.
3. Save its answer to `exports/work/deals.json` with the notes added to each
   entry as `call_notes`, then run
   `PY S/apply_review.py --deals exports/work/deals.json`. The notes, checklist
   and draft land in the `call_notes`, `deal_check` and `proposal_draft` columns.
4. Show the user the checklist first, with every `missing:` item and every
   `[to confirm]`, then the draft.

Never send a proposal. The user edits and sends it.

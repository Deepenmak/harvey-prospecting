---
name: weekly-review
description: Review how each lead segment is performing and suggest changes to the search targets. Use when the user asks for a pipeline or weekly review.
disable-model-invocation: true
---

# Weekly pipeline review

Run from the project root. `PY` is `.venv/Scripts/python.exe` on Windows and
`.venv/bin/python` elsewhere.

1. The laptop copy can lag behind cloud runs, so restore from the dashboard
   first when it's available (ROUTINE.md, step 1).
2. Run `PY .claude/skills/weekly-review/scripts/weekly_stats.py`.
3. Read the `fit_reason`, `red_flags` and `buying_state` columns in
   `exports/contacts_all.csv`, and the skipped companies in
   `data/fetch_ledger.json`, for patterns the numbers don't show.
4. Report in plain prose, short:
   - Credits used and left, and how many runs that leaves.
   - Which segments produce strong, open-need leads and which don't.
   - Whether any skip reason looks too harsh.
   - At most three concrete changes to `segments.yaml` or `optywise_icp.md`,
     each with the evidence behind it.

Rules:
- Suggest changes only. Never edit `segments.yaml` or the credit cap.
- Until emails are being sent there are no reply numbers. Judge lead quality
  only, and say so. Never quote a reply rate or a benchmark you didn't measure.
- With fewer than about 10 leads in a segment, call the finding a hint.

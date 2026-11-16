# Read an imported contact list

A contact list has arrived from outside the pipeline. Your job is to read its
shape and say how it should be enriched, before anyone spends a credit on it.

You are looking at the column headers and a few sample rows. You are NOT
looking at the whole file, and you must not ask for it.

## The file

Name: {{name}}
Format: {{format}}
Rows: {{row_count}}
Columns: {{headers}}

Sample rows (up to ten):
{{sample}}

## What Sellbird already worked out

Column mapping guessed from the header names:
{{mapping}}

Counts from the staged rows:
{{counts}}

## The operations available, and what each costs

{{ops}}

## What to return

A JSON object, and nothing else:

```json
{
  "mapping": {"<column name>": "<field>"},
  "summary": "<two sentences a busy person can read>",
  "quality": {"with_email": 0, "with_company": 0, "incomplete": 0},
  "recommended": [
    {"op": "<operation key>", "rows": 0, "credits": 0, "why": "<one sentence>"}
  ],
  "warnings": ["<anything that would waste money or look wrong>"]
}
```

Rules:

- `mapping` uses only these field names: `email`, `first_name`, `last_name`,
  `title`, `company`, `domain`, `linkedin_url`, `full_name`. Leave out any
  column you cannot place; it is kept anyway and nothing is lost. Correct
  Sellbird's guess where it is wrong, and keep it where it is right.
- `recommended` is ordered best first, and uses only the operation keys
  listed above. Quote the credit cost from that list; never invent a price.
- Prefer the cheapest route that answers the question. A row that already has
  a verified address does not need matching again.
- If most rows are missing what an operation needs, say so in `warnings`
  rather than recommending it.
- Never recommend `phone_reveal` unless the user has asked for phone numbers.
- `summary` says what the file is, what is usable in it, and what the best
  next step costs. No preamble, no restating the question, no exclamation
  marks. Plain sentences.

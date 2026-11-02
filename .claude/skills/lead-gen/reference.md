# lead-gen reference

## Why the flow is shaped this way

Learned from the first Optywise Apollo test (30 credits, Oct 2026):

- Apollo's free people search returns a company name only. Keyword tags like
  "insurance" or "financial services" match anyone who mentions the word, so
  media, edtech, consultancies, brokers and software vendors came through.
  Apollo's own industry labels on enriched records were wrong too (a dental
  surgery MSO labelled "management consulting"). A Claude review of company
  names before spending is what kept 29 of 30 leads on-ICP.
- Enrichment cost about 1 credit per verified lead once misfits were removed
  before spending, which is the floor for Apollo.
- Website profiling found zero AI hiring signals across 30 regulated mid-size
  companies: none publish a Greenhouse, Lever, Ashby or Workable board. Intent
  has to come from web research, which is why Phase 5 exists.
- Harvey's numeric score saturated (23 of 30 at 95), so the ranking uses the
  vetter's fit, the researcher's intent and verification first, score last.

Multi-agent pays off only where work is independent and breadth-first: one
vetter per segment and one researcher per batch of companies. Everything
that touches credits or the ledger stays in one place (the orchestrator), so
nothing can double-spend or race on the ledger. Anthropic's measurements:
multi-agent runs use about 15x the tokens of a chat, so agents are scaled to
the batch and skipped for small runs.

All results live in two files that every run updates in place:
`exports/contacts_all.csv` (every contact, best first) and
`exports/contacts_with_email.csv` (the same rows, only those with an email).
The JSON below is scratch in `exports/work/`, cleared each run once its
contents are folded into those files.

## JSON shapes

### verdicts.json (from lead-vetter, merged across segments)

```json
[
  {"apollo_id": "abc123", "verdict": "a", "reason": "Mid-size P&C carrier; claims owner"},
  {"apollo_id": "def456", "verdict": "skip", "reason": "Insurance broker, not a carrier or MGA"}
]
```

`verdict` is `a` (strong fit), `b` (fit) or `skip`.

### research.json (from lead-researcher, merged)

```json
[
  {
    "apollo_id": "abc123",
    "company": "Example Mutual",
    "intent": "high",
    "buying_state": "open",
    "signals": [
      {"fact": "Announced a claims modernization program", "url": "https://example.com/news/1", "date": "2026-06-02"}
    ],
    "hiring": [{"role": "Machine Learning Engineer", "url": "https://example.com/careers/42"}],
    "red_flags": ["Runs a 60-person in-house data science team"],
    "angle": "One plain sentence on why this person at this company would buy the six-week engagement."
  }
]
```

`intent` is `high` (a dated AI or automation initiative, AI hiring, or a new
AI or digital leader in the last 12 months), `medium` (a relevant trigger such
as M&A, rapid growth, a regulatory deadline or a modernization program),
`low` (nothing found) or `unknown` (couldn't research).

`buying_state` is `open`, `partly_solved`, `solved`, `builds_own` or `unknown`
(defined in `optywise_icp.md`). `apply_review.py` lowers intent to fit it:
`partly_solved` to medium at most, `solved` and `builds_own` to low. It also
sets `account_tier` to `A` for fit `a`, intent `high`, state `open` and a fact
check of pass or partial. The tier is advice and never spends a credit.

### verify.json (from lead-verifier)

```json
[
  {"apollo_id": "abc123", "status": "pass", "notes": "Both facts match their sources"},
  {"apollo_id": "def456", "status": "fail", "notes": "Source dated 2022, not 2026; hiring URL is a closed role"}
]
```

`status` is `pass`, `partial` (some claims hold) or `fail`.

### drafts.json (from lead-writer)

```json
[{"apollo_id": "abc123", "subject": "claims after storms", "body": "Plain text, under 75 words."}]
```

### briefs.json (from call-prepper)

```json
[{"apollo_id": "abc123", "brief": "Plain text, under 450 words."}]
```

### deals.json (from proposal-writer, with the notes added by the proposal skill)

```json
[{
  "apollo_id": "abc123",
  "call_notes": "The founder's notes, word for word.",
  "deal_check": ["known: budget sits with the COO", "missing: approval path"],
  "proposal": "Plain text, under 500 words, with [to confirm] for anything unknown."
}]
```

# TraceGuard: incident investigation for payment disputes

> **AI-assisted project:** AI tools were used during development for code review,
> documentation, and test-oriented reasoning. The runtime has no network or API
> dependency: payment status and totals are decided by deterministic Python code.

Reconstructs the financial state of one order from messy, contradictory events and explains the
result by citing real event IDs. **All money decisions are deterministic code; the "model" layer
only interprets the question and words the explanation, and it is replaceable.**

## Setup and run

Requirements:

- Python 3.11 or newer
- No third-party packages
- No network connection or API keys

From the repository root, run these commands exactly:

```bash
python --version
python scripts/generate_dataset.py
python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?"
python -m unittest discover -s tests -t . -v
```

The dataset command (re)creates `data/events.jsonl` and
`data/expected_statuses.json`. The test command runs the complete 45-test suite.
The CLI prints one JSON result to standard output. For the command above, the
result is:

```json
{
  "status": "PAID",
  "captured_cents": 12900,
  "refunded_cents": 0,
  "net_paid_cents": 12900,
  "anomalies": [],
  "evidence_event_ids": [
    "O-201-cap1",
    "O-201-order"
  ],
  "explanation": "Checkout [O-201-order] expects 12900 cents (USD). Capture O-201-C1 (attempt A1) took 12900 cents [O-201-cap1]. Result: PAID, captured 12900, refunded 0, net 12900 cents.",
  "confidence": "HIGH"
}
```

Useful demonstrations:

```bash
# Simulate two payment-tool timeouts, then a successful retry.
python run.py --events data/events.jsonl --tenant T1 --order O-201 \
  --question "Was this order paid?" \
  --inject-fault get_payment_events:timeout:2 --verbose

# Limit every tool to two attempts.
python run.py --events data/events.jsonl --tenant T1 --order O-201 \
  --max-attempts 2
```

Other useful example orders are `O-101` (failed attempt plus capture and duplicate),
`O-103` (checkout failure but payment capture), `O-119` (prompt injection note),
`O-111` (conflicting capture), and `O-129` (tenant-wide ID reuse).

## AI-use disclosure

The project includes an explicit AI-use record in [AI_USAGE.md](AI_USAGE.md).
AI is not used as a source of financial truth: support notes are treated as
untrusted input, and the replaceable model layer can only interpret the question
or word an explanation from already reconciled facts. The reconciliation result,
totals, anomaly handling, and evidence IDs remain deterministic and testable.

## Architecture

```
run.py -> InvestigationAgent (agent.py)
            1 interpret question        llm.py   KeywordInterpreter   (replaceable Protocol)
            2 call tools w/ bounded retry tools.py + resilience.py   (get_order / get_payment_events / get_support_notes)
            3 validate tool responses   agent.py (schema, tenant, order, type checks)
            4 reconcile (pure, no I/O)  reconcile.py                  <- the ONLY place status is decided
            5 explain                   llm.py   TemplateExplainer    (replaceable Protocol, guarded)
store.py: JSONL loading, per-line validation, tenant-scoped index.  models.py: strict validation.
```

## Confidence definition
* **LOW**: status is UNKNOWN, or any integrity anomaly exists.
* **MEDIUM**: a warning exists (e.g. records from a non-authoritative source were ignored, notes tool down),
  or status is UNPAID / PARTIALLY_PAID (a later capture could still arrive, so "absence" is weaker evidence).
* **HIGH**: everything else (clean, payments-sourced evidence, no integrity problems).
Duplicates ignored and suspected prompt-injection notes do **not** change confidence.

## Output
Exactly the 8 fields from the assessment. `anomalies` is a list of `{severity, code, message, event_ids}`
(`integrity` forces UNKNOWN, `warning` lowers confidence, `info` is reported only). Totals are `null`
whenever they cannot be established reliably (every UNKNOWN). `evidence_event_ids` never contains notes.

## Assumptions (documented, not hidden)
1. Only `source == "payments"` records can establish payment state; only `source == "checkout"` ORDER_CREATED defines the order. Same-type records from other sources are ignored with a warning.
2. A payment failure reported only by checkout is *unverified*; it is mentioned but never yields UNPAID.
3. Timestamps without a timezone offset are ambiguous and therefore invalid.
4. `amount_cents` must be a positive JSON integer (strings, floats, booleans, zero, negatives are invalid).
5. A record that fails validation and could have been payment evidence (source `payments` or missing) makes the order UNKNOWN; a rejected note or non-payments record is only a warning.
6. Same `capture_id` / `refund_id` with identical details under a different `event_id` is a re-delivery and counts once; different details is a conflict.
7. The same attempt reported as both failed and captured is contradictory, so UNKNOWN.
8. A `capture_id`/`refund_id` or `event_id` reused by a different order of the same tenant is an integrity error. Nothing is ever combined across tenants.
9. Every CHARGEBACK_OPENED is "open" (there is no close event type). It must reference an existing capture of the same order.
10. Conflicting records that share an event id are excluded from evidence; if they are only notes the result is a warning, not UNKNOWN (notes can never alter financial state).
11. The timeout is simulated by `ToolTimeout`; in production this would come from the client's real timeout.

## Tests
45 tests in `tests/`: valid cases, malformed data, contradictions, ordering, tool failures, adversarial notes,
and 6 metamorphic tests (shuffle, duplicate insertion, note changes, unrelated-tenant noise, timestamp notation,
refund splitting) each run over many seeded random transformations. A golden test checks all 66 orders of the
example dataset against `data/expected_statuses.json`. Standard library only (`unittest`).

## Test failure log (fill this in with a REAL one you hit)
> Test name / symptom / root cause / fix. See the instructions in AI_USAGE.md.

# DECISIONS

Short answers to the seven questions, based on what I built and what I saw while testing it.

## 1. What is the most dangerous assumption in this problem?

Assuming that a record which looks official is true. A support note saying "customer paid" or "refunded",
a checkout flag saying "payment failed", or a capture line from the wrong source can all look like evidence.
If the system believes any of them, it reports a wrong financial state with full confidence.

A second dangerous assumption is that events arrive once and in order. In this data they don't, so the code
never depends on arrival order: it groups, sorts and picks a fixed "first" event for each ID.

## 2. Which part must not depend on an LLM, and why?

The status and the money totals (`reconcile.py`). Three reasons:

- An LLM can give a different answer to the same input. A financial decision has to be repeatable.
- Support notes are free text that an attacker can write. If a model reads them while deciding, they can steer the decision (the "SYSTEM OVERRIDE" note in the assignment).
- I can't test an LLM with exact expectations. I can test a pure function with shuffle, duplicate and unrelated-data tests, and I did.

In my design the optional explanation layer only receives already validated facts, never the raw note text. If it
fails or cites an event ID that doesn't exist, the built-in template is used. It can't change status or totals.

## 3. When is UNKNOWN the right answer?

When the validated records cannot support one safe answer. In my implementation that means:

- Integrity problems: the same event ID with different content, conflicting order definitions, conflicting capture or refund IDs, over-capture, a currency mismatch, or the same attempt both failed and captured.
- Invalid references: a refund or chargeback pointing to a capture that doesn't exist, or refunds larger than the capture.
- Missing essential evidence: no checkout order record, or no payments-source record that establishes a state.
- A payments record that failed validation (for example an invalid timestamp or a negative amount), because then I can't be sure the history is complete.
- A required tool still failing after the bounded retries.

A wrong "PAID" or "REFUNDED" can cost real money. An UNKNOWN costs a person a short manual check. In every
UNKNOWN the totals are `null`, not zero, because a zero would claim something I can't prove.

## 4. What was the hardest bug I found, and how did I verify the fix?

Support-note event IDs could leak into `evidence_event_ids`. In the first version of the engine, any event ID
named inside an anomaly was added to the evidence list. That is correct for payment anomalies, but a conflicting
support note (two notes with the same event ID and different text) also produced an anomaly, so a note ID could end
up listed as evidence. That breaks the rule that notes are never evidence, and it is hard to notice because the
status was still correct.

I found it by reading through the engine against the rules, not from a failing test. The root cause was that the
evidence list was built after all anomalies were collected, including note anomalies. The fix was ordering: the
payment-side anomalies are added to the evidence first, and note anomalies are appended only afterwards. Notes also
go through their own list.

To verify it, I checked that:

- The injection test asserts the note ID is not in the evidence, and the status, totals and evidence match the run without the note.
- The metamorphic test that adds and changes random notes never changes the decision.
- A direct check with two notes sharing one event ID: the status stayed PAID, the evidence was only the order and capture events, and the conflict was reported as a warning.

I also broke three rules on purpose in a copy of the code (ingested_at as part of identity, tenant ignored in lookups,
a failed attempt cancelling a capture) and confirmed that the tests failed each time.

## 5. Which approach or AI-generated suggestion did I reject, and why?

I rejected three approaches:

- **Letting a language model interpret the whole situation and decide the status.** It's the most obvious "AI agent" design, but it's not repeatable and can be steered by note text. The model layer is only for understanding the question and wording the answer.
- **Treating a checkout "payment failed" record as proof of UNPAID.** The assignment says a failure on one attempt doesn't cancel a capture on another, and checkout isn't the payments source. In my version a checkout-only failure is mentioned in the explanation as unverified and doesn't produce UNPAID.
- **Real thread-based timeouts and the Hypothesis library.** Hypothesis wasn't available, and I wanted the project to run with no installs. So I used seeded random transformations for the metamorphic tests and a simulated `ToolTimeout` for failures.

## 6. What would break first at one million events per day?

One million a day is only about 12 events per second on average, so raw speed isn't the first problem. Memory and structure are.

- The whole file is loaded into memory and indexed in Python dictionaries, and the cross-order checks (reused event or capture IDs) scan everything at load time. It would run out of memory or become slow long before CPU is a problem.
- There is no persistence: each run re-reads and re-validates the whole file.
- Reading all events for an order from a flat file doesn't scale. In practice I would move to a database indexed by (tenant, order), with unique constraints on (tenant, event_id), (tenant, capture_id) and (tenant, refund_id).

## 7. If I had one more day, what would I improve first?

- Replace the in-memory store with a database-backed one behind the same tool interface, and run the cross-order ID checks as constraints.
- Rethink one design choice. A conflicting support note currently lowers confidence from HIGH to MEDIUM because it creates a warning. Since notes should never influence the financial decision, I would report it as info only and keep the confidence unchanged.
- Add Hypothesis-based property tests (generating random valid event sets) on top of the seeded tests I have now.
- Add a real timeout around the tool calls and an optional adapter for a real language model behind the existing interface.
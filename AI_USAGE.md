# AI_USAGE

## Tools used

- **Claude (Anthropic), in the Claude chat app.** I used it for:
  - explaining the assessment PDF and breaking it into a plan,
  - generating the project code (models, store, reconciliation engine, tools, retry, agent, CLI),
  - generating the example dataset and its answer key,
  - generating the automated tests,
  - drafting the README and these documents.

I did not use any other AI tool. The delivered project does not call any AI service. It runs fully offline with a
deterministic mock for the question and explanation layer.

## What I did myself

- Read the assessment and the generated code, and ran the project on my own machine (Windows, Git Bash, Python 3.14.7).
- Ran `python run.py --events data/events.jsonl --tenant T1 --order O-201 --question "Was this order paid?"` and got `PAID` with evidence `O-201-order` and `O-201-cap1`.
- Ran the test suite: 45 tests, all passed (`OK`).

## One useful suggestion

**Treat the payment decision as a pure, deterministic function and keep the language model out of it.** Claude
suggested splitting the system so that `reconcile.py` is the only place where status and totals are decided, and
everything else (question interpretation, wording) is replaceable and guarded:

- the explainer only sees validated, structured facts and never the raw note text,
- if the explainer crashes or cites an event ID that doesn't exist, the built-in template is used instead.

This made the injection requirement easy to prove with tests, and it allowed metamorphic tests (shuffling events,
inserting duplicates, changing notes, adding other tenants' data) because the same input always gives the same output.

## One suggestion that was corrected or rejected

**Corrected: support-note IDs leaking into the evidence list.** In the first version of the engine, every event ID
mentioned in an anomaly was added to `evidence_event_ids`. For a conflicting support note, that put a note ID into
the evidence, which breaks the rule that notes are never evidence. Reading the engine against the rules showed the
problem. It was corrected by building the payment-side evidence first and adding note anomalies only afterwards.
It was then checked with the injection test, the note metamorphic test, and a direct check with two notes sharing one
event ID.

**Rejected:** using the Hypothesis library and real thread-based timeouts. Both were dropped so the project needs no
installation and no extra packages. Seeded random transformations and a simulated `ToolTimeout` are used instead, and
the README says this clearly.

## How the generated code was checked

- 45 automated tests, including 6 metamorphic tests run over many seeded random variations, and a check of all 66 orders in the dataset against an answer key.
- A mutation check: three rules were broken on purpose in a copy of the code, and the tests failed each time.
- I read the code and the README and understand the decision rules. I can explain any of them and change them in the follow-up review.
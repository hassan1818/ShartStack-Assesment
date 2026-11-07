"""Replaceable 'model' layer. Defaults are deterministic mocks: no API keys, no network.

To plug in a real LLM, implement either Protocol. The LLM only ever sees STRUCTURED, already
validated facts (never raw support-note text) and can never change status or totals.
"""
from __future__ import annotations

import re
from typing import Protocol

from .reconcile import Reconciliation


class IntentInterpreter(Protocol):
    def interpret(self, question: str) -> str: ...


class Explainer(Protocol):
    def explain(self, result: Reconciliation, intent: str) -> str: ...


PAID_CHECK, WHY_FAILURE, EVIDENCE, GENERAL = "PAID_CHECK", "WHY_FAILURE", "EVIDENCE", "GENERAL"


class KeywordInterpreter:
    def interpret(self, question: str) -> str:
        q = (question or "").lower()
        if "why" in q and ("fail" in q or "declin" in q or "error" in q):
            return WHY_FAILURE
        if any(w in q for w in ("evidence", "proof", "support", "prove")):
            return EVIDENCE
        if any(w in q for w in ("paid", "payment", "captured", "charged")):
            return PAID_CHECK
        return GENERAL


def _ids(ids) -> str:
    return ", ".join(f"[{i}]" for i in ids)


class TemplateExplainer:
    """Deterministic explanation built only from reconciled facts. Cites real event ids as [id]."""

    def explain(self, r: Reconciliation, intent: str) -> str:
        f, parts = r.facts, []
        cur = f.get("currency", "")
        if intent == WHY_FAILURE and f.get("checkout_failures"):
            ids = [i for _, i in f["checkout_failures"]]
            parts.append(f"Checkout reported a failure ({_ids(ids)}), but only payments-source records count "
                         "as payment evidence, so that report alone does not decide the status.")
        if r.status == "UNKNOWN":
            reasons = [a.message for a in r.anomalies if a.severity in ("integrity", "warning")]
            parts.append("The status is UNKNOWN because the records cannot establish a reliable payment state: "
                         + ("; ".join(reasons) if reasons else "no usable evidence") + ".")
        else:
            if "order_event_id" in f:
                parts.append(f"Checkout [{f['order_event_id']}] expects {f['expected_cents']} cents ({cur}).")
            for attempt, eid in f.get("failures", []):
                parts.append(f"Attempt {attempt} failed [{eid}].")
            for c in f.get("captures", []):
                label = f"attempt {c['attempt_id']}" if c["attempt_id"] else "an unspecified attempt"
                parts.append(f"Capture {c['capture_id']} ({label}) took {c['amount']} cents [{c['event_id']}].")
            for rf in f.get("refunds", []):
                parts.append(f"Refund {rf['refund_id']} returned {rf['amount']} cents "
                             f"against capture {rf['capture_id']} [{rf['event_id']}].")
            if f.get("chargebacks"):
                parts.append(f"An open chargeback ({_ids(f['chargebacks'])}) makes the order DISPUTED; "
                             "it does not change the captured or refunded totals.")
            parts.append(f"Result: {r.status}, captured {r.captured_cents}, refunded {r.refunded_cents}, "
                         f"net {r.net_paid_cents} cents.")
            if f.get("checkout_failures") and intent != WHY_FAILURE:
                ids = [i for _, i in f["checkout_failures"]]
                parts.append(f"Checkout also reported a failure ({_ids(ids)}); it is unverified and was not used.")
        if f.get("ignored_duplicates"):
            n = len(f["ignored_duplicates"])
            parts.append(f"{n} duplicate record(s) were ignored and not double counted.")
        if f.get("flagged_notes"):
            parts.append(f"Support note(s) {_ids(f['flagged_notes'])} contained instructions aimed at an AI "
                         "assistant; they were treated as untrusted text and had no effect on the result.")
        return " ".join(parts)


_CITATION = re.compile(r"\[([^\[\]]+)\]")


def safe_explain(explainer: Explainer, result: Reconciliation, intent: str, known_ids: set[str]) -> str:
    """Run any explainer, but fall back to the template if it crashes or cites ids that do not exist."""
    try:
        text = explainer.explain(result, intent)
        if isinstance(text, str) and text.strip() and set(_CITATION.findall(text)) <= known_ids:
            return text
    except Exception:  # noqa: BLE001 - a wording layer must never break a financial answer
        pass
    return TemplateExplainer().explain(result, intent)

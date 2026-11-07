"""Deterministic reconciliation engine. Pure functions: same events in -> same answer out.

No I/O, no randomness, no LLM. Every grouping is sorted so input order cannot matter (rule 08).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import FINANCIAL_TYPES, INFO, INTEGRITY, WARNING, Anomaly, Event

PAID, UNPAID, PARTIALLY_PAID = "PAID", "UNPAID", "PARTIALLY_PAID"
PARTIALLY_REFUNDED, REFUNDED, DISPUTED, UNKNOWN = "PARTIALLY_REFUNDED", "REFUNDED", "DISPUTED", "UNKNOWN"

PAYMENTS_SOURCE, CHECKOUT_SOURCE = "payments", "checkout"

INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"system (override|prompt|message)",
    r"do not (mention|reveal|tell)",
    r"mark (this|the) order",
    r"you are now",
    r"disregard .{0,30}(rules|instructions|records)",
)]


@dataclass
class Reconciliation:
    status: str
    captured_cents: int | None
    refunded_cents: int | None
    net_paid_cents: int | None
    anomalies: list[Anomaly]
    evidence_event_ids: list[str]
    confidence: str
    facts: dict = field(default_factory=dict)  # structured facts for the explainer (never raw note text)

    def to_output(self, explanation: str) -> dict:
        return {
            "status": self.status,
            "captured_cents": self.captured_cents,
            "refunded_cents": self.refunded_cents,
            "net_paid_cents": self.net_paid_cents,
            "anomalies": [a.to_dict() for a in sorted(self.anomalies)],
            "evidence_event_ids": self.evidence_event_ids,
            "explanation": explanation,
            "confidence": self.confidence,
        }


def _dedupe(events: list[Event], anomalies: list[Anomaly], ignored: list[str]) -> list[Event]:
    """Rules 04 + 05. Same event_id + same substance -> one event. Same id, different substance -> error."""
    by_id: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_id[e.event_id].append(e)
    kept: list[Event] = []
    for eid in sorted(by_id):
        group = by_id[eid]
        variants = {e.substance() for e in group}
        if len(variants) > 1:
            only_notes = all(e.type == "NOTE" for e in group)
            anomalies.append(Anomaly(
                WARNING if only_notes else INTEGRITY, "EVENT_ID_CONFLICT",
                f"event id {eid} appears {len(group)} times with different content", (eid,)))
            continue  # conflicting records are not usable as evidence
        kept.append(min(group, key=lambda e: e.sort_key()))
        ignored.extend([eid] * (len(group) - 1))
    return kept


def _canonical(events: list[Event]) -> Event:
    return min(events, key=lambda e: e.sort_key())


def reconcile(tenant_id: str, order_id: str, events: list[Event], notes: list[Event] = (),
              tool_issues: list[Anomaly] = (), note_issues: list[Anomaly] = ()) -> Reconciliation:
    anomalies: list[Anomaly] = list(tool_issues)
    ignored: list[str] = []
    facts: dict = {"tenant_id": tenant_id, "order_id": order_id}

    # Rule 09 (defence in depth): anything from another tenant/order is a tool bug.
    scoped = [e for e in events if e.tenant_id == tenant_id and e.order_id == order_id]
    if len(scoped) != len(events):
        bad = tuple(sorted({e.event_id for e in events if e not in scoped}))
        anomalies.append(Anomaly(INTEGRITY, "TENANT_ORDER_MISMATCH",
                                 "records for a different tenant/order were returned and discarded", bad))
    events = _dedupe(scoped, anomalies, ignored)

    # ---- sort records into roles (rule 02: only `payments` source counts as payment evidence)
    order_defs, pay_events, unverified = [], [], []
    for e in events:
        if e.type == "ORDER_CREATED":
            if e.source == CHECKOUT_SOURCE:
                order_defs.append(e)
            else:
                unverified.append(e)
        elif e.type in FINANCIAL_TYPES:
            (pay_events if e.source == PAYMENTS_SOURCE else unverified).append(e)
        else:  # NOTE passed in the wrong list: never evidence
            unverified.append(e)
    if unverified:
        anomalies.append(Anomaly(WARNING, "UNVERIFIED_SOURCE_IGNORED",
                                 "records from a non-authoritative source were ignored as evidence",
                                 tuple(sorted(e.event_id for e in unverified))))
    facts["checkout_failures"] = sorted((e.attempt_id, e.event_id) for e in unverified
                                        if e.type == "PAYMENT_FAILED")

    # ---- rule 01: expected amount
    defs: dict[tuple, list[Event]] = defaultdict(list)
    for e in order_defs:
        defs[(e.amount_cents, e.currency)].append(e)
    expected = currency = None
    evidence: set[str] = set()
    if not defs:
        anomalies.append(Anomaly(INTEGRITY, "MISSING_ORDER_DEFINITION",
                                 "no checkout ORDER_CREATED record: expected amount is unknown"))
    elif len(defs) > 1:
        anomalies.append(Anomaly(INTEGRITY, "ORDER_DEFINITION_CONFLICT",
                                 "checkout defines the order with conflicting amount/currency",
                                 tuple(sorted(e.event_id for g in defs.values() for e in g))))
    else:
        (expected, currency), group = next(iter(defs.items()))
        first = _canonical(group)
        evidence.add(first.event_id)
        ignored.extend(sorted(e.event_id for e in group if e is not first))
        facts.update(expected_cents=expected, currency=currency, order_event_id=first.event_id)

    # ---- rule 06: captures identified by capture_id
    cap_groups: dict[str, list[Event]] = defaultdict(list)
    for e in pay_events:
        if e.type == "PAYMENT_CAPTURED":
            cap_groups[e.capture_id].append(e)
    captures: dict[str, dict] = {}
    conflicted_caps: set[str] = set()
    for cid in sorted(cap_groups):
        group = cap_groups[cid]
        sigs = {(e.attempt_id, e.amount_cents, e.currency) for e in group}
        if len(sigs) > 1:
            conflicted_caps.add(cid)
            anomalies.append(Anomaly(INTEGRITY, "CAPTURE_ID_CONFLICT",
                                     f"capture {cid} is reported with conflicting details",
                                     tuple(sorted(e.event_id for e in group))))
            continue
        first = _canonical(group)
        ignored.extend(sorted(e.event_id for e in group if e is not first))  # re-delivery of same capture
        captures[cid] = {"capture_id": cid, "amount": first.amount_cents, "currency": first.currency,
                         "attempt_id": first.attempt_id, "event_id": first.event_id}
        evidence.add(first.event_id)

    if currency:
        for c in captures.values():
            if c["currency"] != currency:
                anomalies.append(Anomaly(INTEGRITY, "CURRENCY_MISMATCH",
                                         f"capture {c['capture_id']} currency differs from the order currency",
                                         (c["event_id"],)))
    captured_total = sum(c["amount"] for c in captures.values())
    if expected is not None and not conflicted_caps and captured_total > expected:
        anomalies.append(Anomaly(INTEGRITY, "OVER_CAPTURE",
                                 f"captured {captured_total} exceeds the expected amount {expected}",
                                 tuple(sorted(c["event_id"] for c in captures.values()))))

    # ---- rule 07: refunds identified by refund_id, must reference an existing capture
    ref_groups: dict[str, list[Event]] = defaultdict(list)
    for e in pay_events:
        if e.type == "REFUND_ISSUED":
            ref_groups[e.refund_id].append(e)
    refunds: dict[str, dict] = {}
    for rid in sorted(ref_groups):
        group = ref_groups[rid]
        sigs = {(e.capture_id, e.amount_cents, e.currency) for e in group}
        if len(sigs) > 1:
            anomalies.append(Anomaly(INTEGRITY, "REFUND_ID_CONFLICT",
                                     f"refund {rid} is reported with conflicting details",
                                     tuple(sorted(e.event_id for e in group))))
            continue
        first = _canonical(group)
        ignored.extend(sorted(e.event_id for e in group if e is not first))
        if first.capture_id in conflicted_caps:
            continue  # already an integrity error on that capture
        if first.capture_id not in captures:
            anomalies.append(Anomaly(INTEGRITY, "INVALID_REFUND_REFERENCE",
                                     f"refund {rid} references unknown capture {first.capture_id}",
                                     (first.event_id,)))
            continue
        if first.currency != captures[first.capture_id]["currency"]:
            anomalies.append(Anomaly(INTEGRITY, "CURRENCY_MISMATCH",
                                     f"refund {rid} currency differs from its capture", (first.event_id,)))
            continue
        refunds[rid] = {"refund_id": rid, "capture_id": first.capture_id,
                        "amount": first.amount_cents, "event_id": first.event_id}
        evidence.add(first.event_id)
    per_capture: dict[str, int] = defaultdict(int)
    for r in refunds.values():
        per_capture[r["capture_id"]] += r["amount"]
    for cid, total in sorted(per_capture.items()):
        if total > captures[cid]["amount"]:
            anomalies.append(Anomaly(INTEGRITY, "REFUND_EXCEEDS_CAPTURE",
                                     f"refunds {total} exceed captured {captures[cid]['amount']} for capture {cid}",
                                     tuple(sorted(r["event_id"] for r in refunds.values() if r["capture_id"] == cid))))
    refunded_total = sum(r["amount"] for r in refunds.values())

    # ---- chargebacks must reference an existing capture
    chargebacks = []
    for e in sorted((e for e in pay_events if e.type == "CHARGEBACK_OPENED"), key=lambda e: e.sort_key()):
        if e.capture_id in conflicted_caps:
            continue
        if e.capture_id not in captures:
            anomalies.append(Anomaly(INTEGRITY, "INVALID_CHARGEBACK_REFERENCE",
                                     f"chargeback references unknown capture {e.capture_id}", (e.event_id,)))
        else:
            chargebacks.append(e.event_id)
            evidence.add(e.event_id)

    # ---- verified failures (payments source only)
    failures = sorted((e.attempt_id, e.event_id) for e in pay_events if e.type == "PAYMENT_FAILED")
    for attempt, eid in failures:
        evidence.add(eid)
    failed_attempts = {a for a, _ in failures}
    clash = sorted(a for a in failed_attempts if any(c["attempt_id"] == a for c in captures.values()))
    for a in clash:  # ASSUMPTION: same attempt both failed and captured is contradictory -> UNKNOWN
        ids = tuple(sorted([eid for at, eid in failures if at == a] +
                           [c["event_id"] for c in captures.values() if c["attempt_id"] == a]))
        anomalies.append(Anomaly(INTEGRITY, "ATTEMPT_CONTRADICTION",
                                 f"attempt {a} is reported both failed and captured", ids))

    # (payment-side) evidence also cites records involved in non-info anomalies
    for a in anomalies:
        if a.severity != INFO:
            evidence.update(a.event_ids)

    # ---- notes: untrusted text. Only scanned for suspicious content, never used as evidence.
    note_ids = []
    flagged = []
    anomalies.extend(note_issues)  # after the evidence update: notes never become evidence
    for n in _dedupe([n for n in notes if n.tenant_id == tenant_id and n.order_id == order_id and n.type == "NOTE"],
                     anomalies, ignored):
        note_ids.append(n.event_id)
        if n.text and any(p.search(n.text) for p in INJECTION_PATTERNS):
            flagged.append(n.event_id)
    if flagged:
        anomalies.append(Anomaly(INFO, "SUSPECTED_PROMPT_INJECTION",
                                 "support note(s) contain instructions aimed at an AI assistant; "
                                 "treated as untrusted text and ignored", tuple(sorted(flagged))))
    facts["flagged_notes"] = sorted(flagged)

    # ---- decision (spec section 3, in the mandated order)
    integrity_failed = any(a.severity == INTEGRITY for a in anomalies)
    if integrity_failed:
        status, cap, ref, net = UNKNOWN, None, None, None
    else:
        cap, ref = captured_total, refunded_total
        net = cap - ref
        if chargebacks:
            status = DISPUTED
        elif not captures and failures:
            status = UNPAID
        elif ref > 0 and ref == cap:
            status = REFUNDED
        elif 0 < ref < cap:
            status = PARTIALLY_REFUNDED
        elif cap == expected:
            status = PAID
        elif 0 < cap < expected:
            status = PARTIALLY_PAID
        else:
            status = UNKNOWN
            anomalies.append(Anomaly(WARNING, "INSUFFICIENT_PAYMENT_EVIDENCE",
                                     "no payments-source evidence establishes the payment state"))
        if status == UNKNOWN:
            cap = ref = net = None

    # ---- confidence (documented in README)
    severities = {a.severity for a in anomalies}
    if status == UNKNOWN or INTEGRITY in severities:
        confidence = "LOW"
    elif WARNING in severities or status in (UNPAID, PARTIALLY_PAID):
        confidence = "MEDIUM"
    else:
        confidence = "HIGH"

    facts.update(captures=sorted(captures.values(), key=lambda c: c["capture_id"]),
                 refunds=sorted(refunds.values(), key=lambda r: r["refund_id"]),
                 chargebacks=sorted(chargebacks), failures=failures,
                 ignored_duplicates=sorted(ignored), note_ids=sorted(note_ids))
    return Reconciliation(status, cap, ref, net, sorted(anomalies), sorted(evidence), confidence, facts)

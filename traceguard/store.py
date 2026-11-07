"""In-memory event store: loads JSONL safely and indexes by (tenant, order).

Bad lines never crash the load. They become either:
  * a global bad line (cannot tell which order it belongs to), or
  * a per-order rejection that the reconciler later sees as an anomaly.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .models import (EVENT_TYPES, FINANCIAL_TYPES, INTEGRITY, WARNING, Anomaly, Event,
                     ValidationError, parse_event)

GROUPS = {"order": {"ORDER_CREATED"}, "notes": {"NOTE"}, "payments": set(FINANCIAL_TYPES)}


@dataclass(frozen=True)
class Rejection:
    line_no: int
    code: str
    message: str
    source: str | None
    type: str | None
    event_id: str | None


def _group_of_type(etype: str | None) -> str:
    if etype == "ORDER_CREATED":
        return "order"
    if etype == "NOTE":
        return "notes"
    return "payments"  # financial, unsupported, or missing type: treat as payment-relevant (safe)


class EventStore:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], list[Event]] = defaultdict(list)
        self.rejections: dict[tuple[str, str], list[Rejection]] = defaultdict(list)
        self.bad_lines: list[tuple[int, str]] = []
        self._issues: dict[tuple[str, str], list[tuple[str, Anomaly]]] = defaultdict(list)

    # ---- loading -------------------------------------------------------
    @classmethod
    def from_file(cls, path: str) -> "EventStore":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return cls.from_lines(fh)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "EventStore":
        store = cls()
        for line_no, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                store.bad_lines.append((line_no, f"invalid JSON: {exc.msg}"))
                continue
            try:
                event = parse_event(raw)
            except ValidationError as exc:
                store._reject(line_no, raw, exc)
                continue
            store.events[(event.tenant_id, event.order_id)].append(event)
        store._tenant_wide_checks()
        return store

    def _reject(self, line_no: int, raw, exc: ValidationError) -> None:
        if not isinstance(raw, dict):
            self.bad_lines.append((line_no, exc.message))
            return
        tenant, order = raw.get("tenant_id"), raw.get("order_id")
        if not (isinstance(tenant, str) and tenant and isinstance(order, str) and order):
            self.bad_lines.append((line_no, f"{exc.message} (cannot attribute to an order)"))
            return
        src, etype, eid = raw.get("source"), raw.get("type"), raw.get("event_id")
        self.rejections[(tenant, order)].append(Rejection(
            line_no, exc.code, exc.message,
            src if isinstance(src, str) else None,
            etype if isinstance(etype, str) else None,
            eid if isinstance(eid, str) else None))

    def _tenant_wide_checks(self) -> None:
        """Checks that need to look across orders (but never across tenants)."""
        event_orders: dict[tuple, set] = defaultdict(set)
        txn_orders: dict[tuple, set] = defaultdict(set)
        for (tenant, order), evs in self.events.items():
            for e in evs:
                event_orders[(tenant, e.event_id)].add(order)
                if e.source == "payments" and e.type == "PAYMENT_CAPTURED":
                    txn_orders[(tenant, "capture", e.capture_id)].add(order)
                if e.source == "payments" and e.type == "REFUND_ISSUED":
                    txn_orders[(tenant, "refund", e.refund_id)].add(order)
        for (tenant, eid), orders in event_orders.items():
            if len(orders) > 1:
                for o in orders:
                    self._issues[(tenant, o)].append(("payments", Anomaly(
                        INTEGRITY, "EVENT_ID_REUSED_ACROSS_ORDERS",
                        f"event id {eid} appears under several orders of the same tenant", (eid,))))
        for (tenant, kind, tid), orders in txn_orders.items():
            if len(orders) > 1:
                for o in orders:
                    self._issues[(tenant, o)].append(("payments", Anomaly(
                        INTEGRITY, "TXN_ID_REUSED_ACROSS_ORDERS",
                        f"{kind} id {tid} is used by several orders of the same tenant", ())))

    # ---- queries (always scoped by tenant AND order: rule 09) -----------
    def order_events(self, tenant: str, order: str, group: str) -> list[Event]:
        wanted = GROUPS[group]
        return [e for e in self.events.get((tenant, order), []) if e.type in wanted]

    def issues(self, tenant: str, order: str, group: str) -> list[Anomaly]:
        out = [a for g, a in self._issues.get((tenant, order), []) if g == group]
        for r in self.rejections.get((tenant, order), []):
            if _group_of_type(r.type) != group:
                continue
            # A rejected record that could have been payment evidence makes the history
            # untrustworthy -> integrity. Rejections from other sources are just noise.
            severity = INTEGRITY if (group == "payments" and r.source in (None, "payments")) else WARNING
            ids = (r.event_id,) if r.event_id else ()
            out.append(Anomaly(severity, f"REJECTED_RECORD_{r.code}",
                               f"line {r.line_no} rejected: {r.message}", ids))
        return out

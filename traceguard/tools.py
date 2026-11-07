"""The three tool interfaces required by the assessment + a fault injector for demos/tests."""
from __future__ import annotations

from collections import defaultdict, deque

from .store import EventStore


class ToolError(Exception):
    """A tool call failed (retryable)."""


class ToolTimeout(ToolError):
    """A tool call timed out (retryable)."""


class FaultPlan:
    """Scripted failures per tool, consumed one per call.

    FaultPlan({"get_payment_events": ["timeout", "error"]}) -> first call times out,
    second raises an error, third call works normally.
    Kinds: "timeout", "error", "garbage" (returns a malformed response).
    """

    def __init__(self, plan: dict[str, list[str]] | None = None):
        self._queues = defaultdict(deque, {k: deque(v) for k, v in (plan or {}).items()})

    def next(self, tool: str) -> str | None:
        q = self._queues[tool]
        return q.popleft() if q else None

    @staticmethod
    def parse(spec: str) -> "FaultPlan":
        """CLI format: 'get_payment_events:timeout:2' (tool:kind:times). Comma-separate several."""
        plan: dict[str, list[str]] = defaultdict(list)
        for part in filter(None, (p.strip() for p in spec.split(","))):
            tool, kind, *rest = part.split(":")
            plan[tool].extend([kind] * (int(rest[0]) if rest else 1))
        return FaultPlan(dict(plan))


class TraceGuardTools:
    def __init__(self, store: EventStore, faults: FaultPlan | None = None):
        self._store = store
        self._faults = faults or FaultPlan()
        self.calls: list[str] = []  # call log, useful for demos and tests

    def _respond(self, tool: str, tenant_id: str, order_id: str, group: str) -> dict:
        self.calls.append(tool)
        fault = self._faults.next(tool)
        if fault == "timeout":
            raise ToolTimeout(f"{tool} timed out")
        if fault == "error":
            raise ToolError(f"{tool} failed (simulated upstream error)")
        if fault == "garbage":
            return {"unexpected": "shape"}
        return {
            "events": [e.to_raw() for e in self._store.order_events(tenant_id, order_id, group)],
            "issues": [a.to_dict() for a in self._store.issues(tenant_id, order_id, group)],
        }

    def get_order(self, tenant_id: str, order_id: str) -> dict:
        return self._respond("get_order", tenant_id, order_id, "order")

    def get_payment_events(self, tenant_id: str, order_id: str) -> dict:
        return self._respond("get_payment_events", tenant_id, order_id, "payments")

    def get_support_notes(self, tenant_id: str, order_id: str) -> dict:
        return self._respond("get_support_notes", tenant_id, order_id, "notes")

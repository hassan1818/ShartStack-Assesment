"""Investigation workflow: interpret -> call tools (bounded retries) -> validate -> reconcile -> explain."""
from __future__ import annotations

from .llm import Explainer, IntentInterpreter, KeywordInterpreter, TemplateExplainer, GENERAL, safe_explain
from .models import INTEGRITY, WARNING, Anomaly, Event, ValidationError, parse_event
from .reconcile import Reconciliation, reconcile
from .resilience import ToolUnavailable, call_with_retry
from .tools import ToolError, TraceGuardTools


class InvalidToolResponse(ToolError):
    """Malformed tool output is treated like a failed call: retried a bounded number of times."""


class InvestigationAgent:
    def __init__(self, tools: TraceGuardTools, interpreter: IntentInterpreter | None = None,
                 explainer: Explainer | None = None, max_attempts: int = 3, base_delay: float = 0.0):
        self.tools = tools
        self.interpreter = interpreter or KeywordInterpreter()
        self.explainer = explainer or TemplateExplainer()
        self.max_attempts, self.base_delay = max_attempts, base_delay
        self.trace: list[str] = []

    # ---- response validation ------------------------------------------
    def _fetch(self, tool: str, tenant: str, order: str, allowed_types: set[str]):
        resp = getattr(self.tools, tool)(tenant, order)
        if not (isinstance(resp, dict) and isinstance(resp.get("events"), list)
                and isinstance(resp.get("issues"), list)):
            raise InvalidToolResponse(f"{tool} returned a malformed response")
        events, issues = [], []
        try:
            for raw in resp["events"]:
                events.append(parse_event(raw))
            for raw in resp["issues"]:
                issues.append(Anomaly.from_dict(raw))
        except (ValidationError, KeyError, TypeError) as exc:
            raise InvalidToolResponse(f"{tool} returned an invalid record: {exc}") from exc
        good = [e for e in events
                if e.tenant_id == tenant and e.order_id == order and e.type in allowed_types]
        if len(good) != len(events):  # rule 09 + schema: drop and flag, never use
            bad = tuple(sorted(e.event_id for e in events if e not in good))
            issues.append(Anomaly(INTEGRITY, "TOOL_RESPONSE_MISMATCH",
                                  f"{tool} returned records outside the requested tenant/order/type", bad))
        return good, issues

    def _call(self, tool: str, tenant: str, order: str, allowed: set[str]):
        return call_with_retry(tool, lambda: self._fetch(tool, tenant, order, allowed),
                               max_attempts=self.max_attempts, base_delay=self.base_delay, trace=self.trace)

    # ---- main entry ---------------------------------------------------
    def investigate(self, tenant_id: str, order_id: str, question: str = "") -> dict:
        try:
            intent = self.interpreter.interpret(question)
        except Exception:  # noqa: BLE001
            intent = GENERAL

        try:
            order_events, order_issues = self._call("get_order", tenant_id, order_id, {"ORDER_CREATED"})
            pay_events, pay_issues = self._call(
                "get_payment_events", tenant_id, order_id,
                {"PAYMENT_FAILED", "PAYMENT_CAPTURED", "REFUND_ISSUED", "CHARGEBACK_OPENED"})
        except ToolUnavailable as exc:  # essential evidence unavailable -> controlled UNKNOWN
            return self._unknown_on_failure(str(exc))

        try:  # notes are optional context: failing here must not block the financial answer
            notes, note_issues = self._call("get_support_notes", tenant_id, order_id, {"NOTE"})
        except ToolUnavailable as exc:
            notes, note_issues = [], [Anomaly(WARNING, "NOTES_UNAVAILABLE", str(exc))]

        result = reconcile(tenant_id, order_id, order_events + pay_events, notes,
                           order_issues + pay_issues, note_issues)
        known = set(result.evidence_event_ids) | {i for a in result.anomalies for i in a.event_ids} \
            | set(result.facts.get("note_ids", []))
        return result.to_output(safe_explain(self.explainer, result, intent, known))

    @staticmethod
    def _unknown_on_failure(message: str) -> dict:
        anomaly = Anomaly(INTEGRITY, "TOOL_UNAVAILABLE", message)
        r = Reconciliation("UNKNOWN", None, None, None, [anomaly], [], "LOW")
        return r.to_output("The status is UNKNOWN because a required data source was unavailable after "
                           "bounded retries, so no financial conclusion can be drawn.")

import json
from traceguard.agent import InvestigationAgent
from traceguard.store import EventStore
from traceguard.tools import FaultPlan, TraceGuardTools


def ev(eid, etype, source="payments", order="O-1", tenant="T1", minute=1, **kw):
    d = {"tenant_id": tenant, "order_id": order, "event_id": eid, "source": source, "type": etype,
         "occurred_at": f"2026-03-01T10:{minute:02d}:00Z", "ingested_at": f"2026-03-01T11:{minute:02d}:00Z"}
    d.update(kw)
    return {k: v for k, v in d.items() if v is not None}


def order_ev(eid="order-1", amount=12900, cur="USD", **kw):
    return ev(eid, "ORDER_CREATED", source="checkout", minute=0, amount_cents=amount, currency=cur, **kw)


def cap_ev(eid="pay-2", cid="C1", amount=12900, attempt="A2", cur="USD", **kw):
    return ev(eid, "PAYMENT_CAPTURED", minute=2, attempt_id=attempt, capture_id=cid,
              amount_cents=amount, currency=cur, **kw)


def fail_ev(eid="pay-1", attempt="A1", **kw):
    return ev(eid, "PAYMENT_FAILED", minute=1, attempt_id=attempt, **kw)


def refund_ev(eid="ref-1", rid="R1", cid="C1", amount=1000, cur="USD", **kw):
    return ev(eid, "REFUND_ISSUED", minute=3, capture_id=cid, refund_id=rid, amount_cents=amount, currency=cur, **kw)


def chargeback_ev(eid="cb-1", cid="C1", **kw):
    return ev(eid, "CHARGEBACK_OPENED", minute=4, capture_id=cid, **kw)


def note_ev(eid="note-1", text="hello", **kw):
    return ev(eid, "NOTE", source="support", minute=5, text=text, **kw)


def to_lines(records):
    return [r if isinstance(r, str) else json.dumps(r) for r in records]


def investigate(records, tenant="T1", order="O-1", faults=None, question="Was this order paid?", **agent_kw):
    store = EventStore.from_lines(to_lines(records))
    tools = TraceGuardTools(store, FaultPlan(faults))
    agent = InvestigationAgent(tools, **agent_kw)
    return agent.investigate(tenant, order, question), agent, tools


def decision(out):
    """The parts of the output that constitute the financial decision (no wording)."""
    return {k: out[k] for k in ("status", "captured_cents", "refunded_cents", "net_paid_cents",
                                "evidence_event_ids", "confidence")}


def codes(out):
    return {a["code"] for a in out["anomalies"]}

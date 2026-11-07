"""Data models + strict validation of raw JSON records.

Rule of thumb: nothing becomes an `Event` unless it passed validation.
Anything else becomes a *rejection* that the reconciler can see.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

EVENT_TYPES = {
    "ORDER_CREATED", "PAYMENT_FAILED", "PAYMENT_CAPTURED",
    "REFUND_ISSUED", "CHARGEBACK_OPENED", "NOTE",
}
FINANCIAL_TYPES = {"PAYMENT_FAILED", "PAYMENT_CAPTURED", "REFUND_ISSUED", "CHARGEBACK_OPENED"}
AMOUNT_REQUIRED = {"ORDER_CREATED", "PAYMENT_CAPTURED", "REFUND_ISSUED"}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Severity levels of an anomaly.
INTEGRITY = "integrity"  # forces status UNKNOWN
WARNING = "warning"      # status stands, confidence drops to MEDIUM
INFO = "info"            # reported only (e.g. suspected prompt injection)


class ValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, order=True)
class Anomaly:
    severity: str
    code: str
    message: str
    event_ids: tuple = ()

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "message": self.message, "event_ids": list(self.event_ids)}

    @staticmethod
    def from_dict(d: dict) -> "Anomaly":
        return Anomaly(d["severity"], d["code"], d["message"], tuple(d.get("event_ids", ())))


@dataclass(frozen=True)
class Event:
    tenant_id: str
    order_id: str
    event_id: str
    source: str
    type: str
    occurred_at: datetime
    ingested_at: datetime | None = None
    attempt_id: str | None = None
    capture_id: str | None = None
    refund_id: str | None = None
    amount_cents: int | None = None
    currency: str | None = None
    text: str | None = None

    def substance(self) -> tuple:
        """Everything that defines the event EXCEPT ingested_at (rule 04)."""
        return (self.tenant_id, self.order_id, self.event_id, self.source, self.type,
                self.occurred_at, self.attempt_id, self.capture_id, self.refund_id,
                self.amount_cents, self.currency, self.text)

    def sort_key(self) -> tuple:
        return (self.occurred_at, self.event_id)

    def to_raw(self) -> dict:
        d = {
            "tenant_id": self.tenant_id, "order_id": self.order_id, "event_id": self.event_id,
            "source": self.source, "type": self.type,
            "occurred_at": self.occurred_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
            "attempt_id": self.attempt_id, "capture_id": self.capture_id,
            "refund_id": self.refund_id, "amount_cents": self.amount_cents,
            "currency": self.currency, "text": self.text,
        }
        return {k: v for k, v in d.items() if v is not None}


def _req_str(raw: dict, key: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValidationError("MISSING_FIELD", f"'{key}' is missing or not a non-empty string")
    return v


def _opt_str(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ValidationError("INVALID_FIELD", f"'{key}' must be a non-empty string when present")
    return v


def _timestamp(raw: dict, key: str, required: bool) -> datetime | None:
    v = raw.get(key)
    if v is None:
        if required:
            raise ValidationError("MISSING_FIELD", f"'{key}' is missing")
        return None
    if not isinstance(v, str):
        raise ValidationError("INVALID_TIMESTAMP", f"'{key}' must be an ISO 8601 string")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValidationError("INVALID_TIMESTAMP", f"'{key}' is not valid ISO 8601: {v!r}") from None
    if dt.tzinfo is None:  # ASSUMPTION: a timestamp without timezone is ambiguous -> invalid
        raise ValidationError("INVALID_TIMESTAMP", f"'{key}' has no timezone offset: {v!r}")
    return dt.astimezone(timezone.utc)


def _amount(raw: dict, required: bool) -> int | None:
    v = raw.get("amount_cents")
    if v is None:
        if required:
            raise ValidationError("INVALID_AMOUNT", "'amount_cents' is required for this event type")
        return None
    # bool is a subclass of int in Python; floats like 12900.0 are rejected on purpose.
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ValidationError("INVALID_AMOUNT", f"'amount_cents' must be a positive integer, got {v!r}")
    return v


def parse_event(raw: Any) -> Event:
    """Validate one raw record. Raises ValidationError, never anything else."""
    if not isinstance(raw, dict):
        raise ValidationError("NOT_AN_OBJECT", "record is not a JSON object")
    etype = _req_str(raw, "type")
    if etype not in EVENT_TYPES:
        raise ValidationError("UNSUPPORTED_TYPE", f"unsupported event type {etype!r}")
    tenant, order = _req_str(raw, "tenant_id"), _req_str(raw, "order_id")
    event_id, source = _req_str(raw, "event_id"), _req_str(raw, "source")
    occurred = _timestamp(raw, "occurred_at", required=True)
    ingested = _timestamp(raw, "ingested_at", required=False)
    amount = _amount(raw, required=etype in AMOUNT_REQUIRED)
    currency = _opt_str(raw, "currency")
    if etype in AMOUNT_REQUIRED and currency is None:
        raise ValidationError("INVALID_CURRENCY", "'currency' is required for this event type")
    if currency is not None and not CURRENCY_RE.match(currency):
        raise ValidationError("INVALID_CURRENCY", f"'currency' must be a 3-letter uppercase code, got {currency!r}")
    attempt, capture, refund = _opt_str(raw, "attempt_id"), _opt_str(raw, "capture_id"), _opt_str(raw, "refund_id")
    text = raw.get("text")
    if text is not None and not isinstance(text, str):
        raise ValidationError("INVALID_FIELD", "'text' must be a string when present")
    needs = {"PAYMENT_FAILED": [("attempt_id", attempt)],
             "PAYMENT_CAPTURED": [("capture_id", capture)],
             "REFUND_ISSUED": [("capture_id", capture), ("refund_id", refund)],
             "CHARGEBACK_OPENED": [("capture_id", capture)]}.get(etype, [])
    for name, value in needs:
        if value is None:
            raise ValidationError("MISSING_FIELD", f"'{name}' is required for {etype}")
    return Event(tenant, order, event_id, source, etype, occurred, ingested,
                 attempt, capture, refund, amount, currency, text)

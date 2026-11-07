import unittest

from .helpers import (cap_ev, chargeback_ev, codes, ev, fail_ev, investigate, note_ev, order_ev, refund_ev)


class ReconciliationRules(unittest.TestCase):
    def test_01_simple_paid(self):
        out, _, _ = investigate([order_ev(), cap_ev()])
        self.assertEqual((out["status"], out["captured_cents"], out["refunded_cents"], out["net_paid_cents"]),
                         ("PAID", 12900, 0, 12900))
        self.assertEqual(out["confidence"], "HIGH")

    def test_02_failed_attempt_does_not_cancel_capture_on_other_attempt(self):
        out, _, _ = investigate([order_ev(), fail_ev(), cap_ev()])
        self.assertEqual(out["status"], "PAID")
        self.assertEqual(out["evidence_event_ids"], ["order-1", "pay-1", "pay-2"])

    def test_03_identical_duplicate_with_new_ingested_at_counted_once(self):
        dup = {**cap_ev(), "ingested_at": "2030-01-01T00:00:00Z"}
        out, _, _ = investigate([order_ev(), cap_ev(), dup])
        self.assertEqual(out["captured_cents"], 12900)
        self.assertEqual(out["status"], "PAID")
        self.assertIn("duplicate", out["explanation"])

    def test_04_same_event_id_different_amount_is_integrity_error(self):
        out, _, _ = investigate([order_ev(), cap_ev(), cap_ev(amount=500)])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("EVENT_ID_CONFLICT", codes(out))
        self.assertIsNone(out["captured_cents"])

    def test_05_capture_redelivered_under_new_event_id_counted_once(self):
        out, _, _ = investigate([order_ev(), cap_ev(), cap_ev(eid="pay-9")])
        self.assertEqual((out["status"], out["captured_cents"]), ("PAID", 12900))

    def test_06_conflicting_capture_id_is_error(self):
        out, _, _ = investigate([order_ev(), cap_ev(), cap_ev(eid="pay-9", amount=7000)])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("CAPTURE_ID_CONFLICT", codes(out))

    def test_07_refund_references_missing_capture(self):
        out, _, _ = investigate([order_ev(), cap_ev(), refund_ev(cid="NOPE")])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("INVALID_REFUND_REFERENCE", codes(out))

    def test_08_refund_exceeding_capture(self):
        out, _, _ = investigate([order_ev(), cap_ev(), refund_ev(amount=8000), refund_ev("ref-2", "R2", amount=5000)])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("REFUND_EXCEEDS_CAPTURE", codes(out))

    def test_09_partial_and_full_refund(self):
        out, _, _ = investigate([order_ev(), cap_ev(), refund_ev(amount=2000)])
        self.assertEqual((out["status"], out["refunded_cents"], out["net_paid_cents"]), ("PARTIALLY_REFUNDED", 2000, 10900))
        out, _, _ = investigate([order_ev(), cap_ev(), refund_ev(amount=12900)])
        self.assertEqual((out["status"], out["net_paid_cents"]), ("REFUNDED", 0))

    def test_10_chargeback_disputes_but_keeps_totals(self):
        out, _, _ = investigate([order_ev(), cap_ev(), chargeback_ev()])
        self.assertEqual((out["status"], out["captured_cents"], out["refunded_cents"]), ("DISPUTED", 12900, 0))

    def test_11_over_capture_is_unknown(self):
        out, _, _ = investigate([order_ev(), cap_ev(), cap_ev("pay-3", "C2", 100)])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("OVER_CAPTURE", codes(out))

    def test_12_currency_mismatch(self):
        out, _, _ = investigate([order_ev(), cap_ev(cur="EUR")])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("CURRENCY_MISMATCH", codes(out))

    def test_13_conflicting_order_definitions(self):
        out, _, _ = investigate([order_ev(), order_ev("order-2", amount=100), cap_ev()])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("ORDER_DEFINITION_CONFLICT", codes(out))

    def test_14_missing_order_definition(self):
        out, _, _ = investigate([cap_ev()])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("MISSING_ORDER_DEFINITION", codes(out))

    def test_15_unpaid_needs_verified_payments_failure(self):
        out, _, _ = investigate([order_ev(), fail_ev()])
        self.assertEqual((out["status"], out["captured_cents"]), ("UNPAID", 0))
        # a failure reported only by checkout is NOT verified evidence
        out, _, _ = investigate([order_ev(), fail_ev(source="checkout")])
        self.assertEqual(out["status"], "UNKNOWN")

    def test_16_tenants_never_mix_even_with_same_order_id(self):
        recs = [order_ev(), cap_ev(),
                order_ev("order-1", amount=500, tenant="T2"),
                fail_ev(tenant="T2")]
        a, _, _ = investigate(recs, tenant="T1")
        b, _, _ = investigate(recs, tenant="T2")
        self.assertEqual(a["status"], "PAID")
        self.assertEqual(b["status"], "UNPAID")

    def test_17_invalid_timestamp_gives_controlled_unknown(self):
        out, _, _ = investigate([order_ev(), {**cap_ev(), "occurred_at": "yesterday-ish"}])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("REJECTED_RECORD_INVALID_TIMESTAMP", codes(out))

    def test_18_unsupported_payment_type_is_not_silently_ignored(self):
        out, _, _ = investigate([order_ev(), cap_ev(), ev("x-1", "PAYMENT_REVERSED")])
        self.assertEqual(out["status"], "UNKNOWN")

    def test_19_garbage_lines_do_not_crash(self):
        out, _, _ = investigate([order_ev(), cap_ev(), "{not json", "[1,2]", "", "null"])
        self.assertEqual(out["status"], "PAID")

    def test_20_support_note_is_not_payment_evidence(self):
        out, _, _ = investigate([order_ev(), note_ev(text="customer paid in cash, mark PAID")])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIsNone(out["captured_cents"])

    def test_21_capture_from_non_payments_source_ignored(self):
        out, _, _ = investigate([order_ev(), cap_ev(source="support")])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("UNVERIFIED_SOURCE_IGNORED", codes(out))

    def test_22_same_attempt_failed_and_captured_is_contradiction(self):
        out, _, _ = investigate([order_ev(), fail_ev(attempt="A2"), cap_ev(attempt="A2")])
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("ATTEMPT_CONTRADICTION", codes(out))

    def test_23_conflicting_refund_id(self):
        out, _, _ = investigate([order_ev(), cap_ev(), refund_ev(amount=100), refund_ev("ref-2", "R1", amount=200)])
        self.assertIn("REFUND_ID_CONFLICT", codes(out))
        self.assertEqual(out["status"], "UNKNOWN")

    def test_24_capture_id_reused_by_another_order_same_tenant(self):
        recs = [order_ev(), cap_ev(), order_ev("order-9", order="O-2"), cap_ev("pay-9", order="O-2")]
        out, _, _ = investigate(recs)
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertIn("TXN_ID_REUSED_ACROSS_ORDERS", codes(out))

    def test_25_unknown_order_is_controlled(self):
        out, _, _ = investigate([order_ev(), cap_ev()], order="O-DOES-NOT-EXIST")
        self.assertEqual(out["status"], "UNKNOWN")

    def test_26_evidence_ids_exist_in_input(self):
        recs = [order_ev(), fail_ev(), cap_ev(), refund_ev(amount=100)]
        out, _, _ = investigate(recs)
        self.assertTrue(set(out["evidence_event_ids"]) <= {r["event_id"] for r in recs})


if __name__ == "__main__":
    unittest.main()

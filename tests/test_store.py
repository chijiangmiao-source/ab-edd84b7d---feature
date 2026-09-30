import unittest

from app.solver import InputError
from app.store import AuditStore, ConflictError, PlanConflictError
from app.planner import SourceNotAmbiguousError


AMBIGUOUS_PAYLOAD = {
    "request_id": "req-ambig",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
        {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
    ],
}


def plan_payload(plan_id="pln-1", target=("A", "C"), qid="q1",
                 pair=("A", "C")):
    return {
        "plan_id": plan_id,
        "target": list(target),
        "queries": [{"id": qid, "left": pair[0], "right": pair[1]}],
    }


def payload(request_id="req-1", hi=8):
    return {
        "request_id": request_id,
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": hi},
        ],
    }


class AuditStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()

    def test_create_assigns_sequential_ids(self):
        record, created = self.store.create(payload())
        self.assertTrue(created)
        self.assertEqual(record["audit_id"], "AUD-000001")
        record2, created2 = self.store.create(payload(request_id="req-2"))
        self.assertTrue(created2)
        self.assertEqual(record2["audit_id"], "AUD-000002")

    def test_replay_returns_original_audit(self):
        first, _ = self.store.create(payload())
        second, created = self.store.create(payload())
        self.assertFalse(created)
        self.assertEqual(second["audit_id"], first["audit_id"])
        self.assertEqual(self.store.count(), 1)

    def test_changed_payload_is_rejected_without_new_record(self):
        first, _ = self.store.create(payload())
        with self.assertRaises(ConflictError) as ctx:
            self.store.create(payload(hi=9))
        self.assertEqual(ctx.exception.audit_id, first["audit_id"])
        self.assertEqual(self.store.count(), 1)

    def test_changed_event_is_rejected(self):
        self.store.create(payload())
        changed = payload()
        changed["events"] = [{"id": "B", "counter": 4}]
        with self.assertRaises(ConflictError):
            self.store.create(changed)
        self.assertEqual(self.store.count(), 1)

    def test_reordered_payload_still_replays(self):
        first, _ = self.store.create({
            "request_id": "req-9",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
            "constraints": [
                {"id": "c2", "source": "B", "target": "C", "lo": 47, "hi": 47},
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        })
        reordered = {
            "request_id": "req-9",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "C", "counter": 50}, {"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
                {"id": "c2", "source": "B", "target": "C", "lo": 47, "hi": 47},
            ],
        }
        second, created = self.store.create(reordered)
        self.assertFalse(created)
        self.assertEqual(second["audit_id"], first["audit_id"])

    def test_invalid_payload_claims_no_request_id(self):
        broken = payload()
        broken["events"] = []
        with self.assertRaises(InputError):
            self.store.create(broken)
        record, created = self.store.create(payload())
        self.assertTrue(created)
        self.assertEqual(record["audit_id"], "AUD-000001")

    def test_frozen_record_is_readable(self):
        record, _ = self.store.create(payload())
        fetched = self.store.get(record["audit_id"])
        self.assertEqual(fetched["input"], payload())
        self.assertEqual(fetched["status"], "unique")
        self.assertIn("conclusion", fetched)
        self.assertIn("evidence", fetched)
        self.assertIsNone(self.store.get("AUD-999999"))


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        self.audit, _ = self.store.create(AMBIGUOUS_PAYLOAD)
        self.audit_id = self.audit["audit_id"]

    def test_create_resolved_plan(self):
        plan, created = self.store.create_plan(
            self.audit_id, plan_payload())
        self.assertTrue(created)
        self.assertEqual(plan["plan_ref"], "PLN-000001")
        self.assertEqual(plan["status"], "resolved")
        self.assertEqual(plan["worst_case_questions"], 1)
        self.assertEqual(plan["audit_id"], self.audit_id)

    def test_unknown_audit_is_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.create_plan("AUD-999999", plan_payload())
        self.assertEqual(self.store.plan_count(), 0)

    def test_non_ambiguous_source_rejected(self):
        unique, _ = self.store.create(payload("req-unique"))
        with self.assertRaises(SourceNotAmbiguousError):
            self.store.create_plan(unique["audit_id"], plan_payload())
        self.assertEqual(self.store.plan_count(), 0)

    def test_invalid_plan_writes_nothing(self):
        bad = plan_payload()
        bad["target"] = ["A", "ZZ"]
        with self.assertRaises(InputError):
            self.store.create_plan(self.audit_id, bad)
        self.assertEqual(self.store.plan_count(), 0)

    def test_replay_returns_same_plan(self):
        first, created = self.store.create_plan(self.audit_id, plan_payload())
        self.assertTrue(created)
        second, replayed = self.store.create_plan(
            self.audit_id, plan_payload())
        self.assertFalse(replayed)
        self.assertEqual(second["plan_ref"], first["plan_ref"])
        self.assertEqual(self.store.plan_count(), 1)

    def test_same_plan_id_changed_content_is_conflict_and_writes_nothing(self):
        first, _ = self.store.create_plan(self.audit_id, plan_payload())
        changed = plan_payload(target=("B", "C"))
        with self.assertRaises(PlanConflictError) as ctx:
            self.store.create_plan(self.audit_id, changed)
        self.assertEqual(ctx.exception.plan_ref, first["plan_ref"])
        self.assertEqual(self.store.plan_count(), 1)

    def test_plan_id_rebound_under_other_audit_is_conflict(self):
        other_input = dict(AMBIGUOUS_PAYLOAD, request_id="req-ambig-2")
        other, _ = self.store.create(other_input)
        first, _ = self.store.create_plan(self.audit_id, plan_payload())
        with self.assertRaises(PlanConflictError) as ctx:
            self.store.create_plan(other["audit_id"], plan_payload())
        self.assertEqual(ctx.exception.plan_ref, first["plan_ref"])

    def test_read_plan_with_frozen_source(self):
        plan, _ = self.store.create_plan(self.audit_id, plan_payload())
        bundle = self.store.plan_with_source(plan["plan_ref"])
        self.assertEqual(bundle["plan"]["plan_ref"], plan["plan_ref"])
        self.assertEqual(bundle["source_audit"]["input"], AMBIGUOUS_PAYLOAD)
        self.assertEqual(bundle["source_audit"]["status"], "ambiguous")
        self.assertIsNone(self.store.plan_with_source("PLN-999999"))

    def test_plan_refs_scoped_to_audit(self):
        self.store.create_plan(self.audit_id, plan_payload("pln-a"))
        other, _ = self.store.create(
            dict(AMBIGUOUS_PAYLOAD, request_id="req-ambig-2"))
        self.store.create_plan(other["audit_id"], plan_payload("pln-b"))
        self.assertEqual(
            self.store.plan_refs_for(self.audit_id), ["PLN-000001"])
        self.assertEqual(
            self.store.plan_refs_for(other["audit_id"]), ["PLN-000002"])



if __name__ == "__main__":
    unittest.main()

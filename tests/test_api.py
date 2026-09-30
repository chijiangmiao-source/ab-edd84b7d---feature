import json
import threading
import unittest
import urllib.error
import urllib.request

from app.server import make_server
from app.store import AuditStore


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = AuditStore()
        cls.server = make_server("127.0.0.1", 0, cls.store)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def unique_payload(self, request_id="api-unique"):
        return {
            "request_id": request_id,
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
            ],
        }

    def ambiguous_payload(self, request_id="api-ambig"):
        return {
            "request_id": request_id,
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
            "constraints": [
                {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
                {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
            ],
        }

    def plan_body(self, plan_id="api-pln", target=("A", "C"), qid="q1",
                  pair=("A", "C")):
        return {
            "plan_id": plan_id,
            "target": list(target),
            "queries": [{"id": qid, "left": pair[0], "right": pair[1]}],
        }

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_create_and_read_frozen_record(self):
        sent = self.unique_payload("api-read")
        status, created = self.request("POST", "/audits", sent)
        self.assertEqual(status, 201)
        self.assertFalse(created["replayed"])
        self.assertEqual(created["status"], "unique")
        timeline = created["conclusion"]["timeline"]
        b = next(e for e in timeline if e["id"] == "B")
        self.assertEqual(b["absolute"], 103)

        status, fetched = self.request("GET", f"/audits/{created['audit_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["input"], sent)
        self.assertEqual(fetched["conclusion"], created["conclusion"])
        self.assertEqual(fetched["evidence"], created["evidence"])

    def test_idempotent_replay_and_conflict(self):
        sent = self.unique_payload("api-idem")
        _, created = self.request("POST", "/audits", sent)
        count = self.store.count()

        status, replay = self.request("POST", "/audits", sent)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["audit_id"], created["audit_id"])

        changed = self.unique_payload("api-idem")
        changed["constraints"][0]["hi"] = 9
        status, conflict = self.request("POST", "/audits", changed)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["existing_audit_id"], created["audit_id"])
        self.assertEqual(self.store.count(), count)

    def test_unknown_audit_is_404(self):
        status, body = self.request("GET", "/audits/AUD-999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_invalid_payload_is_400(self):
        broken = self.unique_payload("api-bad")
        broken["events"].append({"id": "ZZ", "counter": 1})
        status, body = self.request("POST", "/audits", broken)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_input")
        self.assertTrue(body["problems"])

    def test_malformed_json_is_400(self):
        req = urllib.request.Request(
            self.base + "/audits", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail(f"expected 400, got {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    # -- plans -------------------------------------------------------------

    def test_create_plan_and_read_with_source(self):
        _, audit = self.request("POST", "/audits",
                                self.ambiguous_payload("api-pln-src"))
        status, created = self.request(
            "POST", f"/audits/{audit['audit_id']}/plans",
            self.plan_body("api-pln-create"))
        self.assertEqual(status, 201)
        self.assertFalse(created["replayed"])
        self.assertEqual(created["status"], "resolved")
        self.assertEqual(created["worst_case_questions"], 1)
        tree = created["tree"]
        self.assertEqual(tree["ask"], "q1")
        self.assertEqual(tree["impossible_answers"], ["same"])

        status, bundle = self.request(
            "GET", f"/plans/{created['plan_ref']}")
        self.assertEqual(status, 200)
        self.assertEqual(bundle["plan"]["plan_ref"], created["plan_ref"])
        self.assertEqual(bundle["plan"]["input"],
                         self.plan_body("api-pln-create"))
        self.assertEqual(bundle["source_audit"]["audit_id"],
                         audit["audit_id"])
        self.assertEqual(bundle["source_audit"]["input"],
                         self.ambiguous_payload("api-pln-src"))

    def test_plan_replay_conflict_and_no_half_written_records(self):
        _, audit = self.request("POST", "/audits",
                                self.ambiguous_payload("api-pln-idem"))
        path = f"/audits/{audit['audit_id']}/plans"
        status, first = self.request(
            "POST", path, self.plan_body("api-pln-idem-1"))
        self.assertEqual(status, 201)

        status, replay = self.request(
            "POST", path, self.plan_body("api-pln-idem-1"))
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["plan_ref"], first["plan_ref"])

        changed = self.plan_body("api-pln-idem-1", target=("B", "C"))
        status, conflict = self.request("POST", path, changed)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "plan_id_conflict")
        self.assertEqual(conflict["existing_plan_ref"], first["plan_ref"])

        status, listing = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["plans"], [first["plan_ref"]])

    def test_plan_on_non_ambiguous_audit_is_409(self):
        _, audit = self.request("POST", "/audits",
                                self.unique_payload("api-pln-unique"))
        status, body = self.request(
            "POST", f"/audits/{audit['audit_id']}/plans",
            self.plan_body("api-pln-unique-1"))
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "source_not_ambiguous")

    def test_plan_validation_errors_are_400(self):
        _, audit = self.request("POST", "/audits",
                                self.ambiguous_payload("api-pln-bad"))
        bad = self.plan_body()
        bad["queries"].append(
            {"id": "q1", "left": "B", "right": "C"})  # duplicate id
        status, body = self.request(
            "POST", f"/audits/{audit['audit_id']}/plans", bad)
        self.assertEqual(status, 400)
        self.assertTrue(any("duplicate query id" in p
                            for p in body["problems"]))
        bad2 = self.plan_body()
        bad2["target"] = ["A", "NOPE"]
        status, body = self.request(
            "POST", f"/audits/{audit['audit_id']}/plans", bad2)
        self.assertEqual(status, 400)
        self.assertTrue(any("does not exist" in p for p in body["problems"]))

    def test_unknown_audit_plan_collection_is_404(self):
        status, body = self.request(
            "POST", "/audits/AUD-999999/plans",
            self.plan_body("api-pln-404"))
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/audits/AUD-999999/plans")
        self.assertEqual(status, 404)



if __name__ == "__main__":
    unittest.main()

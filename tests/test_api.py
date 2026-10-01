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


class PlanApiTest(unittest.TestCase):
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

    AMBIG = {
        "request_id": "api-ambig",
        "modulus": 10,
        "anchor": {"id": "A", "absolute": 0},
        "events": [
            {"id": "X", "counter": 5},
            {"id": "Y", "counter": 0},
            {"id": "Z", "counter": 0},
        ],
        "constraints": [
            {"id": "c1", "source": "A", "target": "Z", "lo": 0, "hi": 10},
            {"id": "c2", "source": "Z", "target": "X", "lo": 5, "hi": 5},
            {"id": "c3", "source": "A", "target": "Y", "lo": 0, "hi": 10},
        ],
    }

    def setUp(self):
        payload = dict(self.AMBIG,
                       request_id=f"api-ambig-{self._testMethodName}")
        _, self.audit = self.request("POST", "/audits", payload)

    def test_create_plan_and_reachable_branches(self):
        status, body = self.request(
            "POST", f"/audits/{self.audit['audit_id']}/plans",
            {"plan_id": "p1", "target": ["X", "Y"],
             "pairs": [["A", "Z"], ["A", "Y"]]})
        self.assertEqual(status, 201)
        self.assertFalse(body["replayed"])
        self.assertEqual(body["status"], "decided")
        root = body["tree"]
        self.assertEqual(root["kind"], "query")
        self.assertEqual(root["reachable_answers"], ["before", "same"])
        self.assertEqual(root["pruned_answers"], ["after"])
        self.assertEqual(body["worst_case_queries"], 2)

    def test_plan_read_includes_source_evidence(self):
        _, created = self.request(
            "POST", f"/audits/{self.audit['audit_id']}/plans",
            {"plan_id": "p2", "target": ["X", "Y"],
             "pairs": [["A", "Z"], ["A", "Y"]]})
        status, fetched = self.request(
            "GET", f"/plans/{created['plan_record_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["source_audit"]["audit_id"],
                         self.audit["audit_id"])
        self.assertEqual(fetched["source_audit"]["evidence"],
                         self.audit["evidence"])
        self.assertEqual(fetched["tree"], created["tree"])

    def test_plan_idempotent_replay_and_conflict(self):
        url = f"/audits/{self.audit['audit_id']}/plans"
        payload = {"plan_id": "p3", "target": ["X", "Y"],
                   "pairs": [["A", "Y"]]}
        status, first = self.request("POST", url, payload)
        self.assertEqual(status, 201)
        status, replay = self.request("POST", url, dict(payload))
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["plan_record_id"], first["plan_record_id"])

        changed = dict(payload, pairs=[["A", "Z"], ["A", "Y"]])
        status, conflict = self.request("POST", url, changed)
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"], "plan_id_conflict")
        self.assertEqual(conflict["existing_plan_record_id"],
                         first["plan_record_id"])

        status, listing = self.request(
            "GET", f"/audits/{self.audit['audit_id']}/plans")
        self.assertEqual(status, 200)
        self.assertEqual(listing["count"], 1)

    def test_plan_against_unique_audit_is_409(self):
        unique = {
            "request_id": "api-unique-plan-src",
            "modulus": 10,
            "anchor": {"id": "A", "absolute": 0},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [{"id": "c1", "source": "A", "target": "B",
                             "lo": 3, "hi": 3}],
        }
        _, rec = self.request("POST", "/audits", unique)
        status, body = self.request(
            "POST", f"/audits/{rec['audit_id']}/plans",
            {"plan_id": "px", "target": ["A", "B"], "pairs": []})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "source_not_ambiguous")

    def test_invalid_plan_is_400_and_writes_nothing(self):
        status, body = self.request(
            "POST", f"/audits/{self.audit['audit_id']}/plans",
            {"plan_id": "p4", "target": ["A", "NOPE"], "pairs": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_input")
        self.assertTrue(body["problems"])
        status, listing = self.request(
            "GET", f"/audits/{self.audit['audit_id']}/plans")
        self.assertEqual(listing["count"], 0)

    def test_plan_against_missing_audit_is_404(self):
        status, body = self.request(
            "POST", "/audits/AUD-999999/plans",
            {"plan_id": "p5", "target": ["A", "X"], "pairs": []})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_undecidable_plan_returns_counterexample(self):
        status, body = self.request(
            "POST", f"/audits/{self.audit['audit_id']}/plans",
            {"plan_id": "p6", "target": ["X", "Y"], "pairs": [["A", "Z"]]})
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "undecidable")
        self.assertIsNone(body["tree"])
        ce = body["counterexample"]
        self.assertIn("timeline_before", ce)
        self.assertIn("timeline_after", ce)

    def test_unknown_plan_is_404(self):
        status, body = self.request("GET", "/plans/PLN-999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")


if __name__ == "__main__":
    unittest.main()

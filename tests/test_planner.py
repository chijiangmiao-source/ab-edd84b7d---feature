import unittest

from app.planner import (
    MAX_QUERIES,
    SourceNotAmbiguousError,
    build_plan,
    validate_plan_payload,
)
from app.solver import InputError


AMBIGUOUS_INPUT = {
    "request_id": "smoke-ambiguous-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
        {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
    ],
}

# Three same-residue events, each wrap in {0,1}, adjacent wrap differences
# bounded to {-1,0,1}: every before/same/after answer is live for [X,Z].
THREE_WAY_INPUT = {
    "request_id": "three-way",
    "modulus": 10,
    "anchor": {"id": "A", "absolute": 0},
    "events": [
        {"id": "X", "counter": 5},
        {"id": "Y", "counter": 5},
        {"id": "Z", "counter": 5},
    ],
    "constraints": [
        {"id": "a-x", "source": "A", "target": "X", "lo": 5, "hi": 15},
        {"id": "a-y", "source": "A", "target": "Y", "lo": 5, "hi": 15},
        {"id": "a-z", "source": "A", "target": "Z", "lo": 5, "hi": 15},
        {"id": "x-z", "source": "X", "target": "Z", "lo": -10, "hi": 10},
        {"id": "z-y", "source": "Z", "target": "Y", "lo": -10, "hi": 10},
    ],
}


def record(payload, status="ambiguous"):
    return {"status": status, "input": payload}


def leaves(tree, out=None):
    if out is None:
        out = []
    if tree["type"] == "internal":
        for answer, child in tree["answers"].items():
            if child is not None:
                leaves(child, out)
    else:
        out.append(tree)
    return out


class PruningAndBranchesTest(unittest.TestCase):
    def test_direct_query_primes_arithmetic_impossible_same(self):
        result = build_plan(record(AMBIGUOUS_INPUT), {
            "plan_id": "p1",
            "target": ["A", "C"],
            "queries": [{"id": "q-ac", "left": "A", "right": "C"}],
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["worst_case_questions"], 1)
        tree = result["tree"]
        self.assertEqual(tree["ask"], "q-ac")
        self.assertEqual(tree["impossible_answers"], ["same"])
        self.assertEqual(set(tree["reachable_answers"]), {"before", "after"})
        for answer in ("before", "after"):
            leaf = tree["answers"][answer]
            self.assertEqual(leaf["type"], "leaf")
            self.assertEqual(leaf["target_relation"], answer)
            self.assertEqual(leaf["answer_path"][0]["answer"], answer)

    def test_all_three_answers_reachable(self):
        result = build_plan(record(THREE_WAY_INPUT), {
            "plan_id": "p3",
            "target": ["X", "Z"],
            "queries": [{"id": "q", "left": "X", "right": "Z"}],
        })
        self.assertEqual(result["status"], "resolved")
        tree = result["tree"]
        self.assertEqual(tree["reachable_answers"], ["before", "same", "after"])
        self.assertEqual(tree["impossible_answers"], [])
        leaf_relations = {
            answer: tree["answers"][answer]["target_relation"]
            for answer in ("before", "same", "after")}
        self.assertEqual(
            leaf_relations,
            {"before": "before", "same": "same", "after": "after"})


class AdaptiveDepthTest(unittest.TestCase):
    def test_two_step_adaptive_plan_is_minimal_and_prunes_deep_branch(self):
        result = build_plan(record(THREE_WAY_INPUT), {
            "plan_id": "p-adapt",
            "target": ["X", "Y"],
            "queries": [
                {"id": "q-xz", "left": "X", "right": "Z"},
                {"id": "q-zy", "left": "Z", "right": "Y"},
            ],
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["worst_case_questions"], 2)
        root = result["tree"]
        # identifier tie-break: both root choices have worst case 2
        self.assertEqual(root["ask"], "q-xz")
        self.assertEqual(set(root["reachable_answers"]),
                         {"before", "same", "after"})
        # wraps are in {0,1}: k_X=0,k_Z=1,k_Y=2 cannot happen, so after
        # "before" on X,Z, asking Z,Y "before" again is pruned; symmetrically
        # for after/after.
        self.assertIsNone(root["answers"]["before"]["answers"]["before"])
        self.assertIsNone(root["answers"]["after"]["answers"]["after"])
        # every reachable leaf settles the target exactly once
        for leaf in leaves(root):
            self.assertEqual(leaf["type"], "leaf")
            self.assertIn(leaf["target_relation"],
                          ("before", "same", "after"))
        # depth-1 root answers never settle immediately; depth-2 branches do
        for answer in ("before", "same", "after"):
            child = root["answers"][answer]
            self.assertEqual(child["type"], "internal")
            for grand in child["answers"].values():
                if grand is not None:
                    self.assertEqual(grand["type"], "leaf")

    def test_uninformative_query_cannot_shortcut(self):
        # The direct query must be preferred over a pair that cannot decide.
        result = build_plan(record(AMBIGUOUS_INPUT), {
            "plan_id": "p-mix",
            "target": ["A", "C"],
            "queries": [
                {"id": "q-noise", "left": "A", "right": "B"},
                {"id": "q-direct", "left": "A", "right": "C"},
            ],
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["worst_case_questions"], 1)
        self.assertEqual(result["tree"]["ask"], "q-direct")

    def test_invariant_target_resolves_without_questions(self):
        # B is always strictly after A in every timeline, regardless of C.
        result = build_plan(record(AMBIGUOUS_INPUT), {
            "plan_id": "p0",
            "target": ["A", "B"],
            "queries": [],
        })
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["worst_case_questions"], 0)
        self.assertEqual(result["tree"]["type"], "leaf")
        self.assertEqual(result["tree"]["target_relation"], "before")


class CounterexampleTest(unittest.TestCase):
    def test_indistinguishable_path_with_two_timelines(self):
        result = build_plan(record(AMBIGUOUS_INPUT), {
            "plan_id": "p-fail",
            "target": ["A", "C"],
            "queries": [{"id": "only-ab", "left": "A", "right": "B"}],
        })
        self.assertEqual(result["status"], "indistinguishable")
        self.assertIsNone(result["worst_case_questions"])
        ce = result["counterexample"]
        self.assertIn("before", ce["possible_relations_at_leaf"])
        self.assertIn("after", ce["possible_relations_at_leaf"])
        t1, t2 = ce["timeline_1"], ce["timeline_2"]
        self.assertNotEqual(t1["target_relation"], t2["target_relation"])
        absolutes1 = {e["id"]: e["absolute"] for e in t1["events"]}
        absolutes2 = {e["id"]: e["absolute"] for e in t2["events"]}
        self.assertEqual(absolutes1["A"], absolutes2["A"])
        self.assertEqual(absolutes1["B"], absolutes2["B"])
        self.assertNotEqual(absolutes1["C"], absolutes2["C"])
        # both timelines replay to exactly every answer on the witness path
        for step in ce["answer_replay"]:
            self.assertEqual(step["timeline_1_answer"],
                             step["expected_answer"])
            self.assertEqual(step["timeline_2_answer"],
                             step["expected_answer"])
        # the path answers are feasible live answers, not pruned ones
        self.assertEqual(ce["answer_path"][0]["answer"], "before")

    def test_indistinguishable_after_exhausting_every_query(self):
        # E1..E7 each independently sit at 50 or 150 (anchor A = 95).
        # Asking about E3..E7 yields no information about the target E1/E2,
        # so after every available question the target is still unsettled and
        # the planner must hand back a replayable indistinguishable path.
        events = [{"id": f"E{i}", "counter": 50} for i in range(1, 8)]
        constraints = [
            {"id": f"a-e{i}", "source": "A", "target": f"E{i}",
             "lo": -45, "hi": 55}
            for i in range(1, 8)]
        payload_input = {
            "request_id": "independent",
            "modulus": 100,
            "anchor": {"id": "A", "absolute": 95},
            "events": events,
            "constraints": constraints,
        }
        result = build_plan(record(payload_input), {
            "plan_id": "p-budget",
            "target": ["E1", "E2"],
            "queries": [
                {"id": f"q{i}", "left": "A", "right": f"E{i}"}
                for i in range(3, 8)],
        })
        self.assertEqual(result["status"], "indistinguishable")
        ce = result["counterexample"]
        self.assertEqual(len(ce["answer_path"]), 5)
        t1, t2 = ce["timeline_1"], ce["timeline_2"]
        self.assertNotEqual(t1["target_relation"], t2["target_relation"])
        for step in ce["answer_replay"]:
            self.assertEqual(step["timeline_1_answer"],
                             step["expected_answer"])
            self.assertEqual(step["timeline_2_answer"],
                             step["expected_answer"])


class ValidationTest(unittest.TestCase):
    def assert_problems(self, payload, fragment):
        with self.assertRaises(InputError) as ctx:
            validate_plan_payload(record(AMBIGUOUS_INPUT), payload)
        self.assertTrue(
            any(fragment in p for p in ctx.exception.problems),
            f"expected {fragment!r} in {ctx.exception.problems}")

    def base(self):
        return {
            "plan_id": "pv",
            "target": ["A", "C"],
            "queries": [{"id": "q1", "left": "A", "right": "C"}],
        }

    def test_source_must_be_ambiguous(self):
        for status in ("unique", "unsatisfiable"):
            with self.assertRaises(SourceNotAmbiguousError):
                build_plan(record(AMBIGUOUS_INPUT, status), self.base())

    def test_target_event_must_exist(self):
        payload = self.base()
        payload["target"] = ["A", "ZZ"]
        self.assert_problems(payload, "does not exist")

    def test_query_event_must_exist(self):
        payload = self.base()
        payload["queries"][0]["left"] = "ZZ"
        self.assert_problems(payload, "does not exist")

    def test_duplicate_candidate_pair_rejected_even_when_reversed(self):
        payload = self.base()
        payload["queries"].append(
            {"id": "q2", "left": "C", "right": "A"})
        self.assert_problems(payload, "repeats an already submitted event pair")

    def test_duplicate_query_id_rejected(self):
        payload = self.base()
        payload["queries"].append(
            {"id": "q1", "left": "B", "right": "C"})
        self.assert_problems(payload, "duplicate query id")

    def test_self_pair_rejected(self):
        payload = self.base()
        payload["target"] = ["A", "A"]
        self.assert_problems(payload, "two distinct events")

    def test_too_many_queries(self):
        payload = self.base()
        payload["queries"] = [
            {"id": f"q{i:02d}", "left": "B", "right": "C"}
            for i in range(MAX_QUERIES + 1)]
        self.assert_problems(payload, f"at most {MAX_QUERIES}")

    def test_unknown_field_rejected(self):
        payload = self.base()
        payload["note"] = "nope"
        self.assert_problems(payload, "unknown field")


if __name__ == "__main__":
    unittest.main()

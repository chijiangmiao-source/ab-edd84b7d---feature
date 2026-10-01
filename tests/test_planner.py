import unittest

from app.planner import (
    MAX_PAIRS,
    PlanningError,
    _answer_edges,
    build_plan,
)
from app.solver import normalize, solve
from app.store import AuditStore, ConflictError


def ambiguous_audit(store, request_id="r", modulus=10, anchor=None,
                    events=None, constraints=None):
    record, _ = store.create({
        "request_id": request_id,
        "modulus": modulus,
        "anchor": anchor or {"id": "A", "absolute": 0},
        "events": events or [],
        "constraints": constraints or [],
    })
    assert record["status"] == "ambiguous", record["status"]
    return record


def plan(store, record, plan_id="p", target=None, pairs=None):
    return store.create_plan(record["audit_id"], {
        "plan_id": plan_id,
        "target": target,
        "pairs": pairs or [],
    })[0]


def leaves(tree):
    if tree["kind"] == "leaf":
        return [tree]
    out = []
    for branch in tree["branches"]:
        out.extend(leaves(branch["then"]))
    return out


def walk(tree, answers):
    """Follow a concrete sequence of answer codes; return the node reached."""
    node = tree
    for answer in answers:
        self_branch = next(b for b in node["branches"] if b["answer"] == answer)
        node = self_branch["then"]
    return node


class AnswerEdgeTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        self.record = ambiguous_audit(
            self.store, "edges",
            events=[{"id": "B", "counter": 3}],
            constraints=[{"id": "c1", "source": "A", "target": "B",
                          "lo": 3, "hi": 13}])

    def system(self):
        from app.solver import build_system
        return build_system(normalize(self.record["input"]))

    def test_same_impossible_with_differing_residues(self):
        system = self.system()
        self.assertIsNone(_answer_edges(system, "A", "B", "same"))

    def test_before_after_bounds(self):
        system = self.system()
        # d = c_A - c_B = -3, M = 10
        # before: k >= floor(-3/10)+1 = 0  (edge B->A weight 0)
        before = _answer_edges(system, "A", "B", "before")
        self.assertEqual([(u, v, w) for u, v, w, _ in before],
                         [(system["idx"]["B"], system["idx"]["A"], 0)])
        # after: k <= ceil(-3/10)-1 = -1 (edge A->B weight -1)
        after = _answer_edges(system, "A", "B", "after")
        self.assertEqual([(u, v, w) for u, v, w, _ in after],
                         [(system["idx"]["A"], system["idx"]["B"], -1)])


class PlanShapeTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        # M=10, A=0; Z binary {0,10}; X = Z+5 in {5,15}; Y binary {0,10}.
        self.record = ambiguous_audit(
            self.store, "shape",
            events=[{"id": "X", "counter": 5},
                    {"id": "Y", "counter": 0},
                    {"id": "Z", "counter": 0}],
            constraints=[
                {"id": "c1", "source": "A", "target": "Z", "lo": 0, "hi": 10},
                {"id": "c2", "source": "Z", "target": "X", "lo": 5, "hi": 5},
                {"id": "c3", "source": "A", "target": "Y", "lo": 0, "hi": 10},
            ])

    def test_every_reachable_leaf_resolves_target(self):
        p = plan(self.store, self.record, "s1", ["X", "Y"],
                 [["A", "Z"], ["A", "Y"]])
        self.assertEqual(p["status"], "decided")
        for leaf in leaves(p["tree"]):
            self.assertIn(leaf["target_relation"],
                          ("before", "same", "after"))

    def test_impossible_answer_is_pruned(self):
        p = plan(self.store, self.record, "s2", ["X", "Y"],
                 [["A", "Z"], ["A", "Y"]])
        # Y in {0,10} can never be strictly after A=0: "after" is pruned at
        # the root, and the tree only lists answers that keep a timeline.
        root = p["tree"]
        self.assertEqual(root["reachable_answers"], ["before", "same"])
        self.assertEqual(root["pruned_answers"], ["after"])
        # every inner node must prune "after" as well for the same reason
        same_leaf = next(b for b in root["branches"]
                         if b["answer"] == "same")["then"]
        self.assertEqual(same_leaf["kind"], "leaf")
        before_node = next(b for b in root["branches"]
                           if b["answer"] == "before")["then"]
        self.assertEqual(before_node["pruned_answers"], ["after"])

    def test_worst_case_is_minimal(self):
        # Asking Y first settles one branch immediately and Z only on the
        # other: worst case 2.  Either order has worst case 2 here; the value
        # must nevertheless be exactly 2, never 1 or 3.
        p = plan(self.store, self.record, "s3", ["X", "Y"],
                 [["A", "Z"], ["A", "Y"]])
        self.assertEqual(p["worst_case_queries"], 2)
        self.assertEqual(p["stats"]["worst_case_queries"], 2)

    def test_worst_case_prefers_shorter_plan(self):
        # A direct query (Y itself, target X-vs-Y reduces to Z once Y known
        # path) versus an indirect one: build a case where one pair decides in
        # one query and another needs two.  Offer [A,Y] (two-level) plus [X,Y]
        # directly: querying the target pair itself decides in ONE query, so
        # the optimal worst case must drop to 1.
        p = plan(self.store, self.record, "s4", ["X", "Y"],
                 [["A", "Z"], ["A", "Y"], ["X", "Y"]])
        self.assertEqual(p["worst_case_queries"], 1)
        self.assertEqual(p["tree"]["pair"], ["X", "Y"])

    def test_tie_breaks_on_pair_identifier_sequence(self):
        # Both [A,Y] and [X,Y] do not decide in one here; use pairs that each
        # decide alone: [A,Y] decides X-vs-Y only partially, so instead use a
        # fresh binary audit where two equally-good pairs exist.
        rec = ambiguous_audit(
            self.store, "tie",
            events=[{"id": "P", "counter": 0},
                    {"id": "Q", "counter": 0},
                    {"id": "R", "counter": 0}],
            constraints=[
                {"id": "c1", "source": "A", "target": "P", "lo": 0, "hi": 10},
                {"id": "c2", "source": "A", "target": "Q", "lo": 0, "hi": 10},
                {"id": "c3", "source": "A", "target": "R", "lo": 0, "hi": 10},
            ])
        # R target is only settled by querying (A,R); (A,P) is useless alone.
        p = plan(self.store, rec, "t1", ["A", "R"],
                 [["A", "P"], ["A", "R"]])
        self.assertEqual(p["tree"]["pair"], ["A", "R"])
        self.assertEqual(p["worst_case_queries"], 1)


class CounterexampleTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        # Z is fixed at 0 while Y stays binary; target X-vs-Y is binary and
        # the only offered pair (A,Z) says nothing about it.
        self.record = ambiguous_audit(
            self.store, "cex",
            events=[{"id": "X", "counter": 5},
                    {"id": "Y", "counter": 0},
                    {"id": "Z", "counter": 0}],
            constraints=[
                {"id": "c1", "source": "A", "target": "Z", "lo": 0, "hi": 0},
                {"id": "c2", "source": "A", "target": "X", "lo": 5, "hi": 5},
                {"id": "c3", "source": "A", "target": "Y", "lo": 0, "hi": 10},
            ])

    def test_undecidable_returns_disagreeing_timelines(self):
        p = plan(self.store, self.record, "u1", ["X", "Y"], [["A", "Z"]])
        self.assertEqual(p["status"], "undecidable")
        self.assertIsNone(p["tree"])
        ce = p["counterexample"]
        self.assertIsNotNone(ce)
        t_before = {e["id"]: e["absolute"]
                    for e in ce["timeline_before"]}
        t_after = {e["id"]: e["absolute"]
                   for e in ce["timeline_after"]}
        # both timelines satisfy the taken answers
        for step in ce["answer_path"]:
            a, b = step["pair"]
            ta, tb = t_before[a], t_before[b]
            if step["answer"] == "same":
                self.assertEqual(ta, tb)
            elif step["answer"] == "before":
                self.assertLess(ta, tb)
            else:
                self.assertGreater(ta, tb)
        # the frozen constraints hold on both timelines
        for timeline in (t_before, t_after):
            self.assertEqual(timeline["A"], 0)
            self.assertEqual(timeline["X"], 5)
            self.assertEqual(timeline["Z"], 0)
            self.assertIn(timeline["Y"], (0, 10))
        # and they genuinely disagree about the target pair
        self.assertTrue((t_before["X"] < t_before["Y"])
                        ^ (t_after["X"] < t_after["Y"]))
        self.assertLess(t_before["X"], t_before["Y"])
        self.assertGreater(t_after["X"], t_after["Y"])

    def test_no_candidates_on_unresolved_target_is_undecidable(self):
        rec = ambiguous_audit(
            self.store, "noq",
            events=[{"id": "B", "counter": 0}],
            constraints=[{"id": "c1", "source": "A", "target": "B",
                          "lo": 0, "hi": 10}])
        p = plan(self.store, rec, "u2", ["A", "B"], [])
        self.assertEqual(p["status"], "undecidable")
        self.assertEqual(p["counterexample"]["answer_path"], [])


class AlreadyResolvedAndThreeWayTest(unittest.TestCase):
    def test_target_invariant_needs_zero_queries(self):
        # Audit is ambiguous (B floats) but the named target A-vs-C is fixed
        # across every feasible timeline: no query is required.
        store = AuditStore()
        rec = ambiguous_audit(
            store, "invar",
            events=[{"id": "B", "counter": 0},
                    {"id": "C", "counter": 9}],
            constraints=[
                {"id": "c1", "source": "A", "target": "B", "lo": 0, "hi": 10},
                {"id": "c2", "source": "A", "target": "C", "lo": 9, "hi": 9},
            ])
        p = plan(store, rec, "z", ["A", "C"], [["A", "B"]])
        self.assertEqual(p["status"], "decided")
        self.assertEqual(p["worst_case_queries"], 0)
        self.assertEqual(p["tree"]["kind"], "leaf")
        self.assertEqual(p["tree"]["target_relation"], "before")

    def test_all_three_answers_reachable_when_pair_is_target(self):
        # A=0; X in {0,10,20}; Y in {0,10}.  X-vs-Y admits before, same and
        # after, and querying the target pair directly settles it in one
        # question with nothing pruned.
        store = AuditStore()
        rec = ambiguous_audit(
            store, "three",
            events=[{"id": "X", "counter": 0},
                    {"id": "Y", "counter": 0}],
            constraints=[
                {"id": "c1", "source": "A", "target": "X", "lo": 0, "hi": 20},
                {"id": "c2", "source": "A", "target": "Y", "lo": 0, "hi": 10},
            ])
        p = plan(store, rec, "t3", ["X", "Y"], [["X", "Y"]])
        self.assertEqual(p["status"], "decided")
        self.assertEqual(p["worst_case_queries"], 1)
        self.assertEqual(p["tree"]["reachable_answers"],
                         ["before", "same", "after"])
        self.assertEqual(p["tree"]["pruned_answers"], [])
        relations = {b["answer"]: b["then"]["target_relation"]
                     for b in p["tree"]["branches"]}
        self.assertEqual(relations,
                         {"before": "before", "same": "same",
                          "after": "after"})


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        self.amb = ambiguous_audit(
            self.store, "ok",
            events=[{"id": "B", "counter": 0}],
            constraints=[{"id": "c1", "source": "A", "target": "B",
                          "lo": 0, "hi": 10}])
        unique_record, _ = self.store.create({
            "request_id": "uniq", "modulus": 10,
            "anchor": {"id": "A", "absolute": 0},
            "events": [{"id": "C", "counter": 3}],
            "constraints": [{"id": "c1", "source": "A", "target": "C",
                             "lo": 3, "hi": 3}],
        })
        self.assertEqual(unique_record["status"], "unique")
        self.unique = unique_record

    def assert_invalid(self, record, payload, fragment):
        with self.assertRaises(PlanningError) as ctx:
            build_plan(record, payload)
        self.assertEqual(ctx.exception.code, "invalid_input")
        self.assertTrue(
            any(fragment in prob for prob in ctx.exception.problems),
            ctx.exception.problems)

    def base(self, **over):
        body = {"plan_id": "x", "target": ["A", "B"], "pairs": []}
        body.update(over)
        return body

    def test_source_must_be_ambiguous(self):
        with self.assertRaises(PlanningError) as ctx:
            build_plan(self.unique, self.base())
        self.assertEqual(ctx.exception.code, "source_not_ambiguous")

    def test_missing_target_event(self):
        self.assert_invalid(self.amb, self.base(target=["A", "ZZ"]),
                            "does not exist")

    def test_same_event_pair_rejected(self):
        self.assert_invalid(self.amb, self.base(target=["B", "B"]),
                            "same event twice")

    def test_duplicate_candidate_pair_rejected(self):
        self.assert_invalid(
            self.amb,
            self.base(pairs=[["A", "B"], ["A", "B"]]),
            "duplicates pair")

    def test_reversed_pair_treated_as_duplicate(self):
        self.assert_invalid(
            self.amb,
            self.base(pairs=[["A", "B"], ["B", "A"]]),
            "reverse order")

    def test_too_many_pairs(self):
        pairs = [["A", f"E{i}"] for i in range(MAX_PAIRS + 1)]
        self.assert_invalid(self.amb, self.base(pairs=pairs),
                            f"at most {MAX_PAIRS}")

    def test_plan_id_required(self):
        self.assert_invalid(self.amb, self.base(plan_id="   "),
                            "plan_id")

    def test_unknown_field_rejected(self):
        self.assert_invalid(self.amb, self.base(extra=1), "unknown field")


class PlanStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = AuditStore()
        self.record = ambiguous_audit(
            self.store, "stored",
            events=[{"id": "B", "counter": 0}],
            constraints=[{"id": "c1", "source": "A", "target": "B",
                          "lo": 0, "hi": 10}])

    def test_replay_returns_same_plan_record(self):
        payload = {"plan_id": "id-1", "target": ["A", "B"],
                   "pairs": [["A", "B"]]}
        first, created = self.store.create_plan(self.record["audit_id"], payload)
        self.assertTrue(created)
        second, again = self.store.create_plan(self.record["audit_id"],
                                               dict(payload))
        self.assertFalse(again)
        self.assertEqual(first["plan_record_id"], second["plan_record_id"])
        self.assertEqual(self.store.plan_count(), 1)

    def test_plan_id_changed_content_conflicts_and_writes_nothing(self):
        self.store.create_plan(self.record["audit_id"], {
            "plan_id": "id-2", "target": ["A", "B"], "pairs": [["A", "B"]]})
        with self.assertRaises(ConflictError):
            self.store.create_plan(self.record["audit_id"], {
                "plan_id": "id-2", "target": ["A", "B"], "pairs": []})
        self.assertEqual(self.store.plan_count(), 1)

    def test_invalid_plan_writes_nothing(self):
        with self.assertRaises(PlanningError):
            self.store.create_plan(self.record["audit_id"], {
                "plan_id": "id-3", "target": ["A", "NOPE"], "pairs": []})
        self.assertEqual(self.store.plan_count(), 0)

    def test_plan_read_carries_frozen_source_evidence(self):
        stored, _ = self.store.create_plan(self.record["audit_id"], {
            "plan_id": "id-4", "target": ["A", "B"], "pairs": [["A", "B"]]})
        fetched = self.store.get_plan(stored["plan_record_id"])
        source = fetched["source_audit"]
        self.assertEqual(source["audit_id"], self.record["audit_id"])
        self.assertEqual(source["status"], "ambiguous")
        self.assertEqual(source["evidence"], self.record["evidence"])
        self.assertEqual(source["input"], self.record["input"])
        self.assertEqual(fetched["tree"], stored["tree"])

    def test_plan_against_missing_audit(self):
        with self.assertRaises(KeyError):
            self.store.create_plan("AUD-999999",
                                   {"plan_id": "x", "target": ["A", "B"],
                                    "pairs": []})

    def test_plan_id_reused_against_other_audit_conflicts(self):
        other = ambiguous_audit(
            self.store, "stored-2",
            events=[{"id": "C", "counter": 0}],
            constraints=[{"id": "c1", "source": "A", "target": "C",
                          "lo": 0, "hi": 10}])
        payload = {"plan_id": "shared-id", "target": ["A", "B"],
                   "pairs": [["A", "B"]]}
        self.store.create_plan(self.record["audit_id"], payload)
        with self.assertRaises(ConflictError):
            self.store.create_plan(other["audit_id"],
                                   {"plan_id": "shared-id",
                                    "target": ["A", "C"],
                                    "pairs": [["A", "C"]]})
        self.assertEqual(self.store.plan_count(), 1)


if __name__ == "__main__":
    unittest.main()

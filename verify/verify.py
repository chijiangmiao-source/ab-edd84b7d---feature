"""One-shot acceptance harness for the deep-space audit service.

Runs three gates and reports the outcome through the process exit code:

1. build check -- every Python source compiles;
2. code tests  -- the unit-test suite (solver, store, planner, API);
3. API smoke   -- HTTP checks against a live service at APP_URL covering the
   reference unwrap (B=103), the ambiguous twin timelines, the bidirectional
   conflict chain, idempotent record creation, and the adaptive interrogation
   plans: reachable branches, pruning of impossible answers, the shortest
   canonical plan, and a failure counterexample with two disagreeing
   timelines.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:8080").rstrip("/")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" -- {detail}"
    print(line, flush=True)


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        APP_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


# ---------------------------------------------------------------------------
# gate 1 + 2: build check and code tests
# ---------------------------------------------------------------------------

def gate_build():
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "tests", "verify"],
        cwd=ROOT, capture_output=True, text=True)
    detail = ""
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else "compileall failed"
    check("build: all sources compile", proc.returncode == 0, detail)


def gate_tests():
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT, capture_output=True, text=True)
    lines = (proc.stderr + proc.stdout).strip().splitlines()
    check("code: unit test suite", proc.returncode == 0,
          lines[-1] if lines else "")


# ---------------------------------------------------------------------------
# gate 3: HTTP smoke against the live service
# ---------------------------------------------------------------------------

UNIQUE_PAYLOAD = {
    "request_id": "smoke-unique-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
    ],
}

AMBIGUOUS_PAYLOAD = {
    "request_id": "smoke-ambiguous-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}, {"id": "C", "counter": 50}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 108},
        {"id": "c2", "source": "B", "target": "C", "lo": -53, "hi": 47},
    ],
}

CONFLICT_PAYLOAD = {
    "request_id": "smoke-conflict-1",
    "modulus": 100,
    "anchor": {"id": "A", "absolute": 95},
    "events": [{"id": "B", "counter": 3}],
    "constraints": [
        {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        {"id": "c2", "source": "B", "target": "A", "lo": -108, "hi": -108},
    ],
}

# Adaptive-plan fixture: M=10, A=0; Z in {0,10}; X = Z+5 in {5,15};
# Y independently in {0,10}.  Target order X-vs-Y is genuinely ambiguous;
# querying (A,Y) settles it outright on "same" (Y=0 -> X=5|15 > 0 -> after)
# and leaves one more (A,Z) question on "before" (Y=10).
PLAN_PAYLOAD = {
    "request_id": "smoke-plan-1",
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


def follow(tree, answers):
    node = tree
    for answer in answers:
        branch = next(b for b in node["branches"] if b["answer"] == answer)
        node = branch["then"]
    return node


def timeline_map(timeline):
    return {e["id"]: e for e in timeline}


def wait_ready():
    for _ in range(60):
        try:
            status, _body = request("GET", "/health")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def smoke_health():
    status, body = request("GET", "/health")
    expect(status == 200, f"health returned {status}")
    expect(body.get("status") == "ok", f"unexpected health body {body}")


def smoke_unique_unwrap():
    status, body = request("POST", "/audits", UNIQUE_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "unique", f"status={body.get('status')}")
    expect(body.get("replayed") is False, "first create must not be a replay")
    timeline = timeline_map(body["conclusion"]["timeline"])
    expect(timeline["B"]["absolute"] == 103,
           f"B unwrapped to {timeline['B']['absolute']}, expected 103")
    expect(timeline["B"]["wrap"] == 1, "B wrap count must be 1")
    expect(timeline["A"]["absolute"] == 95, "anchor must stay at 95")


def smoke_frozen_record():
    status, created = request("POST", "/audits",
                              {**UNIQUE_PAYLOAD, "request_id": "smoke-frozen-1"})
    expect(status == 201, f"create returned {status}")
    status, fetched = request("GET", f"/audits/{created['audit_id']}")
    expect(status == 200, f"read returned {status}")
    expect(fetched["input"] == {**UNIQUE_PAYLOAD, "request_id": "smoke-frozen-1"},
           "frozen input does not match the submitted payload")
    expect(fetched["conclusion"] == created["conclusion"],
           "frozen conclusion changed between create and read")
    expect("evidence" in fetched and "derivation" in fetched["evidence"],
           "frozen evidence missing")


def smoke_ambiguous_timelines():
    status, body = request("POST", "/audits", AMBIGUOUS_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "ambiguous", f"status={body.get('status')}")
    first, second = body["conclusion"]["timelines"]
    t1, t2 = timeline_map(first), timeline_map(second)
    expect(t1["B"]["absolute"] == 103 and t1["C"]["absolute"] == 50,
           f"timeline 1 unexpected: {first}")
    expect(t2["B"]["absolute"] == 103 and t2["C"]["absolute"] == 150,
           f"timeline 2 unexpected: {second}")
    rel = body["conclusion"]["first_unstable_relation"]
    expect(rel is not None, "ambiguous case must report an unstable relation")
    expect(rel["events"] == ["A", "C"], f"unstable pair: {rel['events']}")
    expect(rel["in_timeline_1"] == "after", "A must follow C in timeline 1")
    expect(rel["in_timeline_2"] == "before", "A must precede C in timeline 2")


def smoke_conflict_chain():
    status, body = request("POST", "/audits", CONFLICT_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "unsatisfiable", f"status={body.get('status')}")
    chain = body["conclusion"]["conflict_chain"]
    expect(chain["constraints"] == ["c1", "c2"],
           f"chain constraints: {chain['constraints']}")
    total = sum(step["weight"] for step in chain["steps"])
    expect(chain["total_weight"] == total,
           "chain weights do not recompute to the reported total")
    expect(total < 0, "conflict chain must close with a negative total")
    expect(chain["cycle"][0] == chain["cycle"][-1], "chain must be a cycle")


def smoke_idempotency():
    _, before = request("GET", "/audits")
    payload = {**UNIQUE_PAYLOAD, "request_id": "smoke-idem-1"}
    status, created = request("POST", "/audits", payload)
    expect(status == 201, f"create returned {status}")

    status, replay = request("POST", "/audits", copy.deepcopy(payload))
    expect(status == 200, f"replay returned {status}")
    expect(replay["replayed"] is True, "replay must be flagged")
    expect(replay["audit_id"] == created["audit_id"],
           "replay must return the original audit id")

    changed = copy.deepcopy(payload)
    changed["constraints"][0]["hi"] = 9
    status, conflict = request("POST", "/audits", changed)
    expect(status == 409, f"changed payload returned {status}, expected 409")
    expect(conflict.get("existing_audit_id") == created["audit_id"],
           "409 must reference the original audit")

    _, after = request("GET", "/audits")
    expect(after["count"] == before["count"] + 1,
           f"record count moved {before['count']} -> {after['count']}; "
           "the rejected retry must not add a record")


def smoke_validation_and_404():
    disconnected = {
        "request_id": "smoke-invalid-1",
        "modulus": 100,
        "anchor": {"id": "A", "absolute": 95},
        "events": [{"id": "B", "counter": 3}, {"id": "Z", "counter": 1}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "B", "lo": 8, "hi": 8},
        ],
    }
    status, body = request("POST", "/audits", disconnected)
    expect(status == 400, f"disconnected event returned {status}")
    expect(body.get("error") == "invalid_input", f"error body: {body}")

    status, _ = request("GET", "/audits/AUD-000000")
    expect(status == 404, f"unknown audit returned {status}")


def smoke_plan_audit_source():
    status, body = request("POST", "/audits", PLAN_PAYLOAD)
    expect(status == 201, f"create returned {status}: {body}")
    expect(body.get("status") == "ambiguous",
           f"plan source must be ambiguous, got {body.get('status')}")
    return body["audit_id"]


def smoke_plan_reachable_branches(audit_id):
    status, body = request("POST", f"/audits/{audit_id}/plans", {
        "plan_id": "smoke-plan-reachable",
        "target": ["X", "Y"],
        "pairs": [["A", "Z"], ["A", "Y"]],
    })
    expect(status == 201, f"plan create returned {status}: {body}")
    expect(body.get("status") == "decided", f"plan status {body.get('status')}")
    tree = body["tree"]
    # only before/same can happen: Y never strictly follows A=0
    expect(tree["reachable_answers"] == ["before", "same"],
           f"reachable answers: {tree['reachable_answers']}")
    expect(tree["pruned_answers"] == ["after"],
           f"pruned answers: {tree['pruned_answers']}")
    # same (Y=0) -> X in {5,15} always after Y: a leaf in one query
    same_node = follow(tree, ["same"])
    expect(same_node["kind"] == "leaf"
           and same_node["target_relation"] == "after",
           f"same branch must be an after leaf: {same_node}")
    # before (Y=10): ask Z; Z=0 -> X=5 before Y, Z=10 -> X=15 after Y
    before_node = follow(tree, ["before"])
    expect(before_node["kind"] == "query", "before branch must ask again")
    expect(before_node["pruned_answers"] == ["after"],
           "Z cannot follow A either; after must be pruned")
    b_same = follow(before_node, ["same"])
    b_before = follow(before_node, ["before"])
    expect(b_same["kind"] == "leaf" and b_same["target_relation"] == "before",
           f"Z=0 path: {b_same}")
    expect(b_before["kind"] == "leaf" and b_before["target_relation"] == "after",
           f"Z=10 path: {b_before}")


def smoke_plan_impossible_answer_pruning(audit_id):
    # Dedicated fixture: A=5; P(counter 3) in {3,13}, so A is after P when
    # P=3 and before P when P=13, but A and P can never share a tick
    # (residues 5 and 3 modulo 10).  Querying (A,P) decides the target in one
    # question while the equal-tick answer is arithmetically impossible.
    payload = {
        "request_id": "smoke-plan-prune-src",
        "modulus": 10,
        "anchor": {"id": "A", "absolute": 5},
        "events": [{"id": "P", "counter": 3}],
        "constraints": [
            {"id": "c1", "source": "A", "target": "P", "lo": -2, "hi": 8},
        ],
    }
    status, src = request("POST", "/audits", payload)
    expect(status == 201 and src["status"] == "ambiguous",
           f"prune source: status {status} {src.get('status')}")
    status, body = request("POST", f"/audits/{src['audit_id']}/plans", {
        "plan_id": "smoke-plan-prune",
        "target": ["A", "P"],
        "pairs": [["A", "P"]],
    })
    expect(status == 201, f"plan create returned {status}: {body}")
    expect(body.get("status") == "decided", f"plan status {body.get('status')}")
    tree = body["tree"]
    expect(tree["pair"] == ["A", "P"], f"root pair: {tree['pair']}")
    expect(tree["reachable_answers"] == ["before", "after"],
           f"reachable answers: {tree['reachable_answers']}")
    expect(tree["pruned_answers"] == ["same"],
           f"equal tick with differing residues must be pruned: "
           f"{tree['pruned_answers']}")
    expect(body["worst_case_queries"] == 1,
           f"one-query decision expected, got {body['worst_case_queries']}")


def smoke_plan_shortest_canonical(audit_id):
    # Offering the target pair itself decides in a single query; the engine
    # must prefer it over the two-level chain even though both are listed.
    status, body = request("POST", f"/audits/{audit_id}/plans", {
        "plan_id": "smoke-plan-shortest",
        "target": ["X", "Y"],
        "pairs": [["A", "Z"], ["A", "Y"], ["X", "Y"]],
    })
    expect(status == 201, f"plan create returned {status}: {body}")
    expect(body["worst_case_queries"] == 1,
           f"worst case must be 1, got {body['worst_case_queries']}")
    expect(body["tree"]["pair"] == ["X", "Y"],
           f"root must be the one-query pair, got {body['tree']['pair']}")
    # equal tick is impossible between residues 5 and 0
    expect(body["tree"]["pruned_answers"] == ["same"],
           f"unexpected pruned answers: {body['tree']['pruned_answers']}")


def smoke_plan_failure_counterexample(audit_id):
    # Only (A,Z) is offered while Z is free {0,10}; it correlates with X and
    # cannot by itself settle X-vs-Y in every branch -> the plan must fail
    # with a recomputable answer path and two timelines that disagree.
    status, body = request("POST", f"/audits/{audit_id}/plans", {
        "plan_id": "smoke-plan-fail",
        "target": ["X", "Y"],
        "pairs": [["A", "Z"]],
    })
    expect(status == 201, f"plan create returned {status}: {body}")
    expect(body.get("status") == "undecidable",
           f"expected undecidable, got {body.get('status')}")
    expect(body.get("tree") is None, "undecidable plan must carry no tree")
    ce = body["counterexample"]
    expect(ce is not None, "undecidable plan must carry a counterexample")
    tb = {e["id"]: e["absolute"] for e in ce["timeline_before"]}
    ta = {e["id"]: e["absolute"] for e in ce["timeline_after"]}
    # every answer on the witness path must really hold on both timelines
    for step in ce["answer_path"]:
        a, b = step["pair"]
        for tl in (tb, ta):
            if step["answer"] == "same":
                expect(tl[a] == tl[b], f"witness answer {step} violated")
            elif step["answer"] == "before":
                expect(tl[a] < tl[b], f"witness answer {step} violated")
            else:
                expect(tl[a] > tl[b], f"witness answer {step} violated")
    # both satisfy the frozen constraints ...
    for tl in (tb, ta):
        expect(tl["A"] == 0, "anchor must stay at 0")
        expect(tl["X"] - tl["Z"] == 5, "c2 forces X = Z+5")
        expect(tl["Z"] in (0, 10) and tl["Y"] in (0, 10),
               f"wrap values outside frozen ranges: {tl}")
    # ... yet they disagree about the target pair
    expect(tb["X"] < tb["Y"], f"before timeline wrong: {tb}")
    expect(ta["X"] > ta["Y"], f"after timeline wrong: {ta}")


def smoke_plan_rejections_and_freeze(audit_id):
    # source must be ambiguous: reject against the unique reference audit
    status, unique = request("POST", "/audits", UNIQUE_PAYLOAD)
    expect(status in (200, 201), f"reference audit status {status}")
    status, body = request("POST",
                           f"/audits/{unique['audit_id']}/plans",
                           {"plan_id": "x", "target": ["A", "B"],
                            "pairs": []})
    expect(status == 409, f"non-ambiguous source returned {status}")
    expect(body.get("error") == "source_not_ambiguous", f"error body: {body}")

    # unknown target event -> 400 and no half-written plan
    status, body = request("POST", f"/audits/{audit_id}/plans", {
        "plan_id": "smoke-plan-bad", "target": ["A", "NOPE"], "pairs": []})
    expect(status == 400, f"missing event returned {status}")
    # duplicate candidate pair -> 400
    status, body = request("POST", f"/audits/{audit_id}/plans", {
        "plan_id": "smoke-plan-dup", "target": ["X", "Y"],
        "pairs": [["A", "Z"], ["A", "Z"]]})
    expect(status == 400, f"duplicate pair returned {status}")

    # plan_id reuse with changed content -> 409, no new record
    url = f"/audits/{audit_id}/plans"
    _, before = request("GET", url)
    payload = {"plan_id": "smoke-plan-idem", "target": ["X", "Y"],
               "pairs": [["X", "Y"]]}
    status, first = request("POST", url, payload)
    expect(status == 201, f"plan create returned {status}")
    status, replay = request("POST", url, dict(payload))
    expect(status == 200 and replay["replayed"] is True,
           f"plan replay returned {status}")
    expect(replay["plan_record_id"] == first["plan_record_id"],
           "plan replay must return the same record id")
    status, conflict = request("POST", url,
                               dict(payload, pairs=[["A", "Z"]]))
    expect(status == 409, f"changed plan content returned {status}")
    status, listing = request("GET", url)
    expect(listing["count"] == before["count"] + 1,
           f"exactly one new plan allowed: {before['count']} -> "
           f"{listing['count']}")

    # reading the plan returns the frozen source audit evidence alongside it
    status, fetched = request(
        "GET", f"/plans/{first['plan_record_id']}")
    expect(status == 200, f"plan read returned {status}")
    expect(fetched["source_audit"]["audit_id"] == audit_id,
           "plan read must name its source audit")
    expect(fetched["source_audit"]["status"] == "ambiguous",
           "plan read must carry the source status")
    expect("evidence" in fetched["source_audit"]
           and "derivation" in fetched["source_audit"]["evidence"],
           "plan read must carry frozen source evidence")


def gate_http():
    if not wait_ready():
        check("http: service reachable", False, f"no /health from {APP_URL}")
        return
    check("http: service reachable", True, APP_URL)
    smokes = [
        ("http: health", smoke_health),
        ("http: reference unwrap B=103", smoke_unique_unwrap),
        ("http: frozen record readable", smoke_frozen_record),
        ("http: ambiguous twin timelines", smoke_ambiguous_timelines),
        ("http: bidirectional conflict chain", smoke_conflict_chain),
        ("http: idempotent records", smoke_idempotency),
        ("http: validation and 404", smoke_validation_and_404),
    ]
    for name, fn in smokes:
        try:
            fn()
        except AssertionError as exc:
            check(name, False, str(exc))
        except Exception as exc:  # keep reporting the remaining checks
            check(name, False, f"{type(exc).__name__}: {exc}")
        else:
            check(name, True)

    # Adaptive-plan suite: one ambiguous source audit shared by every plan
    # check so the branch/pruning/optimality/counterexample scenarios stay
    # consistent with each other.
    try:
        plan_audit_id = smoke_plan_audit_source()
    except AssertionError as exc:
        check("http: plan source audit is ambiguous", False, str(exc))
        return
    except Exception as exc:
        check("http: plan source audit is ambiguous", False,
              f"{type(exc).__name__}: {exc}")
        return
    check("http: plan source audit is ambiguous", True)

    plan_smokes = [
        ("http: plan reachable branches decide target",
         lambda: smoke_plan_reachable_branches(plan_audit_id)),
        ("http: plan prunes impossible answers",
         lambda: smoke_plan_impossible_answer_pruning(plan_audit_id)),
        ("http: plan shortest canonical worst case",
         lambda: smoke_plan_shortest_canonical(plan_audit_id)),
        ("http: plan failure counterexample",
         lambda: smoke_plan_failure_counterexample(plan_audit_id)),
        ("http: plan rejections and frozen source read",
         lambda: smoke_plan_rejections_and_freeze(plan_audit_id)),
    ]
    for name, fn in plan_smokes:
        try:
            fn()
        except AssertionError as exc:
            check(name, False, str(exc))
        except Exception as exc:
            check(name, False, f"{type(exc).__name__}: {exc}")
        else:
            check(name, True)


def main():
    print(f"acceptance target: {APP_URL}", flush=True)
    gate_build()
    gate_tests()
    gate_http()
    passed = sum(RESULTS)
    total = len(RESULTS)
    ok = passed == total
    print(f"acceptance: {'OK' if ok else 'FAILED'} "
          f"({passed}/{total} checks passed)", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

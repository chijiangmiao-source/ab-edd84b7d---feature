"""Adaptive interrogation plans for ambiguous wrap-around audits.

An auditor opens an audit whose conclusion is ``ambiguous`` and names a target
pair whose precedence must be settled on site, together with up to ten
candidate event pairs the device may be queried about and a stable plan id.
Every query returns exactly one of three results:

* ``before`` -- the first event strictly precedes the second;
* ``same``   -- the two events share one absolute tick;
* ``after``  -- the first event strictly follows the second.

The service grows a finite, adaptive decision tree starting from the *frozen*
integer difference constraints of the original audit.  Each possible answer is
converted back into integer bounds on the wrap counts and, per branch, tightens
the feasible set with a shortest-path closure; answers the constraints already
rule out are pruned and never appear in the plan.

A tree is valid only when every reachable leaf fixes a *unique* precedence
relation for the target pair over every timeline still feasible there.  Two
canonical timelines or a single witness path are never treated as such a
guarantee: the decision uses the full difference-constraint closure, which is
exact for integer difference systems.

Optimality: among all valid plans the one with the smallest worst-case number
of queries is returned; ties are broken by the lexicographic sequence of
candidate-pair identifiers along the tree.  When no adaptive sequence of the
offered pairs can distinguish the target relation, the engine returns a
recomputable answer path shared by two timelines that disagree about the
target pair.
"""

from __future__ import annotations

from dataclasses import dataclass

from .solver import (
    _floyd_warshall,
    _infeasible,
    _lex_min_assignment,
    build_system,
    normalize,
)

MAX_PAIRS = 10

# Answer codes, in the fixed order used to serialise branches and break ties.
ANSWER_ORDER = ("before", "same", "after")


class PlanningError(Exception):
    """The plan request is incompatible with the referenced audit."""

    def __init__(self, code, problems):
        self.code = code
        self.problems = [str(p) for p in problems]
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------------------
# answers as integer difference bounds
# ---------------------------------------------------------------------------
#
# With ordered pair (a, b), residues c_a, c_b and modulus M, write
# d = c_a - c_b and k = wrap(b) - wrap(a).  The graph convention (from
# solver.py) is edge p->q weight w  <=>  x_q - x_p <= w.
#
# before: abs(a) < abs(b)  <=>  k >  d/M  <=>  k >= floor(d/M) + 1
#         edge b->a weight -(floor(d/M)+1)
# same:   abs(a) = abs(b)  <=>  k = -d/M  (only when d == 0 mod M)
#         edges a->b weight -d/M and b->a weight d/M
# after:  abs(a) > abs(b)  <=>  k <  d/M  <=>  k <= ceil(d/M) - 1
#         edge a->b weight ceil(d/M) - 1


def _answer_edges(system, a_id, b_id, answer):
    """Integer wrap-difference edges imposed by observing ``answer`` on the
    ordered pair (a, b), or None when that answer is arithmetically impossible
    regardless of the other constraints (same tick needs equal residues)."""
    idx = system["idx"]
    counters = system["counters"]
    modulus = system["modulus"]
    u, v = idx[a_id], idx[b_id]
    d = counters[a_id] - counters[b_id]
    if answer == "before":
        bound = d // modulus + 1              # floor(d/M) + 1
        return [(v, u, -bound, {"answer": answer, "side": "lower",
                                "bound": bound})]
    if answer == "after":
        bound = -((-d) // modulus) - 1        # ceil(d/M) - 1
        return [(u, v, bound, {"answer": answer, "side": "upper",
                               "bound": bound})]
    # same absolute tick
    if d % modulus != 0:
        return None
    k = -(d // modulus)                       # wrap(b) - wrap(a) == k
    return [
        (u, v, k, {"answer": "same", "side": "upper", "bound": k}),
        (v, u, -k, {"answer": "same", "side": "lower", "bound": k}),
    ]


def _edge_relation_text(u, v, w, system):
    labels = system["labels"]
    if u == 0:
        return f"wrap({labels[v]}) <= {w}"
    if v == 0:
        return f"wrap({labels[u]}) <= {w}"
    return f"wrap({labels[v]}) - wrap({labels[u]}) <= {w}"


def _branch_bounds(system, pair, answer):
    extra = _answer_edges(system, pair[0], pair[1], answer) or []
    return [{
        "events": [pair[0], pair[1]],
        "answer": answer,
        "edge": f"{system['labels'][u]}->{system['labels'][v]}",
        "weight": w,
        "relation": _edge_relation_text(u, v, w, system),
    } for u, v, w, _m in extra]


def _close(n, edges):
    dist = _floyd_warshall(n, edges)
    return tuple(tuple(row) for row in dist), not _infeasible(dist)


def _augment(n, base_dist, edges, extra):
    """Close ``base_dist`` (already a full shortest-path closure) after adding
    a handful of ``extra`` edges.  The edge-insertion rule
    ``d[i][j] = min(d[i][j], d[i][u] + w + d[v][j])`` is swept to fixpoint; if
    relaxation does not settle quickly (a negative cycle woven from the new
    edges), we fall back to an exact Floyd over the whole edge list so
    feasibility stays exact.
    """
    d = [list(row) for row in base_dist]
    for _ in range(len(extra) + 2):
        changed = False
        for (u, v, w, _m) in extra:
            if d[u][v] is not None and w >= d[u][v]:
                continue
            for i in range(n):
                diu = d[i][u]
                if diu is None:
                    continue
                rowi = d[i]
                dv = d[v]
                for j in range(n):
                    dvj = dv[j]
                    if dvj is None:
                        continue
                    cand = diu + w + dvj
                    if rowi[j] is None or cand < rowi[j]:
                        rowi[j] = cand
                        changed = True
        if any(d[i][i] < 0 for i in range(n)):
            return tuple(tuple(row) for row in d), False
        if not changed:
            return tuple(tuple(row) for row in d), True
    dist = _floyd_warshall(n, list(edges) + list(extra))
    return tuple(tuple(row) for row in dist), not _infeasible(dist)


# ---------------------------------------------------------------------------
# target resolution over the full feasible set
# ---------------------------------------------------------------------------


def target_relations(system, dist, target):
    """Relations of ordered target pair (x, y) still feasible under closure.

    Decided from the tight difference bounds, not from sampled timelines.
    """
    idx = system["idx"]
    counters = system["counters"]
    modulus = system["modulus"]
    i, j = idx[target[0]], idx[target[1]]
    delta_c = counters[target[0]] - counters[target[1]]
    k_min = -dist[i][j]          # min of wrap(x) - wrap(y)
    k_max = dist[j][i]           # max of wrap(x) - wrap(y)
    d_min = delta_c + modulus * k_min
    d_max = delta_c + modulus * k_max
    possible = []
    if d_min < 0:
        possible.append("before")
    # same tick needs wrap(x) - wrap(y) = -delta_c / M integral and in range
    if (-delta_c) % modulus == 0:
        k_equal = (-delta_c) // modulus
        if k_min <= k_equal <= k_max:
            possible.append("same")
    if d_max > 0:
        possible.append("after")
    return possible


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------


def normalize_plan_request(audit_record, payload):
    """Validate a plan request against the frozen audit record.

    Returns (target, candidates, plan_id).  Raises PlanningError listing every
    problem; the store persists nothing for a rejected request.
    """
    if not isinstance(payload, dict):
        raise PlanningError("invalid_input", ["payload must be a JSON object"])

    problems = []
    for key in sorted(payload):
        if key not in {"plan_id", "target", "pairs"}:
            problems.append(f"unknown field '{key}'")

    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        problems.append("plan_id must be a non-empty string")
        plan_id = None

    frozen_input = audit_record["input"]
    node_ids = {frozen_input["anchor"]["id"]}
    node_ids |= {e["id"] for e in frozen_input.get("events", [])}

    def parse_pair(raw, where):
        if not isinstance(raw, list) or len(raw) != 2:
            problems.append(f"{where} must be [event_id, event_id]")
            return None
        a, b = raw
        ok = True
        for pos, name in ((0, a), (1, b)):
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{where}[{pos}] must be a non-empty event id")
                ok = False
            elif name not in node_ids:
                problems.append(
                    f"{where}[{pos}] event '{name}' does not exist in "
                    f"audit {audit_record['audit_id']}")
                ok = False
        if ok and a == b:
            problems.append(f"{where} names the same event twice; order only "
                            "exists between distinct events")
            return None
        return (a, b) if ok else None

    target = parse_pair(payload.get("target"), "target")

    candidates = []
    raw_pairs = payload.get("pairs")
    if not isinstance(raw_pairs, list):
        problems.append("pairs must be a list (possibly empty)")
    else:
        if len(raw_pairs) > MAX_PAIRS:
            problems.append(
                f"at most {MAX_PAIRS} candidate pairs allowed, got "
                f"{len(raw_pairs)}")
        seen_oriented = set()
        seen_unordered = set()
        for pos in range(min(len(raw_pairs), MAX_PAIRS + 1)):
            pair = parse_pair(raw_pairs[pos], f"pairs[{pos}]")
            if pair is None:
                continue
            if pair in seen_oriented:
                problems.append(
                    f"pairs[{pos}] duplicates pair {list(pair)}")
                continue
            if pair[::-1] in seen_oriented or frozenset(pair) in seen_unordered:
                problems.append(
                    f"pairs[{pos}] {list(pair)} duplicates an earlier pair in "
                    "reverse order; the reversed query carries no new answers")
                continue
            seen_oriented.add(pair)
            seen_unordered.add(frozenset(pair))
            candidates.append(pair)

    if problems:
        raise PlanningError("invalid_input", problems)

    candidates.sort(key=lambda p: (p[0], p[1]))
    return target, candidates, plan_id


# ---------------------------------------------------------------------------
# adaptive tree search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Node:
    edges: tuple          # explicit edges active on the path (base + answers)


def _edges_key(edges):
    return tuple(sorted((u, v, w) for u, v, w, _m in edges))


def _solve_node(system, edges, dist, target, available, memo):
    """Optimal subtree for one feasible-set state.

    ``dist`` is the shortest-path closure of ``edges`` (already known
    feasible).  Returns (tree, worst_case_queries) or None when no adaptive
    ordering of the available candidate pairs can force a unique target
    relation.
    """
    key = (_edges_key(edges), frozenset(available))
    if key in memo:
        return memo[key]

    n = system["n"]
    relations = target_relations(system, dist, target)
    if len(relations) == 1:
        result = ({"kind": "leaf", "target_relation": relations[0]}, 0)
        memo[key] = result
        return result

    if not available:
        memo[key] = None
        return None

    best = None  # (worst, tie_key, tree)
    for pair in available:
        branch_specs = []
        for answer in ANSWER_ORDER:
            extra = _answer_edges(system, pair[0], pair[1], answer)
            if extra is None:
                continue  # e.g. equal tick with differing residues
            child_dist, child_feasible = _augment(n, dist, edges, extra)
            if not child_feasible:
                continue  # prune answers the frozen constraints rule out
            branch_specs.append((answer, tuple(list(edges) + extra),
                                 child_dist))

        if not branch_specs:
            continue  # every answer is impossible: the pair is uninformative

        remaining = [p for p in available if p != pair]  # re-asking adds nothing
        branches = []
        worst = 0
        failed = False
        for answer, child_edges, child_dist in branch_specs:
            sub = _solve_node(system, child_edges, child_dist, target,
                              remaining, memo)
            if sub is None:
                failed = True  # an answer no continuation can resolve
                break
            child_tree, child_worst = sub
            worst = max(worst, 1 + child_worst)
            branches.append({
                "answer": answer,
                "bounds": _branch_bounds(system, pair, answer),
                "then": child_tree,
            })
        if failed:
            continue

        tree = {
            "kind": "query",
            "pair": list(pair),
            "reachable_answers": [b["answer"] for b in branches],
            "pruned_answers": [a for a in ANSWER_ORDER
                               if a not in {b["answer"] for b in branches}],
            "branches": branches,
        }
        tie = _tree_sequence(tree)
        if best is None or (worst, tie) < (best[0], best[1]):
            best = (worst, tie, tree)

    if best is None:
        memo[key] = None
        return None
    result = (best[2], best[0])
    memo[key] = result
    return result


def _tree_sequence(tree):
    """Lexicographic key of candidate-pair identifiers across the tree.

    Pair ids are compared first; descendants follow in the fixed answer
    order (before, same, after).  Leaves contribute an empty sequence so a
    shallower subtree sorts first inside one branch.
    """
    if tree["kind"] == "leaf":
        return ()
    parts = [tuple(tree["pair"])]
    for branch in tree["branches"]:
        parts.append((branch["answer"], _tree_sequence(branch["then"])))
    return tuple(parts)


def _tree_stats(tree):
    if tree["kind"] == "leaf":
        return {"query_nodes": 0, "leaves": 1, "worst_case_queries": 0}
    leaves = 0
    queries = 1
    worst = 0
    for branch in tree["branches"]:
        sub = _tree_stats(branch["then"])
        queries += sub["query_nodes"]
        leaves += sub["leaves"]
        worst = max(worst, 1 + sub["worst_case_queries"])
    return {"query_nodes": queries, "leaves": leaves,
            "worst_case_queries": worst}


def _all_leaves_resolved(system, tree, edges, dist, target, out):
    """Sanity walk: every concrete answer path ends with a unique relation."""
    if tree["kind"] == "leaf":
        rels = target_relations(system, dist, target)
        out.append(rels == [tree["target_relation"]])
        return
    pair = tuple(tree["pair"])
    for branch in tree["branches"]:
        extra = _answer_edges(system, pair[0], pair[1], branch["answer"])
        child_dist, feasible = _augment(system["n"], dist, edges, extra)
        assert feasible
        _all_leaves_resolved(system, branch["then"],
                             tuple(list(edges) + extra), child_dist,
                             target, out)


# ---------------------------------------------------------------------------
# failure counterexample
# ---------------------------------------------------------------------------


def _timeline_view(system, wraps):
    counters = system["counters"]
    modulus = system["modulus"]
    anchor_id = system["anchor_id"]
    view = []
    for nid in system["node_ids"]:
        k = system["anchor_wrap"] if nid == anchor_id else wraps[system["idx"][nid]]
        view.append({
            "id": nid,
            "anchor": nid == anchor_id,
            "counter": counters[nid],
            "wrap": k,
            "absolute": counters[nid] + modulus * k,
        })
    return view


def _witness_for_relation(system, edges, target, relation):
    """A concrete feasible timeline (lex-smallest) with the target in
    ``relation``, or None."""
    extra = _answer_edges(system, target[0], target[1], relation)
    if extra is None:
        return None
    all_edges = list(edges) + extra
    _, feasible = _close(system["n"], all_edges)
    if not feasible:
        return None
    order = [system["idx"][e.id] for e in system["events"]]
    wraps = _lex_min_assignment(system["n"], all_edges, order, {})
    if wraps is None:
        return None
    return _timeline_view(system, wraps)


def _failure_witness(system, root_edges, target, candidates):
    """A recomputable answer path plus two timelines whose target relation
    differs, proving the offered pairs cannot decide the target.

    The walk only takes answers that keep at least two target relations
    feasible; each pair is used at most once.  When no answer even needs to be
    spent, the empty path already witnesses indistinguishability.
    """
    edges = list(root_edges)
    path = []
    unused = list(candidates)
    while True:
        dist, _ = _close(system["n"], edges)
        rels = target_relations(system, dist, target)
        extended = False
        for pos, pair in enumerate(unused):
            for answer in ANSWER_ORDER:
                extra = _answer_edges(system, pair[0], pair[1], answer)
                if extra is None:
                    continue
                trial_edges = edges + extra
                trial_dist, feasible = _close(system["n"], trial_edges)
                if not feasible:
                    continue
                if len(target_relations(system, trial_dist, target)) >= 2:
                    edges = trial_edges
                    path.append({
                        "pair": list(pair),
                        "answer": answer,
                        "bounds": _branch_bounds(system, pair, answer),
                    })
                    unused.pop(pos)
                    extended = True
                    break
            if extended:
                break
        if not extended:
            break

    dist, feasible = _close(system["n"], edges)
    assert feasible
    rels = target_relations(system, dist, target)
    assert len(rels) >= 2
    r1, r2 = rels[0], rels[1]
    t1 = _witness_for_relation(system, edges, target, r1)
    t2 = _witness_for_relation(system, edges, target, r2)
    if t1 is None or t2 is None:
        return None
    return {
        "answer_path": path,
        "target": list(target),
        "relations_still_possible": rels,
        f"timeline_{r1}": t1,
        f"timeline_{r2}": t2,
        "explanation": (
            "following every listed answer (each tightening the frozen wrap "
            "constraints by the shown integer bounds) leaves both target "
            f"relations {r1!r} and {r2!r} feasible; the two timelines "
            "recompute from exactly those constraints yet order "
            f"{target[0]} versus {target[1]} differently, so no adaptive plan "
            "using the offered pairs can distinguish them"),
    }


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_plan(audit_record, payload):
    """Plan against a stored audit record.  Raises PlanningError on rejection.

    Returns the JSON-able plan body; persistence is the store's responsibility.
    """
    if audit_record.get("status") != "ambiguous":
        raise PlanningError("source_not_ambiguous", [
            f"plan source must be an ambiguous audit; audit "
            f"{audit_record.get('audit_id')} is "
            f"{audit_record.get('status')}"])

    target, candidates, plan_id = normalize_plan_request(audit_record, payload)
    # The frozen input is the original raw payload; normalize once more to
    # obtain solver-ready structures (normalization is deterministic).
    system = build_system(normalize(audit_record["input"]))
    root_edges = tuple(system["edges"])

    dist, feasible = _close(system["n"], list(root_edges))
    if not feasible:  # defensive: the stored audit was solved successfully
        raise PlanningError("source_infeasible",
                            ["the frozen source constraints are not feasible"])

    root_relations = target_relations(system, dist, target)
    memo = {}
    solved = _solve_node(system, root_edges, dist, target, candidates, memo)

    body = {
        "plan_id": plan_id,
        "source_audit_id": audit_record["audit_id"],
        "source_status": "ambiguous",
        "target": list(target),
        "candidate_pairs": [list(p) for p in candidates],
        "target_relations_without_queries": root_relations,
        "optimality_rule": "minimize worst-case query count, then the "
                           "lexicographic candidate-pair identifier sequence",
    }

    if solved is None:
        witness = _failure_witness(system, list(root_edges), target, candidates)
        body.update({
            "status": "undecidable",
            "tree": None,
            "worst_case_queries": None,
            "counterexample": witness,
        })
        return body

    tree, worst = solved
    checks = []
    _all_leaves_resolved(system, tree, root_edges, dist, target, checks)
    if not checks or not all(checks):  # internal guarantee, never expected
        raise PlanningError("planner_fault",
                            [f"generated tree has an unresolved leaf ({len(checks)})"])
    body.update({
        "status": "decided",
        "tree": tree,
        "worst_case_queries": worst,
        "stats": _tree_stats(tree),
        "counterexample": None,
    })
    return body

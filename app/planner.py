"""Adaptive on-site query plans for ambiguous wrap-audit records.

When an audit concludes ``ambiguous`` the auditor may designate a **target
pair** of events whose precedence must be confirmed on site, together with at
most ten **candidate pairs** that can be put to the device.  Every query only
returns one of three answers relative to the submitted pair ``[left, right]``:

* ``before`` -- ``left`` strictly precedes ``right``;
* ``same``   -- both readings are the same absolute tick;
* ``after``  -- ``left`` strictly follows ``right``.

Each answer tightens the *frozen* integer wrap-count difference constraints by
one (``same`` by two) edges.  With ``d = counter(right) - counter(left)``::

    absolute(right) - absolute(left) = d + M * (wrap(right) - wrap(left))

    before:  wrap(right) - wrap(left) >= ceil((1 - d) / M)
    after:   wrap(right) - wrap(left) <= floor((-1 - d) / M)
    same:    wrap(right) - wrap(left) == -d / M   (only when M divides d)

A branch whose tightened system closes a negative cycle is an answer the
device can never give, so it is pruned; re-asking an already answered pair can
only repeat that answer (the other branches collapse) and is never useful.
A finite adaptive decision tree is built by minimax search over the at most
ten questions:

* a reachable state is a leaf when exactly one target relation is still
  feasible -- two canonical timelines or a single solution are never accepted
  as a guarantee while other relations remain realizable;
* the plan minimizes the worst-case number of questions, breaking ties by the
  lexicographically smallest candidate-pair identifier at every node;
* if no submitted strategy can resolve the target within ten questions, an
  indistinguishable response path is returned together with two complete
  timelines (both consistent with every answer on that path) that put the
  target pair in different relations.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .solver import (
    InputError,
    _ceil_div,
    _floyd_warshall,
    _lex_min_assignment,
    _timeline,
    build_model,
    normalize,
)

MAX_QUERIES = 10
ANSWERS = ("before", "same", "after")
ANSWER_CODE = {"before": 1, "same": 2, "after": 3}
INF = 10 ** 9


class SourceNotAmbiguousError(Exception):
    """Plans may only be attached to audits whose status is ambiguous."""

    def __init__(self, status):
        self.status = status
        super().__init__(f"source audit status is {status!r}, not 'ambiguous'")


@dataclass(frozen=True)
class Query:
    id: str
    left: str
    right: str


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _is_id(value):
    return isinstance(value, str) and bool(value.strip())


def validate_plan_payload(record, payload):
    """Validate a plan request against the frozen source audit.

    Returns (plan_id, target_pair, queries sorted by id).  Raises InputError
    with every problem found; invalid requests never bind the plan identifier.
    """
    if not isinstance(payload, dict):
        raise InputError(["payload must be a JSON object"])

    problems = []
    allowed = {"plan_id", "target", "queries"}
    for key in sorted(payload):
        if key not in allowed:
            problems.append(f"unknown field '{key}'")

    plan_id = payload.get("plan_id")
    if not _is_id(plan_id):
        problems.append("plan_id must be a non-empty string")

    source_input = record.get("input") or {}
    node_ids = {source_input.get("anchor", {}).get("id")}
    node_ids |= {e.get("id") for e in source_input.get("events", [])}
    node_ids.discard(None)

    def parse_pair(container, label):
        if not isinstance(container, list) or len(container) != 2:
            problems.append(f"{label} must be a pair [left, right]")
            return None
        left, right = container
        pair_ok = True
        if not _is_id(left):
            problems.append(f"{label} left endpoint must be a non-empty string")
            pair_ok = False
        elif left not in node_ids:
            problems.append(f"{label} event '{left}' does not exist in the source audit")
            pair_ok = False
        if not _is_id(right):
            problems.append(f"{label} right endpoint must be a non-empty string")
            pair_ok = False
        elif right not in node_ids:
            problems.append(f"{label} event '{right}' does not exist in the source audit")
            pair_ok = False
        if pair_ok and left == right:
            problems.append(f"{label} endpoints must be two distinct events")
            pair_ok = False
        return (left, right) if pair_ok else None

    target = None
    raw_target = payload.get("target")
    if raw_target is None:
        problems.append("target must be a pair [left, right]")
    else:
        target = parse_pair(raw_target, "target")

    queries = []
    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list):
        problems.append("queries must be a list (possibly empty)")
    elif len(raw_queries) > MAX_QUERIES:
        problems.append(
            f"at most {MAX_QUERIES} queries allowed, got {len(raw_queries)}")
    else:
        seen_ids = set()
        seen_pairs = set()
        for pos, item in enumerate(raw_queries):
            if not isinstance(item, dict):
                problems.append(f"queries[{pos}] must be an object {{id, left, right}}")
                continue
            for key in sorted(item):
                if key not in {"id", "left", "right"}:
                    problems.append(f"unknown queries[{pos}] field '{key}'")
            qid = item.get("id")
            if not _is_id(qid):
                problems.append(f"queries[{pos}].id must be a non-empty string")
                continue
            if qid in seen_ids:
                problems.append(f"duplicate query id '{qid}'")
                continue
            endpoints = parse_pair(
                [item.get("left"), item.get("right")], f"query '{qid}'")
            if endpoints is None:
                continue
            unordered = frozenset(endpoints)
            if unordered in seen_pairs:
                problems.append(
                    f"query '{qid}' repeats an already submitted event pair")
                continue
            seen_ids.add(qid)
            seen_pairs.add(unordered)
            queries.append(Query(qid, endpoints[0], endpoints[1]))

    if problems:
        raise InputError(problems)

    queries.sort(key=lambda q: q.id)
    return plan_id, target, queries


# ---------------------------------------------------------------------------
# answers as wrap-count difference edges
# ---------------------------------------------------------------------------

def _answer_edges(model, endpoints, answer):
    """Shortest-path edges (u, v, w, relation) imposed by an answer.

    Returns None if the answer is arithmetically impossible for this pair
    (e.g. equal ticks when the two counter residues differ).
    """
    idx, labels, counters, modulus = (
        model["idx"], model["labels"], model["counters"], model["modulus"])
    i, j = idx[endpoints[0]], idx[endpoints[1]]
    li, lj = labels[i], labels[j]
    d = counters[lj] - counters[li]
    if answer == "before":  # t_i < t_j: d + M*(k_j-k_i) >= 1
        bound = _ceil_div(1 - d, modulus)
        return [(j, i, -bound,
                 f"wrap({lj}) - wrap({li}) >= {bound}")]
    if answer == "after":  # t_i > t_j: d + M*(k_j-k_i) <= -1
        bound = (-1 - d) // modulus
        return [(i, j, bound,
                 f"wrap({lj}) - wrap({li}) <= {bound}")]
    # same tick: d + M*(k_j-k_i) == 0
    if (-d) % modulus != 0:
        return None
    q = (-d) // modulus
    return [
        (i, j, q, f"wrap({lj}) - wrap({li}) <= {q}"),
        (j, i, -q, f"wrap({lj}) - wrap({li}) >= {q}"),
    ]


def _insert_edge(dist, u, v, w):
    """Re-close an all-pairs shortest-path matrix after adding edge u->v.

    Returns False iff the new edge closes a negative cycle.  G was feasible,
    so a negative cycle in G+e must use e and has weight ``w + d(v,u)``;
    every new simple shortest path uses e at most once and is captured by the
    one-pass relaxation ``i -> u -> v -> j``.
    """
    if dist[v][u] is not None and w + dist[v][u] < 0:
        return False
    if dist[u][v] is not None and w >= dist[u][v]:
        return True
    n = len(dist)
    column_u = [dist[i][u] for i in range(n)]
    row_v = [dist[v][j] for j in range(n)]
    for i in range(n):
        diu = column_u[i]
        if diu is None:
            continue
        row = dist[i]
        base = diu + w
        for j in range(n):
            dvj = row_v[j]
            if dvj is None:
                continue
            cand = base + dvj
            if row[j] is None or cand < row[j]:
                row[j] = cand
    return True


def _tighten(base_dist, edges):
    """Return a copy of the closure tightened by edges, or None if infeasible."""
    dist = [row[:] for row in base_dist]
    for u, v, w, _relation in edges:
        if not _insert_edge(dist, u, v, w):
            return None
    return dist


def _possible_relations(model, dist, endpoints):
    """Target relations still realizable inside the tightened closure."""
    idx, labels, counters, modulus = (
        model["idx"], model["labels"], model["counters"], model["modulus"])
    i, j = idx[endpoints[0]], idx[endpoints[1]]
    li, lj = labels[i], labels[j]
    d = counters[lj] - counters[li]
    dk_min = -dist[j][i]      # k_j - k_i lower bound
    dk_max = dist[i][j]
    delta_min = d + modulus * dk_min
    delta_max = d + modulus * dk_max
    possible = []
    if delta_max >= 1:                    # left strictly before right
        possible.append("before")
    if (-d) % modulus == 0:
        q = (-d) // modulus
        if dk_min <= q <= dk_max:
            possible.append("same")
    if delta_min <= -1:                   # left strictly after right
        possible.append("after")
    return possible


# ---------------------------------------------------------------------------
# minimax adaptive planning
# ---------------------------------------------------------------------------

def _leaf_node(relation):
    return {"type": "leaf", "target_relation": relation}


def _open_node(relations):
    return {"type": "open", "possible_relations": relations}


def build_plan(record, payload):
    """Validate and solve a plan request against a frozen audit record."""
    if record.get("status") != "ambiguous":
        raise SourceNotAmbiguousError(record.get("status"))
    plan_id, target, queries = validate_plan_payload(record, payload)

    norm = normalize(record["input"])
    model = build_model(norm)
    root_dist = _floyd_warshall(model["n"], model["edges"])
    nq = len(queries)
    root_key = (0,) * nq
    memo = {}  # answer-tuple -> (worst-case cost, node); states are shared

    def rec(key, depth, dist):
        """Return (worst-case extra questions, node) for a feasible state."""
        cached = memo.get(key)
        if cached is not None:
            return cached
        relations = _possible_relations(model, dist, target)
        if len(relations) == 1:
            result = (0, _leaf_node(relations[0]))
            memo[key] = result
            return result
        if depth >= MAX_QUERIES:
            result = (INF, _open_node(relations))
            memo[key] = result
            return result

        best = None  # (worst-case cost, query index, node)
        for qi, q in enumerate(queries):
            if key[qi] != 0:
                continue  # already answered: asking again gains nothing
            branches = {}
            reachable = []
            impossible = []
            worst = 0
            pruned = False
            for answer in ANSWERS:
                edges = _answer_edges(model, (q.left, q.right), answer)
                if edges is None:
                    branches[answer] = None
                    impossible.append(answer)
                    continue
                child_dist = _tighten(dist, edges)
                if child_dist is None:
                    branches[answer] = None
                    impossible.append(answer)
                    continue
                child_key = list(key)
                child_key[qi] = ANSWER_CODE[answer]
                child_key = tuple(child_key)
                cost, child = rec(child_key, depth + 1, child_dist)
                branches[answer] = child
                reachable.append(answer)
                if cost > worst:
                    worst = cost
                # Branch-and-bound against the incumbent (id order breaks ties).
                if best is not None:
                    candidate_cost = 1 + worst
                    if candidate_cost > best[0] or (
                            candidate_cost == best[0]
                            and q.id > queries[best[1]].id):
                        pruned = True
                        break
            if pruned:
                continue
            if not reachable:
                continue  # defensive: a pair with no reachable answer is dead
            node = {
                "type": "internal",
                "ask": q.id,
                "pair": [q.left, q.right],
                "depth": depth,
                "reachable_answers": reachable,
                "impossible_answers": impossible,
                "answers": branches,
            }
            cand = (1 + worst if worst < INF else INF, qi, node)
            if best is None or (cand[0], cand[1]) < (best[0], best[1]):
                best = cand
        if best is None:  # no unused informative pair remains
            result = (INF, _open_node(relations))
        else:
            result = (best[0], best[2])
        memo[key] = result
        return result

    worst_case, raw_tree = rec(root_key, 0, root_dist)

    result = {
        "plan_id": plan_id,
        "target": list(target),
        "queries": [
            {"id": q.id, "left": q.left, "right": q.right} for q in queries
        ],
        "worst_case_questions": worst_case if worst_case < INF else None,
    }

    if worst_case < INF:
        result["status"] = "resolved"
        result["tree"] = _render_tree(raw_tree, (), model, queries)
        result["counterexample"] = None
        return result

    trace, key = _failure_trace(
        model, queries, target, root_key, root_dist, rec)
    counterexample = _counterexample(model, queries, key, target, trace)
    result["status"] = "indistinguishable"
    result["tree"] = None
    result["counterexample"] = counterexample
    return result


def _answer_step(model, queries, qi, answer):
    q = queries[qi]
    edges = _answer_edges(model, (q.left, q.right), answer)
    return {
        "query": q.id,
        "pair": [q.left, q.right],
        "answer": answer,
        "imposed_bounds": [relation for _u, _v, _w, relation in edges],
    }


def _render_tree(node, path, model, queries):
    """Project the memoized tree, annotating each leaf with its concrete path."""
    if node["type"] == "leaf":
        return {
            "type": "leaf",
            "target_relation": node["target_relation"],
            "answer_path": [
                _answer_step(model, queries, qi, answer) for qi, answer in path],
        }
    if node["type"] == "open":
        return {
            "type": "open",
            "possible_relations": node["possible_relations"],
            "answer_path": [
                _answer_step(model, queries, qi, answer) for qi, answer in path],
        }
    rendered_answers = {}
    for answer, child in node["answers"].items():
        if child is None:
            rendered_answers[answer] = None
        else:
            qi = next(i for i, q in enumerate(queries) if q.id == node["ask"])
            rendered_answers[answer] = _render_tree(
                child, path + ((qi, answer),), model, queries)
    return {
        "type": "internal",
        "ask": node["ask"],
        "pair": node["pair"],
        "depth": node["depth"],
        "reachable_answers": node["reachable_answers"],
        "impossible_answers": node["impossible_answers"],
        "answers": rendered_answers,
    }


def _failure_trace(model, queries, target, root_key, root_dist, rec):
    """Follow the canonical policy to a reachable open leaf (smallest ids,
    first answer that stays indistinguishable)."""
    key, depth = root_key, 0
    dist = root_dist
    trace = []
    while depth < MAX_QUERIES:
        relations = _possible_relations(model, dist, target)
        if len(relations) == 1:
            break
        chosen = None
        for qi, q in enumerate(queries):
            if key[qi] != 0:
                continue
            for answer in ANSWERS:
                edges = _answer_edges(model, (q.left, q.right), answer)
                if edges is None:
                    continue
                child_dist = _tighten(dist, edges)
                if child_dist is None:
                    continue
                child_key = list(key)
                child_key[qi] = ANSWER_CODE[answer]
                child_key = tuple(child_key)
                cost, _node = rec(child_key, depth + 1, child_dist)
                if cost >= INF:
                    chosen = (qi, answer, child_key, child_dist)
                    break
            if chosen is not None:
                break
        if chosen is None:
            break
        qi, answer, key, dist = chosen
        trace.append(_answer_step(model, queries, qi, answer))
        depth += 1
    return trace, key


# ---------------------------------------------------------------------------
# indistinguishable-case witness
# ---------------------------------------------------------------------------

def _relation_edges(model, endpoints, relation):
    """Edges forcing the target pair into a given relation (witness search)."""
    idx, labels, counters, modulus = (
        model["idx"], model["labels"], model["counters"], model["modulus"])
    i, j = idx[endpoints[0]], idx[endpoints[1]]
    li, lj = labels[i], labels[j]
    d = counters[lj] - counters[li]
    if relation == "before":  # t_i < t_j
        bound = _ceil_div(1 - d, modulus)
        return [(j, i, -bound, {"kind": "witness"})]
    if relation == "after":  # t_i > t_j
        bound = (-1 - d) // modulus
        return [(i, j, bound, {"kind": "witness"})]
    q = (-d) // modulus
    return [(i, j, q, {"kind": "witness"}),
            (j, i, -q, {"kind": "witness"})]


def _assignment_to_wraps(model, fixed):
    wraps = {model["anchor_id"]: model["anchor_wrap"]}
    for node_index, value in fixed.items():
        wraps[model["labels"][node_index]] = value
    return wraps


def _relation_in_timeline(model, endpoints, wraps):
    li, lj = endpoints
    ti = counters_abs(model, li, wraps)
    tj = counters_abs(model, lj, wraps)
    if ti < tj:
        return "before"
    if ti > tj:
        return "after"
    return "same"


def counters_abs(model, node_id, wraps):
    return model["counters"][node_id] + model["modulus"] * wraps[node_id]


def _counterexample(model, queries, key, target, trace):
    order = list(range(1, model["n"]))  # event nodes, identifier order
    code_answer = {1: "before", 2: "same", 3: "after"}
    state_edges = list(model["edges"])
    for qi, code in enumerate(key):
        if code == 0:
            continue
        for u, v, w, relation in _answer_edges(
                model, (queries[qi].left, queries[qi].right), code_answer[code]):
            state_edges.append((u, v, w, {"kind": "witness", "relation": relation}))

    first_fixed = _lex_min_assignment(model["n"], state_edges, order, {})
    if first_fixed is None:
        raise RuntimeError("counterexample leaf is infeasible")
    wraps1 = _assignment_to_wraps(model, first_fixed)
    relation1 = _relation_in_timeline(model, target, wraps1)

    leaf_relations = _possible_relations(
        model, _floyd_warshall(model["n"], state_edges), target)
    wraps2 = None
    relation2 = None
    for candidate in ANSWERS:
        if candidate == relation1 or candidate not in leaf_relations:
            continue
        if candidate == "same":
            li, lj = target
            d = model["counters"][lj] - model["counters"][li]
            if (-d) % model["modulus"] != 0:
                continue
        forced = state_edges + _relation_edges(model, target, candidate)
        fixed2 = _lex_min_assignment(model["n"], forced, order, {})
        if fixed2 is None:
            continue
        candidate_wraps = _assignment_to_wraps(model, fixed2)
        if _relation_in_timeline(model, target, candidate_wraps) == candidate:
            wraps2 = candidate_wraps
            relation2 = candidate
            break
    if wraps2 is None:
        raise RuntimeError("could not extract a second timeline for the witness")

    timeline1 = _timeline(
        model["node_ids"], model["counters"], model["modulus"],
        model["anchor_id"], wraps1)
    timeline2 = _timeline(
        model["node_ids"], model["counters"], model["modulus"],
        model["anchor_id"], wraps2)

    # Recomputable check: both timelines must give exactly every logged answer.
    replay = []
    for step in trace:
        endpoints = step["pair"]
        replay.append({
            "query": step["query"],
            "pair": endpoints,
            "expected_answer": step["answer"],
            "timeline_1_answer": _relation_in_timeline(model, endpoints, wraps1),
            "timeline_2_answer": _relation_in_timeline(model, endpoints, wraps2),
        })

    return {
        "answer_path": trace,
        "possible_relations_at_leaf": leaf_relations,
        "timeline_1": {
            "target_relation": relation1,
            "events": timeline1,
        },
        "timeline_2": {
            "target_relation": relation2,
            "events": timeline2,
        },
        "answer_replay": replay,
        "explanation": (
            "every answer on this path is consistent with both timelines, yet "
            f"timeline 1 puts {target[0]} {relation1} {target[1]} while "
            f"timeline 2 puts it {relation2}; no adaptive strategy using the "
            "submitted pairs can separate them within the question budget"),
    }


def plan_fingerprint(audit_id, target, queries):
    """Stable fingerprint covering the source and the whole plan content."""
    import hashlib
    import json

    def qid(q):
        return q.id if isinstance(q, Query) else q["id"]

    def qfield(q, name):
        return getattr(q, name) if isinstance(q, Query) else q[name]

    body = {
        "audit_id": audit_id,
        "target": list(target),
        "queries": sorted(
            ({"id": qid(q), "left": qfield(q, "left"),
              "right": qfield(q, "right")} for q in queries),
            key=lambda q: q["id"]),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def frozen_plan_input(payload):
    """Deep copy of the raw plan request for frozen storage."""
    return copy.deepcopy(payload)

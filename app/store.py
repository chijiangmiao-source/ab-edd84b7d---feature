"""Idempotent, in-memory audit and interrogation-plan store.

Creation is keyed by a client-supplied identifier: replaying the same
identifier with the same payload returns the original record, while the same
identifier with any semantic content changed is rejected and adds no record.
Every stored record freezes its input, conclusion and evidence for later
reads; a stored plan additionally snapshots its source audit so the frozen
evidence can be reviewed together with it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone

from .planner import PlanningError, build_plan
from .solver import normalize, solve


class ConflictError(Exception):
    """Identifier was already used with a different payload."""

    def __init__(self, existing_id, kind="request_id"):
        self.existing_id = existing_id
        self.kind = kind
        # Back-compat: audit callers historically read .audit_id.
        self.audit_id = existing_id if kind == "request_id" else None
        super().__init__(f"{kind} already bound to {existing_id}")


def canonical_form(payload):
    """Order-insensitive rendering of the semantic payload for fingerprinting."""
    body = {
        "modulus": payload.get("modulus"),
        "anchor": payload.get("anchor"),
        "events": sorted(payload.get("events") or [], key=lambda e: e.get("id", "")),
        "constraints": sorted(
            payload.get("constraints") or [], key=lambda c: c.get("id", "")),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def plan_canonical_form(audit_id, payload):
    """Order-insensitive rendering of a plan request for fingerprinting."""
    body = {
        "source_audit_id": audit_id,
        "target": payload.get("target"),
        "pairs": sorted(
            (list(p) for p in (payload.get("pairs") or [])),
            key=lambda p: (p[0], p[1])),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


class AuditStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_request = {}    # request_id -> (fingerprint, audit_id)
        self._audits = {}        # audit_id -> frozen record
        self._order = []         # audit ids in creation order
        self._plans_by_id = {}   # plan_id -> (fingerprint, plan record id)
        self._plans = {}         # plan record id -> frozen plan record
        self._plan_order = []    # plan record ids in creation order
        self._seq = 0
        self._plan_seq = 0

    # -- audits ------------------------------------------------------------

    def create(self, payload):
        """Return (record, created). Raises InputError or ConflictError."""
        norm = normalize(payload)  # invalid payloads never claim a request_id
        fingerprint = hashlib.sha256(
            canonical_form(payload).encode("utf-8")).hexdigest()
        with self._lock:
            prior = self._by_request.get(norm["request_id"])
            if prior is not None:
                if prior[0] == fingerprint:
                    return self._audits[prior[1]], False
                raise ConflictError(prior[1])
            result = solve(norm)
            self._seq += 1
            audit_id = f"AUD-{self._seq:06d}"
            record = {
                "audit_id": audit_id,
                "request_id": norm["request_id"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "input": copy.deepcopy(payload),
                "status": result["status"],
                "conclusion": result["conclusion"],
                "evidence": result["evidence"],
            }
            self._by_request[norm["request_id"]] = (fingerprint, audit_id)
            self._audits[audit_id] = record
            self._order.append(audit_id)
            return record, True

    def get(self, audit_id):
        return self._audits.get(audit_id)

    def ids(self):
        return list(self._order)

    def count(self):
        return len(self._order)

    # -- interrogation plans ----------------------------------------------

    def create_plan(self, audit_id, payload):
        """Return (plan_record, created).

        Raises KeyError when the audit does not exist, PlanningError for a
        source that is not ambiguous or an invalid plan payload, and
        ConflictError when plan_id is already bound to different content.
        Nothing is persisted on any rejection.
        """
        record = self._audits.get(audit_id)
        if record is None:
            raise KeyError(audit_id)
        # Fully validate and build before taking the lock or claiming the id.
        body = build_plan(record, payload)
        plan_id = body["plan_id"]
        fingerprint = hashlib.sha256(
            plan_canonical_form(audit_id, payload).encode("utf-8")).hexdigest()
        with self._lock:
            prior = self._plans_by_id.get(plan_id)
            if prior is not None:
                if prior[0] == fingerprint:
                    return self._plans[prior[1]], False
                raise ConflictError(prior[1], kind="plan_id")
            self._plan_seq += 1
            plan_record_id = f"PLN-{self._plan_seq:06d}"
            stored = {
                "plan_record_id": plan_record_id,
                "plan_id": plan_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_audit_id": audit_id,
                "input": copy.deepcopy(payload),
                "status": body["status"],
                "worst_case_queries": body["worst_case_queries"],
                "tree": body["tree"],
                "counterexample": body["counterexample"],
                "stats": body.get("stats"),
                "candidate_pairs": body["candidate_pairs"],
                "target": body["target"],
                "optimality_rule": body["optimality_rule"],
                # Frozen source evidence travels with the plan read.
                "source_audit": {
                    "audit_id": record["audit_id"],
                    "request_id": record["request_id"],
                    "status": record["status"],
                    "input": copy.deepcopy(record["input"]),
                    "conclusion": copy.deepcopy(record["conclusion"]),
                    "evidence": copy.deepcopy(record["evidence"]),
                },
            }
            self._plans_by_id[plan_id] = (fingerprint, plan_record_id)
            self._plans[plan_record_id] = stored
            self._plan_order.append(plan_record_id)
            return stored, True

    def get_plan(self, plan_record_id=None, *, plan_id=None):
        if plan_record_id is not None:
            return self._plans.get(plan_record_id)
        if plan_id is not None:
            hit = self._plans_by_id.get(plan_id)
            return self._plans[hit[1]] if hit else None
        return None

    def plan_ids(self, audit_id=None):
        if audit_id is None:
            return list(self._plan_order)
        return [pid for pid in self._plan_order
                if self._plans[pid]["source_audit_id"] == audit_id]

    def plan_count(self):
        return len(self._plan_order)

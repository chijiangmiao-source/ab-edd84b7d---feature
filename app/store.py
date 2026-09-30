"""Idempotent, in-memory audit store.

Creation is keyed by ``request_id``: replaying the same identifier with the
same payload returns the original audit record, while the same identifier
with any event or constraint changed is rejected and adds no record.  Every
stored record freezes the input, conclusion and evidence for later reads.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone

from .solver import normalize, solve
from .planner import build_plan, frozen_plan_input, plan_fingerprint


class ConflictError(Exception):
    """request_id was already used with a different payload."""

    def __init__(self, audit_id):
        self.audit_id = audit_id
        super().__init__(f"request_id already bound to {audit_id}")


class PlanConflictError(Exception):
    """plan_id was already used with different content (or another audit)."""

    def __init__(self, plan_ref):
        self.plan_ref = plan_ref
        super().__init__(f"plan_id already bound to {plan_ref}")


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


class AuditStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_request = {}  # request_id -> (fingerprint, audit_id)
        self._audits = {}      # audit_id -> frozen record
        self._order = []       # audit ids in creation order
        self._seq = 0
        self._plans_by_id = {}   # plan_id -> (fingerprint, plan_ref)
        self._plans = {}         # plan_ref -> frozen plan record
        self._plans_order = []   # plan refs in creation order
        self._plan_seq = 0

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

    # ------------------------------------------------------------------
    # adaptive query plans
    # ------------------------------------------------------------------

    def create_plan(self, audit_id, payload):
        """Create a frozen adaptive plan under an audit.

        Returns (record, created).  Raises KeyError for unknown audits,
        SourceNotAmbiguousError for non-ambiguous sources, InputError for bad
        payloads and PlanConflictError when plan_id changed content.  Nothing
        is written on any failure.
        """
        record = self._audits.get(audit_id)
        if record is None:
            raise KeyError(audit_id)
        # Pure validation/solving happens before any state is touched.
        result = build_plan(record, payload)
        plan_id = result["plan_id"]
        fingerprint = plan_fingerprint(audit_id, result["target"],
                                       result["queries"])
        with self._lock:
            prior = self._plans_by_id.get(plan_id)
            if prior is not None:
                if prior[0] == fingerprint:
                    return self._plans[prior[1]], False
                raise PlanConflictError(prior[1])
            self._plan_seq += 1
            plan_ref = f"PLN-{self._plan_seq:06d}"
            plan_record = {
                "plan_ref": plan_ref,
                "plan_id": plan_id,
                "audit_id": audit_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "input": frozen_plan_input(payload),
                "status": result["status"],
                "target": result["target"],
                "queries": result["queries"],
                "worst_case_questions": result["worst_case_questions"],
                "tree": result["tree"],
                "counterexample": result["counterexample"],
            }
            self._plans_by_id[plan_id] = (fingerprint, plan_ref)
            self._plans[plan_ref] = plan_record
            self._plans_order.append(plan_ref)
            return plan_record, True

    def get_plan(self, plan_ref):
        return self._plans.get(plan_ref)

    def plan_refs(self):
        return list(self._plans_order)

    def plan_refs_for(self, audit_id):
        return [ref for ref in self._plans_order
                if self._plans[ref]["audit_id"] == audit_id]

    def plan_count(self):
        return len(self._plans_order)

    def plan_with_source(self, plan_ref):
        """Return the frozen plan together with its frozen source audit."""
        plan_record = self._plans.get(plan_ref)
        if plan_record is None:
            return None
        source = copy.deepcopy(self._audits[plan_record["audit_id"]])
        return {"plan": copy.deepcopy(plan_record), "source_audit": source}

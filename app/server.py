"""HTTP API for the deep-space timestamp audit service (stdlib only).

Endpoints:
    GET    /health                 liveness probe
    POST   /audits                 create an audit (idempotent on request_id)
    GET    /audits                 list audit ids
    GET    /audits/{id}            read the frozen input, conclusion and evidence
    POST   /audits/{id}/plans      create an adaptive interrogation plan
    GET    /audits/{id}/plans      list plans derived from one audit
    GET    /plans/{plan_record_id} read a frozen plan together with its source
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .planner import PlanningError
from .solver import InputError
from .store import AuditStore, ConflictError

_AUDIT_PATH = re.compile(r"^/audits/([A-Za-z0-9][A-Za-z0-9_-]*)$")
_AUDIT_PLANS_PATH = re.compile(
    r"^/audits/([A-Za-z0-9][A-Za-z0-9_-]*)/plans$")
_PLAN_PATH = re.compile(r"^/plans/([A-Za-z0-9][A-Za-z0-9_-]*)$")


class _BadBody(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSpaceAudit/1.0"
    store = AuditStore()

    # -- helpers -----------------------------------------------------------

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise _BadBody()
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _BadBody()

    def log_message(self, fmt, *args):  # keep container logs free of probe spam
        pass

    # -- reads -------------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            return self._send_json(200, {"status": "ok", "service": "deep-space-audit"})
        if path == "/audits":
            return self._send_json(200, {
                "count": self.store.count(),
                "audits": self.store.ids(),
            })
        plans_match = _AUDIT_PLANS_PATH.match(path)
        if plans_match:
            audit_id = plans_match.group(1)
            if self.store.get(audit_id) is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no audit {audit_id}",
                })
            return self._send_json(200, {
                "audit_id": audit_id,
                "count": len(self.store.plan_ids(audit_id)),
                "plans": self.store.plan_ids(audit_id),
            })
        plan_match = _PLAN_PATH.match(path)
        if plan_match:
            record = self.store.get_plan(plan_match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no plan {plan_match.group(1)}",
                })
            return self._send_json(200, record)
        match = _AUDIT_PATH.match(path)
        if match:
            record = self.store.get(match.group(1))
            if record is None:
                return self._send_json(404, {
                    "error": "not_found",
                    "message": f"no audit {match.group(1)}",
                })
            return self._send_json(200, record)
        return self._send_json(404, {"error": "not_found"})

    # -- writes ------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        plans_match = _AUDIT_PLANS_PATH.match(path)
        if plans_match:
            return self._create_plan(plans_match.group(1))
        if path != "/audits":
            return self._send_json(404, {"error": "not_found"})
        try:
            payload = self._read_json()
        except _BadBody:
            return self._send_json(400, {
                "error": "bad_json",
                "message": "request body is not valid JSON",
            })
        try:
            record, created = self.store.create(payload)
        except InputError as exc:
            return self._send_json(400, {
                "error": "invalid_input",
                "problems": exc.problems,
            })
        except ConflictError as exc:
            return self._send_json(409, {
                "error": "request_id_conflict",
                "message": "request_id was already used with a different payload; "
                           "no record was added",
                "existing_audit_id": exc.existing_id,
            })
        body = dict(record)
        body["replayed"] = not created
        return self._send_json(201 if created else 200, body)

    def _create_plan(self, audit_id):
        if self.store.get(audit_id) is None:
            return self._send_json(404, {
                "error": "not_found",
                "message": f"no audit {audit_id}",
            })
        try:
            payload = self._read_json()
        except _BadBody:
            return self._send_json(400, {
                "error": "bad_json",
                "message": "request body is not valid JSON",
            })
        try:
            record, created = self.store.create_plan(audit_id, payload)
        except PlanningError as exc:
            code = 409 if exc.code == "source_not_ambiguous" else 400
            return self._send_json(code, {
                "error": exc.code,
                "problems": exc.problems,
            })
        except ConflictError as exc:
            return self._send_json(409, {
                "error": "plan_id_conflict",
                "message": "plan_id was already used with different content; "
                           "no plan was added",
                "existing_plan_record_id": exc.existing_id,
            })
        body = dict(record)
        body["replayed"] = not created
        return self._send_json(201 if created else 200, body)


def make_server(host, port, store=None):
    handler = Handler
    if store is not None:
        class _BoundHandler(Handler):
            pass
        _BoundHandler.store = store
        handler = _BoundHandler
    return ThreadingHTTPServer((host, port), handler)


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = make_server("0.0.0.0", port)
    print(f"deep-space-audit listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()

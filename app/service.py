"""HTTP service for the shared-forest grammar arbiter.

Endpoints
---------
``GET  /healthz``                  liveness probe
``POST /api/v1/analyze``           submit/replay a sealed determination
``GET  /api/v1/conclusion/<id>``   fetch a sealed conclusion by audit id
``GET  /api/v1/witness/<id>``      ambiguity witness for a sealed
                                   conclusion: the shared structural
                                   prefix and the first divergence of the
                                   two sealed trees (re-derived from the
                                   sealed trees only; unique/rejected
                                   conclusions explicitly have none)

Analysis outcomes (HTTP 200) all carry a ``verdict``:

* ``REJECTED``           -- with a machine ``reason`` and an actionable
                            Chinese ``detail`` (covers input not
                            accepted, unproductive start symbol and
                            reachable non-consuming cycles);
* ``UNIQUE_ACCEPTED``    -- the single derivation tree;
* ``AMBIGUOUS_ACCEPTED`` -- two stable derivation trees.

Malformed requests answer HTTP 400 with the first actionable reason;
reusing an audit id with different semantics answers HTTP 409 and keeps
the original evidence.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

from . import engine
from .grammar import MAX_NONTERMINALS, MAX_PRODUCTIONS, MAX_TOKENS, ValidationError, build_grammar
from .storage import ConflictError, SealedStore, canonical_fingerprint

STORE_PATH = os.environ.get("ARBITER_STORE", "/data/sealed.json")

# Maximum accepted request body (generous for the protocol limits).
MAX_BODY = 1 << 20

PROTOCOL = {
    "max_nonterminals": MAX_NONTERMINALS,
    "max_productions": MAX_PRODUCTIONS,
    "max_input_tokens": MAX_TOKENS,
    "allows_empty_rhs": True,
    "allows_left_recursion": True,
}


def _compute(payload: dict) -> dict:
    """Validate then analyze; always returns a JSON-serializable result."""
    grammar = build_grammar(payload)
    try:
        outcome = engine.analyze(grammar)
    except engine.EngineError as exc:
        return {
            "verdict": engine.REJECTED,
            "rejection": {
                "reason": exc.reason,
                "detail": exc.detail,
                **({"evidence": exc.extra} if exc.extra else {}),
            },
        }
    return outcome


def _loose_fingerprint(raw_body: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw_body).hexdigest()


def _witness_view(audit_id: str, entry: dict) -> Tuple[int, dict]:
    """Build the ambiguity-witness response for a sealed entry.

    The witness is re-derived exclusively from the two finite trees
    already sealed with the conclusion -- never from a fresh parse.  A
    unique acceptance or a rejection has no ambiguity witness and says
    so explicitly.
    """
    conclusion = entry["conclusion"]
    verdict = conclusion.get("verdict")

    base = {
        "audit_id": audit_id,
        "request_hash": entry["request_hash"],
        "sealed_at": entry["sealed_at"],
        "verdict": verdict,
        "witness_available": False,
    }

    if verdict != engine.AMBIGUOUS:
        reason_map = {
            engine.UNIQUE: "唯一接受结论只封存了一棵派生树，不存在两棵树之间的歧义见证",
            engine.REJECTED: "拒绝结论没有任何接受派生树，不存在歧义见证",
        }
        base["detail"] = reason_map.get(
            verdict, f"结论 {verdict!r} 不封存两棵派生树，不存在歧义见证"
        )
        return 200, base

    trees = conclusion.get("trees") or {}
    first_tree, second_tree = trees.get("first"), trees.get("second")
    if not isinstance(first_tree, dict) or not isinstance(second_tree, dict):
        # Sealed evidence is internally inconsistent: report, never forge.
        base["detail"] = "封存结论缺少两棵派生树，无法重新导出歧义见证"
        return 200, base

    try:
        witness = engine.ambiguity_witness(first_tree, second_tree)
    except ValueError as exc:
        base["detail"] = f"无法从封存树重新导出歧义见证：{exc}"
        return 200, base

    base["witness_available"] = True
    base["production_sequences"] = conclusion.get("production_sequences")
    base["witness"] = witness
    return 200, base


class ArbiterHandler(BaseHTTPRequestHandler):
    server_version = "ForestArbiter/1.0"

    # Injected by make_server.
    store: SealedStore

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet, structured stderr
        import sys

        sys.stderr.write("[http] %s\n" % (fmt % args))

    def _write_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._write_json(200, {"status": "ok", "service": "forest-arbiter"})
            return
        if path == "/api/v1/protocol":
            self._write_json(200, PROTOCOL)
            return
        prefix = "/api/v1/conclusion/"
        witness_prefix = "/api/v1/witness/"
        if path.startswith(prefix) or path.startswith(witness_prefix):
            is_witness = path.startswith(witness_prefix)
            tail = path[len(witness_prefix) if is_witness else len(prefix):]
            audit_id = unquote(tail)
            if not audit_id or "/" in audit_id:
                self._write_json(400, {"error": "BAD_AUDIT_ID",
                                       "detail": "审计标识缺失或非法"})
                return
            entry = self.store.get(audit_id)
            if entry is None:
                # Unknown ids keep the existing read semantics of the
                # conclusion flow: 404, whether or not a witness was asked.
                self._write_json(404, {"error": "NOT_FOUND",
                                       "detail": f"未找到审计标识 {audit_id!r} 的封存结论"})
                return
            if is_witness:
                status, body = _witness_view(audit_id, entry)
                self._write_json(status, body)
                return
            self._write_json(200, {"replayed": True, **entry})
            return
        self._write_json(404, {"error": "NOT_FOUND", "detail": f"未知路径 {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/api/v1/analyze":
            self._write_json(404, {"error": "NOT_FOUND", "detail": f"未知路径 {path}"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json(400, {"error": "BAD_REQUEST", "detail": "Content-Length 非法"})
            return
        if length <= 0:
            self._write_json(400, {"error": "BAD_REQUEST", "detail": "请求体为空"})
            return
        if length > MAX_BODY:
            self._write_json(413, {"error": "BODY_TOO_LARGE",
                                   "detail": f"请求体超过 {MAX_BODY} 字节上限"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._write_json(400, {"error": "INVALID_JSON",
                                   "detail": f"请求体不是合法 UTF-8 JSON：{exc}"})
            return
        if not isinstance(payload, dict):
            self._write_json(400, {"error": "INVALID_REQUEST",
                                   "detail": "请求体必须为 JSON 对象"})
            return

        audit_id = payload.get("audit_id")
        if not isinstance(audit_id, str) or not audit_id:
            self._write_json(400, {"error": "BAD_AUDIT_ID",
                                   "detail": "字段 audit_id 必须为非空字符串（稳定审计标识）"})
            return
        if len(audit_id) > 128:
            self._write_json(400, {"error": "BAD_AUDIT_ID",
                                   "detail": "audit_id 长度不得超过 128"})
            return

        # Audit bookkeeping takes precedence over analysis for a KNOWN id:
        # a retransmitted id must replay, a conflicting id must be refused
        # with the original evidence -- even if the new body is malformed.
        existing = self.store.get(audit_id)
        if existing is not None:
            incoming_hash = None
            try:
                incoming_hash, _ = canonical_fingerprint(payload)
            except (KeyError, TypeError):
                incoming_hash = _loose_fingerprint(raw)
            if existing["request_hash"] == incoming_hash:
                self._write_json(200, {
                    "replayed": True,
                    "seal_status": "REPLAYED",
                    "audit_id": audit_id,
                    "request_hash": existing["request_hash"],
                    "sealed_at": existing["sealed_at"],
                    "protocol": PROTOCOL,
                    "result": existing["conclusion"],
                })
                return
            self._write_json(409, {
                "error": "AUDIT_ID_CONFLICT",
                "detail": (
                    f"审计标识 {audit_id!r} 已封存不同输入；原证据保留不变。"
                    f"原指纹 {existing['request_hash'][:16]}…，"
                    f"本次指纹 {incoming_hash[:16]}…。"
                    "请重传原始输入（语义等价即回放）或改用新标识"
                ),
                "audit_id": audit_id,
                "original_evidence": existing,
                "incoming_fingerprint": incoming_hash,
            })
            return

        # New id: validate shape first so malformed input yields the first
        # actionable reason instead of being sealed (or 500).
        try:
            build_grammar(payload)
        except ValidationError as exc:
            self._write_json(400, {"error": exc.reason, "detail": exc.detail})
            return

        try:
            entry, status = self.store.submit(audit_id, payload, lambda: _compute(payload))
        except ConflictError as exc:  # pragma: no cover - race safety net
            self._write_json(409, {
                "error": "AUDIT_ID_CONFLICT",
                "detail": exc.detail,
                "audit_id": audit_id,
                "original_evidence": exc.existing,
                "incoming_fingerprint": exc.incoming_hash,
            })
            return

        verdict = entry["conclusion"]["verdict"]
        self._write_json(200, {
            "replayed": False,
            "seal_status": status,
            "audit_id": audit_id,
            "request_hash": entry["request_hash"],
            "sealed_at": entry["sealed_at"],
            "protocol": PROTOCOL,
            "result": entry["conclusion"],
        })


def make_server(host: str, port: int, store: Optional[SealedStore] = None
                ) -> Tuple[ThreadingHTTPServer, SealedStore]:
    store = store or SealedStore(STORE_PATH)

    class _Handler(ArbiterHandler):
        pass

    _Handler.store = store
    httpd = ThreadingHTTPServer((host, port), _Handler)
    return httpd, store


def main() -> None:
    host = os.environ.get("ARBITER_HOST", "0.0.0.0")
    port = int(os.environ.get("ARBITER_PORT", "8080"))
    httpd, _ = make_server(host, port)
    print(f"forest-arbiter listening on {host}:{port} (store {STORE_PATH})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Compatibility HTTP facade from legacy Echo Question Mode to Hermes Gadget.

Keeps echo_oneshot.py unchanged while turns migrate to the Gadget platform
session. The actual WebSocket/session owner is echo_gadget_shadow.py on :8798.
"""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GADGET_HOST = "127.0.0.1"
GADGET_PORT = 8798


def gadget(req: dict, timeout: float = 190.0) -> dict:
    with socket.create_connection((GADGET_HOST, GADGET_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        line = s.makefile("r", encoding="utf-8").readline()
    if not line:
        raise RuntimeError("empty gadget bridge response")
    return json.loads(line)


class Handler(BaseHTTPRequestHandler):
    server_version = "EchoGadgetTurnProxy/0.1"

    def log_message(self, fmt, *args):
        pass

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def reply(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/health":
            try:
                st = gadget({"op": "status"}, 5)
                self.reply(200 if st.get("online") else 503, st)
            except Exception as exc:
                self.reply(503, {"ok": False, "error": str(exc)})
        elif path == "/session":
            try:
                st = gadget({"op": "status"}, 5)
                self.reply(200, {"ok": True, "backend": "gadget", "status": st})
            except Exception as exc:
                self.reply(503, {"ok": False, "error": str(exc)})
        else:
            self.reply(404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        try:
            body = self.body()
        except Exception as exc:
            self.reply(400, {"ok": False, "error": f"bad_request: {exc}"})
            return

        try:
            if path == "/turn":
                message = str(body.get("message") or "").strip()
                if not message:
                    self.reply(400, {"ok": False, "error": "empty_message"})
                    return
                t0 = time.time()
                out = gadget({"op": "turn", "message": message, "timeout_s": 180}, 190)
                if not out.get("ok"):
                    self.reply(502, out)
                    return
                answer = str(out.get("answer") or "").strip()
                self.reply(200, {
                    "ok": True, "answer": answer,
                    "session_id": out.get("turn") or out.get("request_id"),
                    "reused": True, "turn_index": None,
                    "hook_ran": True, "injected": True,
                    "tool_calls": [], "latency_ms": out.get("latency_ms") or int((time.time()-t0)*1000),
                    "cancelled": out.get("outcome") == "cancelled",
                    "platform": "gadget", "gadget": out,
                })
                return
            if path == "/cancel":
                out = gadget({"op": "cancel"}, 5)
                self.reply(200, out)
                return
            if path == "/reset":
                out = gadget({"op": "new"}, 10)
                self.reply(200, {"ok": bool(out.get("ok")), "old_session_id": None,
                                 "platform": "gadget", "gadget": out})
                return
            if path == "/shutdown":
                self.reply(200, {"ok": True, "shutting_down": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self.reply(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            self.reply(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    print(f"echo-gadget-turn-proxy listening on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

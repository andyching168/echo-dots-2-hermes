#!/usr/bin/env python3
"""Thin compatibility facade for Echo Question Mode.

Healthy path:
    echo_oneshot -> echo_gadget_shadow (:8798) -> Hermes Gadget

This HTTP service remains on :8796 for compatibility and starts the legacy
Hermes worker only when Gadget recovery fails or the backend is explicitly
switched to legacy. The Gadget shadow adapter is owned by the dedicated
hermes-echo-gadget.service unit; this facade only asks systemd to (re)start it.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GADGET_HOST = os.getenv("ECHO_GADGET_LOCAL_HOST", "127.0.0.1")
GADGET_PORT = int(os.getenv("ECHO_GADGET_LOCAL_PORT", "8798"))
LEGACY_HOST = "127.0.0.1"
LEGACY_PORT = int(os.getenv("ECHO_LEGACY_WORKER_PORT", "8799"))
LEGACY_IDLE_S = float(os.getenv("ECHO_LEGACY_IDLE_S", "300"))
GADGET_SERVICE = os.getenv("ECHO_GADGET_SERVICE", "hermes-echo-gadget.service")
_state = {
    "backend": os.getenv("ECHO_Q_BACKEND", "gadget").strip().lower(),
    "legacy_proc": None,
    "legacy_last_use": 0.0,
    "legacy_args": [],
    "lock": threading.RLock(),
    "httpd": None,
}


def _json_socket(req: dict, timeout: float = 190.0) -> dict:
    with socket.create_connection((GADGET_HOST, GADGET_PORT), timeout=min(timeout, 5.0)) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        line = s.makefile("r", encoding="utf-8").readline()
    if not line:
        raise RuntimeError("empty Gadget bridge response")
    return json.loads(line)


def _gadget_ready() -> bool:
    try:
        out = _json_socket({"op": "status"}, 1.0)
        return bool(out.get("online") and out.get("paired"))
    except Exception:
        return False


def _wait_gadget(deadline: float, interval: float = 0.25) -> bool:
    while time.time() < deadline:
        if _gadget_ready():
            return True
        time.sleep(interval)
    return _gadget_ready()


def _gadget_service_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", GADGET_SERVICE],
            check=False, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except Exception:
        return False


def _gadget_service_action(action: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "--user", action, GADGET_SERVICE],
            check=False, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            print(f"[gadget] systemctl {action} {GADGET_SERVICE} rc={r.returncode}: {err}",
                  flush=True)
        return r.returncode == 0
    except Exception as exc:
        print(f"[gadget] systemctl {action} {GADGET_SERVICE} error: {exc}", flush=True)
        return False


def _start_gadget(wait_s: float = 15.0) -> None:
    """Ensure the dedicated Gadget unit is up and the adapter is paired.

    The adapter is owned and supervised by hermes-echo-gadget.service. This
    facade must never fork or terminate echo_gadget_shadow.py itself; it only
    asks systemd to start/restart the unit and then waits for the adapter.
    """
    if _gadget_ready():
        return
    with _state["lock"]:
        if _gadget_ready():
            return
        if not _gadget_service_active():
            _gadget_service_action("start")
    # An already-active unit may still be connecting/pairing; give it a grace
    # period before bouncing it so startup does not restart a live adapter.
    if _wait_gadget(time.time() + min(wait_s, 8.0)):
        print("[gadget] adapter online via systemd", flush=True)
        return
    with _state["lock"]:
        if _gadget_ready():
            return
        _gadget_service_action("restart")
    if _wait_gadget(time.time() + wait_s):
        print("[gadget] adapter recovered via systemd restart", flush=True)
        return
    raise RuntimeError("Gadget adapter did not become ready via systemd")


def _http_json(method: str, url: str, payload: dict | None, timeout: float) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())
def _legacy_url(path: str) -> str:
    return f"http://{LEGACY_HOST}:{LEGACY_PORT}{path}"


def _legacy_ready() -> bool:
    try:
        out = _http_json("GET", _legacy_url("/health"), None, 1.0)
        return bool(out.get("ok"))
    except Exception:
        return False


def _start_legacy() -> None:
    with _state["lock"]:
        if _legacy_ready():
            _state["legacy_last_use"] = time.time()
            return
        proc = _state.get("legacy_proc")
        if proc is not None and proc.poll() is None:
            pass
        else:
            env = dict(os.environ)
            env["ECHO_Q_BACKEND"] = "legacy"
            cmd = [
                os.getenv("HERMES_PYTHON", os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")),
                os.path.join(HERE, "echo_legacy_worker.py"),
                "--host", LEGACY_HOST, "--port", str(LEGACY_PORT),
                *_state["legacy_args"],
            ]
            proc = subprocess.Popen(cmd, cwd=HERE, env=env)
            _state["legacy_proc"] = proc
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"legacy worker exited rc={proc.returncode}")
        if _legacy_ready():
            _state["legacy_last_use"] = time.time()
            print(f"[legacy] worker ready pid={proc.pid}", flush=True)
            return
        time.sleep(0.25)
    raise RuntimeError("legacy worker did not become ready")


def _legacy_call(path: str, payload: dict, timeout: float = 190.0) -> dict:
    _start_legacy()
    _state["legacy_last_use"] = time.time()
    return _http_json("POST", _legacy_url(path), payload, timeout)


def _stop_legacy(reason: str) -> None:
    with _state["lock"]:
        proc = _state.get("legacy_proc")
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    _http_json("POST", _legacy_url("/shutdown"), {}, 3.0)
                    proc.wait(timeout=5)
                except Exception:
                    proc.terminate()
                    proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _state["legacy_proc"] = None
        print(f"[legacy] worker stopped reason={reason}", flush=True)
def _legacy_reaper() -> None:
    while True:
        time.sleep(30)
        if _state["backend"] == "legacy":
            continue
        proc = _state.get("legacy_proc")
        if proc is None or proc.poll() is not None:
            continue
        last = float(_state.get("legacy_last_use") or 0)
        if last and time.time() - last >= LEGACY_IDLE_S:
            _stop_legacy("idle_timeout")


def _gadget_turn(message: str) -> dict:
    try:
        out = _json_socket({"op": "turn", "message": message, "timeout_s": 180}, 190.0)
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "Gadget turn failed")
        return out
    except Exception as first:
        print(f"[gadget] first attempt failed: {first}", flush=True)
        _start_gadget()
        out = _json_socket({"op": "turn", "message": message, "timeout_s": 180}, 190.0)
        if not out.get("ok"):
            raise RuntimeError(out.get("error") or "Gadget retry failed")
        out["gadget_recovered"] = True
        return out
def _compat_gadget_payload(out: dict) -> dict:
    return {
        "ok": True,
        "answer": str(out.get("answer") or "").strip(),
        "session_id": out.get("turn") or out.get("request_id"),
        "reused": True,
        "turn_index": None,
        "hook_ran": False,
        "injected": False,
        "native_context": True,
        "tool_calls": [],
        "latency_ms": out.get("latency_ms", 0),
        "cancelled": out.get("outcome") == "cancelled",
        "platform": "gadget",
        "gadget": out,
        **({"gadget_recovered": True} if out.get("gadget_recovered") else {}),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "EchoTurnFacade/1.0"

    def log_message(self, fmt, *args):
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _reply(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/health":
            self._reply(200, {"ok": True})
            return
        if path == "/backend":
            self._reply(200, {"ok": True, "backend": _state["backend"],
                              "choices": ["gadget", "legacy"]})
            return
        if path == "/session":
            legacy = _legacy_ready()
            self._reply(200, {
                "ok": True,
                "backend": _state["backend"],
                "gadget_ready": _gadget_ready(),
                "legacy_initialized": legacy,
                "legacy_worker_pid": (_state["legacy_proc"].pid if legacy and _state.get("legacy_proc") else None),
                "legacy_idle_s": LEGACY_IDLE_S,
            })
            return
        self._reply(404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        try:
            body = self._body()
            if path == "/backend":
                mode = str(body.get("backend") or "").strip().lower()
                if mode not in ("gadget", "legacy"):
                    self._reply(400, {"ok": False, "error": "backend must be gadget or legacy"})
                    return
                if mode == "legacy":
                    _start_legacy()
                else:
                    _start_gadget()
                _state["backend"] = mode
                self._reply(200, {"ok": True, "backend": mode,
                                  "legacy_initialized": _legacy_ready()})
                return

            if path == "/turn":
                message = str(body.get("message") or "").strip()
                if not message:
                    self._reply(400, {"ok": False, "error": "empty_message"})
                    return
                if _state["backend"] == "legacy":
                    self._reply(200, _legacy_call("/turn", body))
                    return
                try:
                    self._reply(200, _compat_gadget_payload(_gadget_turn(message)))
                except Exception as exc:
                    print(f"[gadget] recovery failed; legacy fallback: {exc}", flush=True)
                    out = _legacy_call("/turn", body)
                    out["fallback_from"] = "gadget"
                    out["fallback_error"] = str(exc)
                    self._reply(200, out)
                return
            if path == "/cancel":
                if _state["backend"] == "legacy":
                    self._reply(200, _legacy_call("/cancel", body, 10.0))
                    return
                try:
                    self._reply(200, _json_socket({"op": "cancel"}, 5.0))
                except Exception:
                    self._reply(200, _legacy_call("/cancel", body, 10.0))
                return

            if path == "/reset":
                if _state["backend"] == "legacy":
                    self._reply(200, _legacy_call("/reset", body, 15.0))
                    return
                try:
                    self._reply(200, _json_socket({"op": "new"}, 10.0))
                except Exception:
                    self._reply(200, _legacy_call("/reset", body, 15.0))
                return

            if path == "/shutdown":
                self._reply(200, {"ok": True, "shutting_down": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            self._reply(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            self._reply(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--qsession-ttl", type=float, default=300.0)
    ap.add_argument("--qsession-max", type=int, default=2)
    ap.add_argument("--max-iterations", type=int, default=16)
    ap.add_argument("--mcp-servers", default="ha")
    ap.add_argument("--exclude-toolsets", default="")
    ap.add_argument("--trace-log", default=os.path.expanduser("~/.hermes/echo_qsession_trace.log"))
    ap.add_argument("--simulate-tool-failure", default="")
    ap.add_argument("--once", default="")
    args = ap.parse_args()

    _state["legacy_args"] = [
        "--qsession-ttl", str(args.qsession_ttl),
        "--qsession-max", str(args.qsession_max),
        "--max-iterations", str(args.max_iterations),
        "--mcp-servers", args.mcp_servers,
        "--exclude-toolsets", args.exclude_toolsets,
        "--trace-log", args.trace_log,
    ]
    if args.simulate_tool_failure:
        _state["legacy_args"] += ["--simulate-tool-failure", args.simulate_tool_failure]
    if _state["backend"] == "gadget":
        try:
            _start_gadget()
        except Exception as exc:
            print(f"[gadget] adapter not ready at startup: {exc}", flush=True)
    else:
        _start_legacy()

    threading.Thread(target=_legacy_reaper, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    _state["httpd"] = httpd
    print(f"[ready] thin Echo turn facade on http://{args.host}:{args.port} "
          f"backend={_state['backend']} legacy_idle={LEGACY_IDLE_S}s", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        _stop_legacy("facade_shutdown")


if __name__ == "__main__":
    main()

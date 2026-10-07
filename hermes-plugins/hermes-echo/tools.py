"""hermes-echo tool handlers.

All handlers talk to the EchoController through the Phase 5B admin console
(127.0.0.1:8794) using its line protocol. They never touch /dev/input, ALSA,
Android properties, GPIO, LED sysfs or Echo-side processes — Hermes stays
ignorant of hardware details.
"""

import json
import logging
import os
import socket

logger = logging.getLogger("hermes_echo")

ADMIN_HOST = os.getenv("ECHO_ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.getenv("ECHO_ADMIN_PORT", "8794"))
ADMIN_TIMEOUT = float(os.getenv("ECHO_ADMIN_TIMEOUT", "5"))


def _admin(line: str) -> str:
    """Open a short-lived admin connection, skip the banner, run one command."""
    with socket.create_connection((ADMIN_HOST, ADMIN_PORT), timeout=ADMIN_TIMEOUT) as s:
        s.settimeout(ADMIN_TIMEOUT)
        f = s.makefile("rwb")
        f.readline()  # banner
        f.write((line + "\n").encode())
        f.flush()
        return f.readline().decode(errors="replace").strip()


def _admin_json(line: str) -> dict:
    resp = _admin(line)
    if resp.startswith("json "):
        try:
            return json.loads(resp[5:])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "bad_json", "detail": str(exc), "raw": resp[:200]}
    return {"ok": False, "error": "admin_error", "raw": resp[:200]}


def admin_available() -> bool:
    try:
        with socket.create_connection((ADMIN_HOST, ADMIN_PORT), timeout=1) as s:
            s.settimeout(1)
            banner = s.makefile("rwb").readline()
            return bool(banner)
    except Exception:
        return False


def _tool(name: str, args: dict) -> str:
    out = _admin_json(f"tool {name} {json.dumps(args)}")
    return json.dumps(out)


def fetch_context() -> str:
    """Return the live [Echo Runtime Context] block (for the pre_llm_call hook)."""
    out = _admin_json("context")
    return out.get("context", "") or ""


# ── handlers: signature (args: dict, **kwargs) -> JSON string ─────────────────


def echo_get_state(args=None, **kwargs) -> str:
    return _tool("echo_get_state", {})


def echo_get_volume(args=None, **kwargs) -> str:
    return _tool("echo_get_volume", {})


def echo_enter_live_mode(args=None, **kwargs) -> str:
    return _tool("echo_enter_live_mode", {})


def echo_end_live_mode(args=None, **kwargs) -> str:
    a = args or {}
    return _tool("echo_end_live_mode", {"reason": a.get("reason", "remote_control")})


def echo_interrupt(args=None, **kwargs) -> str:
    a = args or {}
    return _tool("echo_interrupt", {"reason": a.get("reason", "remote_control")})


def echo_set_volume(args=None, **kwargs) -> str:
    a = args or {}
    return _tool("echo_set_volume", {"value": a.get("value")})


def echo_play_chime(args=None, **kwargs) -> str:
    a = args or {}
    return _tool("echo_play_chime", {"name": a.get("name", "listen")})

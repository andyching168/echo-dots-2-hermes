#!/usr/bin/env python3
"""Hermes Gadget shadow client for the existing EchoController.

Phase 1 migration adapter:
- registers Echo Dot (Hiyo) as a first-class Hermes Gadget
- exposes safe EchoController actions
- offers a localhost turn API so the existing Question Mode can pilot the
  Gadget session without touching the proven audio/control transport
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import socket
import time
from pathlib import Path

import websockets

SERVER = os.getenv("ECHO_GADGET_URL", "ws://127.0.0.1:8766/gadget")
STATE = Path(os.getenv("ECHO_GADGET_STATE", str(Path(__file__).with_name("gadget_state.json"))))
ADMIN_HOST = os.getenv("ECHO_ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.getenv("ECHO_ADMIN_PORT", "8794"))
LOCAL_HOST = os.getenv("ECHO_GADGET_LOCAL_HOST", "127.0.0.1")
LOCAL_PORT = int(os.getenv("ECHO_GADGET_LOCAL_PORT", "8798"))
AUTH_CONTEXT = b"hermes-gadget/v1|"

ACTIONS = [
    {"name": "echo.get_state", "description": "Read the authoritative Echo interaction state.",
     "params": {"type": "object", "properties": {}}},
    {"name": "echo.get_volume", "description": "Read Echo playback volume.",
     "params": {"type": "object", "properties": {}}},
    {"name": "echo.enter_live_mode", "description": "Enter Echo continuous Live Mode when valid.",
     "params": {"type": "object", "properties": {}}},
    {"name": "echo.end_live_mode", "description": "End Echo Live Mode.",
     "params": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    {"name": "echo.interrupt", "description": "Interrupt the current Echo interaction.",
     "params": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    {"name": "echo.set_volume", "description": "Set Echo playback volume from 0 to 100.",
     "params": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}},
    {"name": "echo.play_chime", "description": "Play an Echo acknowledgement chime.",
     "params": {"type": "object", "properties": {"name": {"type": "string"}}}},
]
ACTION_TO_TOOL = {
    "echo.get_state": "echo_get_state",
    "echo.get_volume": "echo_get_volume",
    "echo.enter_live_mode": "echo_enter_live_mode",
    "echo.end_live_mode": "echo_end_live_mode",
    "echo.interrupt": "echo_interrupt",
    "echo.set_volume": "echo_set_volume",
    "echo.play_chime": "echo_play_chime",
}

class BridgeState:
    def __init__(self) -> None:
        self.ws = None
        self.paired = False
        self.device_id = ""
        self.send_lock = asyncio.Lock()
        self.pending = None
        self.seq = 0
        self.last_status = ""
        self.connected_at = 0.0

    async def send(self, obj: dict) -> None:
        if self.ws is None:
            raise RuntimeError("gadget is offline")
        async with self.send_lock:
            await self.ws.send(json.dumps(obj, separators=(",", ":")))

    async def turn(self, text: str, timeout: float = 180.0) -> dict:
        if not self.ws or not self.paired:
            return {"ok": False, "error": "gadget_not_ready", "paired": self.paired}
        if self.pending is not None:
            return {"ok": False, "error": "turn_busy"}
        self.seq += 1
        req_id = f"echo-q-{self.seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending = {
            "id": req_id, "future": fut, "started": time.monotonic(), "turn": "",
            "draft": "", "answer": "", "status": [], "prompt_denied": None,
        }
        try:
            await self.send({"type": "text", "id": req_id, "text": text})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            try:
                await self.send({"type": "cancel"})
            except Exception:
                pass
            return {"ok": False, "error": "turn_timeout", "request_id": req_id}
        finally:
            if self.pending is not None and self.pending.get("id") == req_id:
                self.pending = None

BRIDGE = BridgeState()


def echo_sensor_snapshot() -> dict:
    """Expose small numeric controller state through Gadget telemetry."""
    try:
        st = admin("tool echo_get_state {}")
    except Exception:
        return {}
    mode_map = {
        "idle": 0, "listen_one_shot": 1, "question_active": 2,
        "live_mode": 3, "live_muted": 4, "error_recovery": 5,
        "announcing": 6,
    }
    return {
        "volume_pct": float(st.get("volume", 0)),
        "mic_muted": 1.0 if st.get("physical_mute") else 0.0,
        "session_active": 1.0 if st.get("session_active") else 0.0,
        "interaction_mode_code": float(mode_map.get(str(st.get("interaction_mode") or "").lower(), -1)),
    }


async def telemetry_loop() -> None:
    last = None
    while True:
        await asyncio.sleep(2.0)
        if not BRIDGE.ws or not BRIDGE.paired:
            continue
        sensors = await asyncio.to_thread(echo_sensor_snapshot)
        if sensors and sensors != last:
            try:
                await BRIDGE.send({"type": "state", "sensors": sensors})
                last = sensors
            except Exception:
                pass


def load_identity() -> tuple[bytes, str]:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        data = json.loads(STATE.read_text())
        key = base64.b64decode(data["device_key"], validate=True)
    else:
        key = os.urandom(32)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"device_key": base64.b64encode(key).decode()}))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE)
    if len(key) != 32:
        raise RuntimeError("invalid gadget device key")
    return key, "hg-" + hashlib.sha256(key).hexdigest()[:16]


def admin(line: str) -> dict:
    with socket.create_connection((ADMIN_HOST, ADMIN_PORT), timeout=5) as sock:
        sock.settimeout(5)
        f = sock.makefile("rwb")
        f.readline()
        f.write((line + "\n").encode())
        f.flush()
        raw = f.readline().decode(errors="replace").strip()
    if not raw.startswith("json "):
        raise RuntimeError(raw or "empty Echo admin response")
    return json.loads(raw[5:])


def action_call(name: str, args: dict) -> dict:
    tool = ACTION_TO_TOOL.get(name)
    if not tool:
        raise ValueError(f"unknown action {name}")
    return admin(f"tool {tool} {json.dumps(args, separators=(',', ':'))}")


def auth_mac(key: bytes, device_id: str, nonce: str) -> str:
    msg = AUTH_CONTEXT + device_id.encode() + b"|" + nonce.encode()
    return base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode()

async def handle_protocol_message(msg: dict) -> None:
    typ = msg.get("type")
    if typ == "ping":
        await BRIDGE.send({"type": "pong", "ts": msg.get("ts")})
        return
    if typ == "pairing":
        print(f"[gadget] PAIR CODE {msg.get('code')} command={msg.get('command')}", flush=True)
        return
    if typ == "paired":
        BRIDGE.paired = True
        print("[gadget] paired", flush=True)
        return
    if typ == "unpaired":
        BRIDGE.paired = False
        print("[gadget] unpaired", flush=True)
        return
    if typ == "action":
        ident, name, args = msg.get("id"), msg.get("name"), msg.get("args") or {}
        try:
            result = await asyncio.to_thread(action_call, name, args)
            await BRIDGE.send({"type": "action.result", "id": ident, "ok": True, "result": result})
        except Exception as exc:
            await BRIDGE.send({"type": "action.result", "id": ident, "ok": False,
                               "error": f"{type(exc).__name__}: {exc}"})
        return
    if typ == "error":
        print(f"[gadget] error {msg}", flush=True)

    pending = BRIDGE.pending
    if pending is None:
        return
    if typ == "turn.start":
        pending["turn"] = str(msg.get("turn") or "")
    elif typ == "status":
        text = str(msg.get("text") or "")
        BRIDGE.last_status = text
        if text:
            pending["status"].append(text)
    elif typ == "reply.delta":
        pending["draft"] = str(msg.get("text") or "")
    elif typ == "reply":
        if not msg.get("interim"):
            pending["answer"] = str(msg.get("text") or "")
    elif typ == "prompt":
        # Phase 1 is intentionally conservative: do not let a headless migration
        # bridge approve destructive/costly actions. Deny and record the prompt.
        pending["prompt_denied"] = {
            "id": msg.get("id"), "title": msg.get("title"), "text": msg.get("text")
        }
        await BRIDGE.send({"type": "prompt.reply", "id": msg.get("id"), "answer": "no"})
    elif typ == "turn.end":
        fut = pending["future"]
        if not fut.done():
            answer = pending["answer"] or pending["draft"]
            fut.set_result({
                "ok": True,
                "answer": answer,
                "outcome": msg.get("outcome"),
                "turn": pending["turn"] or msg.get("turn"),
                "request_id": pending["id"],
                "latency_ms": int((time.monotonic() - pending["started"]) * 1000),
                "status": pending["status"][-8:],
                "prompt_denied": pending["prompt_denied"],
                "platform": "gadget",
            })

async def run_once() -> None:
    key, device_id = load_identity()
    async with websockets.connect(
        SERVER, subprotocols=["hermes-gadget.v1"], ping_interval=None, max_size=2 * 1024 * 1024
    ) as ws:
        BRIDGE.ws = ws
        BRIDGE.device_id = device_id
        BRIDGE.connected_at = time.monotonic()
        await BRIDGE.send({
            "type": "hello", "proto": 1, "device_id": device_id,
            "name": "Echo Dot (Hiyo)", "board": "echo-dot-2-custom",
            "firmware": "echo-bridge-shadow-0.3",
            # Headless bridge, but declare UTF-8 shaping so CJK replies are not
            # folded away by the Gadget platform adapter.
            "caps": {"display": {"charset": "utf-8"}}, "actions": ACTIONS,
            "sensors": await asyncio.to_thread(echo_sensor_snapshot),
        })
        first = json.loads(await ws.recv())
        if first.get("type") != "challenge":
            raise RuntimeError(f"expected challenge, got {first}")
        if first.get("enrolled"):
            await BRIDGE.send({"type": "auth", "mac": auth_mac(key, device_id, first["nonce"])})
        else:
            await BRIDGE.send({"type": "auth", "key": base64.b64encode(key).decode()})
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            raise RuntimeError(f"expected welcome, got {welcome}")
        BRIDGE.paired = bool(welcome.get("paired"))
        print(f"[gadget] online id={device_id} paired={BRIDGE.paired} server={SERVER}", flush=True)

        try:
            while True:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    continue
                await handle_protocol_message(json.loads(frame))
        finally:
            BRIDGE.ws = None
            BRIDGE.paired = False
            if BRIDGE.pending is not None:
                fut = BRIDGE.pending["future"]
                if not fut.done():
                    fut.set_result({"ok": False, "error": "gadget_disconnected"})

async def handle_local(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        req = json.loads(raw.decode() or "{}")
        op = req.get("op")
        if op == "status":
            payload = {
                "ok": True, "online": BRIDGE.ws is not None, "paired": BRIDGE.paired,
                "device_id": BRIDGE.device_id, "busy": BRIDGE.pending is not None,
                "last_status": BRIDGE.last_status,
            }
        elif op == "turn":
            text = str(req.get("message") or "").strip()
            if not text:
                payload = {"ok": False, "error": "empty_message"}
            else:
                payload = await BRIDGE.turn(text, float(req.get("timeout_s") or 180))
        elif op == "cancel":
            if BRIDGE.ws:
                await BRIDGE.send({"type": "cancel"})
            payload = {"ok": True, "cancelled": BRIDGE.pending is not None}
        elif op == "new":
            if not BRIDGE.ws or not BRIDGE.paired:
                payload = {"ok": False, "error": "gadget_not_ready"}
            else:
                await BRIDGE.send({"type": "session.new"})
                payload = {"ok": True}
        else:
            payload = {"ok": False, "error": "unknown_op"}
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def connector() -> None:
    delay = 1
    while True:
        try:
            await run_once()
            delay = 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[gadget] disconnected: {type(exc).__name__}: {exc}; retry in {delay}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def main() -> None:
    server = await asyncio.start_server(handle_local, LOCAL_HOST, LOCAL_PORT)
    print(f"[gadget] local turn API {LOCAL_HOST}:{LOCAL_PORT}", flush=True)
    await asyncio.gather(connector(), telemetry_loop(), server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""Phase 5H — expose the Echo (Hiyo) to Home Assistant as a media_player via MQTT.

Home Assistant already runs an MQTT broker with discovery enabled.  This bridge:

  * publishes an MQTT discovery config for `media_player.hiyo_echo` (so HA
    creates the entity automatically), then
  * publishes state (state / volume / muted / media title) from the authoritative
    EchoController admin console, and
  * subscribes to HA commands (play/pause/turn on/off/volume/mute) and forwards
    them to the EchoController.

It does NOT own any audio hardware: it is a pure control/telemetry bridge, so it
coexists with the oneshot/live/barge-in paths.

Env:
    ECHO_MQTT_HOST   broker host             (default 192.168.0.10)
    ECHO_MQTT_PORT   broker port             (default 1883)
    ECHO_MQTT_USER   broker username         (required)
    ECHO_MQTT_PASS   broker password         (required)
    ECHO_MQTT_PREFIX discovery prefix        (default homeassistant)
    ECHO_ADMIN_HOST  controller admin host   (default 127.0.0.1)
    ECHO_ADMIN_PORT  controller admin port   (default 8794)
    ECHO_HA_NAME     device/entity name      (default "Echo Dot (Hiyo)")
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("ECHO_MQTT_HOST", "192.168.0.10")
MQTT_PORT = int(os.getenv("ECHO_MQTT_PORT", "1883"))
MQTT_USER = os.getenv("ECHO_MQTT_USER", "andyching168")
MQTT_PASS = os.getenv("ECHO_MQTT_PASS", "82266601")
PREFIX = os.getenv("ECHO_MQTT_PREFIX", "homeassistant")
ADMIN_HOST = os.getenv("ECHO_ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.getenv("ECHO_ADMIN_PORT", "8794"))
NAME = os.getenv("ECHO_HA_NAME", "Echo Dot (Hiyo)")

NODE = "hiyo_echo"
OBJ = f"{PREFIX}/media_player/{NODE}"
AVAIL = f"{OBJ}/availability"
STATE = f"{OBJ}/state"
TITLE = f"{OBJ}/title"
VOLUME = f"{OBJ}/volume"           # 0-100
# Command topics (mqtt_media_player custom component schema)
CMD_PLAY = f"{OBJ}/play"
CMD_PAUSE = f"{OBJ}/pause"
CMD_PLAYPAUSE = f"{OBJ}/playpause"
CMD_VOL = f"{OBJ}/volumeset"
CMD_PLAYM = f"{OBJ}/playmedia"    # JSON {media_type, media_id}
CMD_SAY = f"{OBJ}/say"            # raw text -> Hiyo voice announcement

# Hiyo has no on-device media queue, so `play_media` is mapped to a TTS
# announcement: the media_id (or its title/url) is spoken via the Echo.
ANNOUNCE = os.getenv("ECHO_HA_ANNOUNCE", "1").strip().lower() not in ("0", "false", "no", "")

# Hiyo controller state -> HA media_player state
_HA_STATE = {
    "IDLE": "idle",
    "LISTEN_ONE_SHOT": "idle",
    "QUESTION_ACTIVE": "playing",
    "LIVE_MODE": "playing",
    "LIVE_MUTED": "idle",
    "ERROR_RECOVERY": "idle",
    "ANNOUNCING": "playing",
}


async def admin_cmd(cmd: str, timeout: float = 3.0) -> str:
    """Send one line to the controller admin console; return the reply line."""
    reader, writer = await asyncio.open_connection(ADMIN_HOST, ADMIN_PORT)
    try:
        await reader.readline()              # banner
        writer.write((cmd + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        return line.decode(errors="replace").strip()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


def _parse_state(line: str) -> dict:
    out = {"state": "IDLE", "volume": 70, "muted": 0, "interrupted": 0}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


class HaMediaBridge:
    def __init__(self, log=None):
        self._log = log or (lambda m: print(m, flush=True))
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hiyo-ha-media")
        self._client.username_pw_set(MQTT_USER, MQTT_PASS)
        self._client.will_set(AVAIL, "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._connected = False
        self._last = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- MQTT callbacks ----------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            self._connected = True
            self._log(f"[ha] MQTT connected to {MQTT_HOST}:{MQTT_PORT}")
            for t in (CMD_PLAY, CMD_PAUSE, CMD_PLAYPAUSE, CMD_VOL, CMD_PLAYM, CMD_SAY):
                client.subscribe(t)
            self._publish_discovery()
            client.publish(AVAIL, "online", retain=True)
            # Push state immediately so HA never shows `unknown` on fresh setup.
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(self._push_now(), self._loop)
        else:
            self._log(f"[ha] MQTT connect failed rc={rc}")

    async def _push_now(self) -> None:
        try:
            line = await admin_cmd("state", timeout=3.0)
            if line.startswith("state="):
                self.publish_state(_parse_state(line))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[ha] initial state push failed: {exc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode(errors="replace").strip()
        except Exception:  # noqa: BLE001
            return
        topic = msg.topic
        if topic == CMD_VOL:
            cmd = f"volume:{payload}"
        elif topic == CMD_PLAYM:
            cmd = f"playmedia:{payload}"
        elif topic == CMD_SAY:
            cmd = f"say:{payload}"
        elif topic in (CMD_PLAY,):
            cmd = "play"
        elif topic in (CMD_PAUSE,):
            cmd = "pause"
        elif topic in (CMD_PLAYPAUSE,):
            cmd = "pause"      # toggle: Hiyo is either listening or idle
        else:
            cmd = payload
        self._log(f"[ha] command: {cmd!r} (topic={topic})")
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._handle_command(cmd), self._loop)

    async def _handle_command(self, cmd: str) -> None:
        # Lower-case ONLY the command keyword; the payload (URLs, hashes, text)
        # must keep its original case (tts_proxy tokens are case-sensitive).
        head, sep, payload = cmd.partition(":")
        c = head.lower()
        try:
            if c in ("play", "resume", "turn_on", "playpause"):
                r = await admin_cmd("action short")
            elif c in ("pause", "stop", "turn_off"):
                # Stop radio if running, then interrupt any Hiyo interaction.
                try:
                    await admin_cmd("stopstream", timeout=10.0)
                except Exception:  # noqa: BLE001
                    pass
                r = await admin_cmd("interrupt")
            elif c == "volume" and sep:
                # The component publishes HA's volume_level (0.0-1.0); Hiyo wants 0-100.
                v = float(payload)
                pct = round(v * 100) if v <= 1.0 else round(v)
                r = await admin_cmd(f"volume {int(pct)}")
            elif c == "playmedia" and sep:
                r = await self._handle_play_media(payload)
            elif c == "say" and sep:
                r = await admin_cmd(f"say {payload}", timeout=90.0)
            else:
                self._log(f"[ha] unknown command {cmd!r}")
                return
            self._log(f"[ha] cmd {cmd!r} -> {r}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[ha] command {cmd!r} failed: {type(exc).__name__}: {exc}")

    async def _handle_play_media(self, payload: str) -> str:
        """Handle HA `play_media`.

        The component publishes JSON {"media_type": ..., "media_id": ...}.
        - If media_type is a live stream (aac / mpegurl / radio), start a
          continuous radio stream (`stream` command) — no auto-end.
        - Otherwise a URL/path is decoded and played to completion (`announce`).
        - Plain text is forwarded to `turn` (Hiyo answers/speaks it).
        """
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            data = {"media_id": payload}
        media_id = str(data.get("media_id") or data.get("media_content_id") or "").strip()
        media_type = str(data.get("media_type") or "").lower()
        if not media_id:
            return "playmedia:empty"
        # Stop any previous radio before starting something new.
        try:
            await admin_cmd("stopstream", timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        if media_id.startswith(("http://", "https://")):
            live = any(k in media_type for k in ("aac", "mpegurl", "radio")) or \
                   media_id.endswith((".m3u", ".m3u8", ".pls", ".aac"))
            if live:
                r = await admin_cmd(f"stream {media_id}", timeout=30.0)
                return f"playmedia(stream) {r[:80]}"
            r = await admin_cmd(f"announce {media_id}", timeout=120.0)
            return f"playmedia(announce) {r[:80]}"
        if media_id.startswith("/"):
            r = await admin_cmd(f"announce {media_id}", timeout=120.0)
            return f"playmedia(announce) {r[:80]}"
        r = await admin_cmd(f"turn {media_id}", timeout=90.0)
        return f"playmedia(turn) {r[:80]}"

    # ---- discovery ---------------------------------------------------------
    def _publish_discovery(self) -> None:
        # Schema for the HACS `mqtt_media_player` component (bkbilly/mqtt_media_player).
        cfg = {
            "name": NAME,
            "availability": {
                "topic": AVAIL,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            "state_state_topic": STATE,
            "state_title_topic": TITLE,
            "state_volume_topic": VOLUME,
            "state_mediatype_topic": f"{OBJ}/mediatype",
            "command_play_topic": CMD_PLAY,
            "command_pause_topic": CMD_PAUSE,
            "command_playpause_topic": CMD_PLAYPAUSE,
            "command_volume_topic": CMD_VOL,
            "command_playmedia_topic": CMD_PLAYM,
            "device": {
                "identifiers": [NODE],
                "name": NAME,
                "manufacturer": "Amazon",
                "model": "Echo Dot 2nd gen (Hiyo)",
                "sw_version": "Hiyo 5H",
            },
        }
        self._client.publish(f"{OBJ}/config", json.dumps(cfg), retain=True)
        self._client.publish(f"{OBJ}/mediatype", "music", retain=True)
        self._log(f"[ha] published discovery -> {OBJ}/config")

    # ---- state publishing --------------------------------------------------
    def publish_state(self, st: dict) -> None:
        ctrl = str(st.get("state", "IDLE"))
        ha_state = _HA_STATE.get(ctrl, "idle")
        try:
            vol = int(st.get("volume", 70))
        except Exception:  # noqa: BLE001
            vol = 70
        # Title: underlying media ids (TTS hash filenames, radio ids, raw
        # filenames) are not meaningful, so always advertise the generic label.
        title = "Default Media Receiver"
        payloads = {
            STATE: ha_state,
            # The component maps state_volume_topic straight to HA's volume_level,
            # which is 0.0-1.0 (not 0-100).
            VOLUME: f"{max(0, min(100, vol)) / 100.0:.3f}",
            TITLE: title,
        }
        for topic, val in payloads.items():
            if self._last.get(topic) != val:
                self._last[topic] = val
                self._client.publish(topic, val, retain=True)

    def connect(self) -> None:
        self._client.connect(MQTT_HOST, MQTT_PORT, 15)
        self._client.loop_start()
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.connect()
        # Also publish a state once on startup (LWT/retained availability can
        # otherwise leave HA at `unknown`).
        await asyncio.sleep(1.0)
        await self._push_now()
        while True:
            try:
                # tick() lets the controller leave ANNOUNCING once audio drains
                line = await admin_cmd("state", timeout=3.0)
                if line.startswith("state="):
                    self.publish_state(_parse_state(line))
                    # piggyback the announce timer on the 1 s poll
                    await admin_cmd("tick", timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[ha] state poll failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)


if __name__ == "__main__":
    try:
        asyncio.run(HaMediaBridge().run())
    except KeyboardInterrupt:
        pass

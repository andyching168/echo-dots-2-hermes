#!/usr/bin/env python3
"""Phase 5B — Echo mode controller + physical-control arbitration.

Server-side singleton that is the authoritative owner of the Echo interaction
state. It is deliberately independent of GPT Live / Hermes / Seed-VC: for 5B
state changes only produce logs and simulated chimes.

Control priority (highest first), enforced in code:
    physical hardware mute
      -> physical buttons
        -> this controller
          -> local wake/control intents
            -> future Hermes tools
              -> future GPT Live conversational behaviour

Control frames reuse the existing Phase 3/4/5A framing
(magic 0xE0D1, <HBBIH>) on a dedicated control connection, so the audio
transport on :8792 is untouched.
"""

from __future__ import annotations

import array
import asyncio
import json
import math
import os
import struct
import time
from typing import Callable, List, Optional

from echo_hermes import ECHO_PROMPT_FRAGMENT, EchoHermes, dispatch_tool, runtime_context_dict

# --- existing framing (kept identical to echo_server.py) ----------------------
MAGIC = 0xE0D1
HDR = struct.Struct("<HBBIH")

# --- new control message types (Echo -> server) -------------------------------
T_CONTROL_HELLO = 0x10
T_BUTTON_ACTION_DOWN = 0x11
T_BUTTON_ACTION_UP = 0x12
T_BUTTON_VOLUME_UP = 0x13
T_BUTTON_VOLUME_DOWN = 0x14
T_MIC_MUTE_STATE = 0x15
T_VOLUME_VALUE = 0x16
# --- server -> Echo -----------------------------------------------------------
T_SET_VOLUME = 0x90
T_LED_MODE = 0x91
T_LED_FRAME = 0x91   # Phase 5D.5: 72-char IS31FL3236 frame
# --- Echo -> server -----------------------------------------------------------
T_LED_OWNER_STATE = 0x17  # Phase 5D.5B: u8 owner, u8 mode

ACTION_LONG_MS = 2000

# Phase 5G: raw/endpoint mode bypasses the stock audio path, so Hiyo owns the
# physical volume.  The stock ring has 30 steps, so step by 100/30 (~3.33) to
# match the original feel (a full sweep is ~30 presses).
VOLUME_STEP = float(os.getenv("ECHO_VOLUME_STEP", "3.33"))
# Ceiling: two presses below max = stock step 28/30 => level ~93.3 (PCM ~163,
# i.e. below the codec full scale of 175).
VOLUME_MAX = float(os.getenv("ECHO_VOLUME_MAX", "93.3"))
_VOLUME_TONE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "volume_tone_48k_mono.wav")
_volume_tone_cache = None


def load_volume_tone():
    """Stock ``state_volume_adjust_tone`` as a 48 kHz mono int16 array (cached)."""
    global _volume_tone_cache
    if _volume_tone_cache is not None:
        return _volume_tone_cache or None
    import array as _array
    import wave as _wave

    try:
        with _wave.open(_VOLUME_TONE_PATH, "rb") as w:
            if w.getframerate() != 48000 or w.getnchannels() != 1:
                _volume_tone_cache = False
                return None
            a = _array.array("h")
            a.frombytes(w.readframes(w.getnframes()))
        _volume_tone_cache = a
    except Exception:  # noqa: BLE001 - the tone is optional
        _volume_tone_cache = False
    return _volume_tone_cache or None


def load_media_48k(url_or_path: str):
    """Fetch a media URL/path and decode to 48 kHz mono int16 via ffmpeg.

    Used by the HA `play_media` / announce path.  Returns an array('h') or None.
    """
    import array as _array
    import shlex
    import subprocess

    src = url_or_path.strip()
    if not src:
        return None
    try:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
               "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1"]
        out = subprocess.run(cmd, capture_output=True, timeout=120)
        if out.returncode != 0 or not out.stdout:
            return None
        a = _array.array("h")
        a.frombytes(out.stdout[: len(out.stdout) // 2 * 2])
        return a if len(a) else None
    except Exception:  # noqa: BLE001
        return None


def resample_to_48k_mono(pcm: bytes, sr_in: int, ch: int = 1):
    """Linear resample raw s16 PCM to 48 kHz mono."""
    import array as _array
    import numpy as _np

    if not pcm:
        return None
    x = _np.frombuffer(pcm, dtype="<i2")
    if ch > 1:
        x = x[: (len(x) // ch) * ch].reshape(-1, ch).mean(axis=1)
    if sr_in != 48000:
        n_out = int(round(len(x) * 48000 / sr_in))
        if n_out <= 0:
            return None
        idx = _np.arange(n_out, dtype=_np.float64) * (sr_in / 48000.0)
        i0 = idx.astype(_np.int64)
        i1 = _np.minimum(i0 + 1, len(x) - 1)
        frac = (idx - i0).astype(_np.float32)
        x = x[i0] * (1.0 - frac) + x[i1] * frac
    a = _array.array("h")
    a.frombytes(_np.clip(x, -32768, 32767).astype("<i2").tobytes())
    return a if len(a) else None


_BREEZE_URL = os.getenv("ECHO_BREEZE_URL", "http://127.0.0.1:10901/synthesize")
_VC_WS = os.getenv("ECHO_VC_WS", "ws://127.0.0.1:8790")


def _media_label(url_or_text: str) -> str:
    """Best-effort human-readable label for a media URL (HA title)."""
    s = (url_or_text or "").strip()
    if not s:
        return "廣播"
    try:
        from urllib.parse import urlparse, unquote
        if s.startswith(("http://", "https://")):
            p = urlparse(s)
            host = p.netloc
            last = unquote(p.path.rstrip("/").rsplit("/", 1)[-1]) if p.path else ""
            if last:
                return last
            return host or "廣播"
        return s.rsplit("/", 1)[-1] or s
    except Exception:  # noqa: BLE001
        return s[:120]


def synth_say_48k(text: str, voice: bool = True):
    """Synthesize `text` to 48 kHz mono PCM via Breeze TTS (+ optional Seed-VC).

    Mirrors the oneshot pipeline (Breeze :10901 -> Seed-VC :8790) so HA
    announcements use the same Hiyori voice.  Synchronous; returns array('h').

    Set ECHO_HA_SAY_VC=0 to skip Seed-VC (plain Breeze voice).
    """
    import array as _array
    import base64
    import io
    import json as _json
    import urllib.request
    import wave as _wave

    try:
        req = urllib.request.Request(
            _BREEZE_URL, data=_json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            wav = r.read()
        with _wave.open(io.BytesIO(wav), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            return None
        pcm48 = resample_to_48k_mono(raw, sr, ch)
        if not pcm48:
            return None
        if not voice or os.getenv("ECHO_HA_SAY_VC", "1").strip().lower() in ("0", "false", "no", ""):
            return pcm48
        # Seed-VC: 24 kHz in -> 22050 out, then resample to 48 kHz
        import asyncio as _aio
        from websockets.asyncio.client import connect

        async def _vc(pcm48_in):
            # downsample 48k -> 24k for Seed-VC
            a48 = pcm48_in
            step = 2
            a24 = _array.array("h", a48[::step])
            out22 = bytearray()
            async with connect(_VC_WS, open_timeout=5, close_timeout=2,
                               max_size=8 * 1024 * 1024, proxy=None) as vc:
                await vc.send(_json.dumps({"type": "reset"}))
                b = a24.tobytes()
                chunk = 24000 // 10 * 2
                for i in range(0, len(b), chunk):
                    await vc.send(_json.dumps({
                        "type": "audio", "mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(b[i:i + chunk]).decode()}))
                    await _aio.sleep(0.005)
                tail = b"\x00" * (24000 * 2 // 2)
                await vc.send(_json.dumps({
                    "type": "audio", "mimeType": "audio/pcm;rate=24000",
                    "data": base64.b64encode(tail).decode()}))
                last = time.time()
                while time.time() - last < 1.5:
                    try:
                        m = await _aio.wait_for(vc.recv(), timeout=0.3)
                    except _aio.TimeoutError:
                        continue
                    if not isinstance(m, str):
                        continue
                    j = _json.loads(m)
                    if j.get("type") == "audio" and j.get("data"):
                        out22.extend(base64.b64decode(j["data"]))
                        last = time.time()
            return bytes(out22)

        try:
            out22 = asyncio.run(_vc(pcm48))
        except Exception:  # noqa: BLE001
            return pcm48
        if not out22:
            return pcm48
        return resample_to_48k_mono(out22, 22050, 1) or pcm48
    except Exception:  # noqa: BLE001
        return None


def _tone(freq: float, ms: float, sr: int = 48000, amp: float = 0.22, fade_ms: float = 5.0):
    n = int(sr * ms / 1000.0)
    f = max(1, int(sr * fade_ms / 1000.0))
    a = array.array("h", bytes(n * 2))
    w = 2.0 * math.pi * freq / sr
    for i in range(n):
        if i < f:
            env = i / f
        elif i >= n - f:
            env = (n - 1 - i) / f
        else:
            env = 1.0
        if env < 0.0:
            env = 0.0
        a[i] = int(amp * 32767.0 * env * math.sin(w * i))
    return a


def _sil(ms: float, sr: int = 48000):
    return array.array("h", bytes(int(sr * ms / 1000.0) * 2))


# Simulated "AI" feedback tones (48 kHz mono PCM16), one per interaction cue.
_CHIME_SPECS = {
    "listen": [(880, 100), ("s", 30), (1320, 120)],       # one-shot listening
    "question": [(660, 90), ("s", 25), (990, 110)],       # question active
    "live": [(660, 80), ("s", 20), (880, 80), ("s", 20), (1320, 130)],  # live mode
    "idle": [(520, 90), ("s", 25), (360, 140)],           # return to idle
    "interrupt": [(300, 70)],                             # cancel / barge cue
    "muted": [(240, 60)],                                 # soft muted blip
    "error": [(300, 120), ("s", 40), (300, 120)],         # recovery
}


def make_chime(kind: str):
    spec = _CHIME_SPECS.get(kind, _CHIME_SPECS["listen"])
    out = array.array("h")
    for item in spec:
        if item[0] == "s":
            out.extend(_sil(item[1]))
        else:
            out.extend(_tone(item[0], item[1]))
    return out


class EchoController:
    IDLE = "IDLE"
    LISTEN_ONE_SHOT = "LISTEN_ONE_SHOT"
    QUESTION_ACTIVE = "QUESTION_ACTIVE"
    LIVE_MODE = "LIVE_MODE"
    LIVE_MUTED = "LIVE_MUTED"
    ERROR_RECOVERY = "ERROR_RECOVERY"
    ANNOUNCING = "ANNOUNCING"     # HA play_media audio playing on the downlink
    STATES = (IDLE, LISTEN_ONE_SHOT, QUESTION_ACTIVE, LIVE_MODE, LIVE_MUTED,
              ERROR_RECOVERY, ANNOUNCING)

    def __init__(self, sink=None, volume: int = 73, log: Optional[Callable[[str], None]] = None):
        self.state = self.IDLE
        self.volume = int(volume)
        self._vol_exact = float(self.volume)
        self._announce_until = 0.0
        self._radio_start = None    # set by echo_server: callable(url)->bool
        self._radio_stop = None     # set by echo_server: callable()->bool
        self.now_playing = ""       # human-readable title for HA (radio/track/text)
        self.mic_muted = False
        self.interrupted = False
        self._pre_mute_state = None
        self._post_mute_target = None
        self._sink = sink
        self._log = log or (lambda m: print(m, flush=True))
        self._control_writers = []  # Echo control connections (for server->Echo)
        self._listeners = []        # Phase 5D: state-change observers
        self._led = None            # Phase 5D.5: LedProjector (semantic LED layer)
        self._oneshot = None        # Phase 5G: one-shot orchestrator (push-to-talk end)

    # ---- plumbing -----------------------------------------------------------
    def attach_led(self, led) -> None:
        """Attach the Phase 5D.5 semantic LED projector.  The controller stays
        authoritative; higher layers only emit semantic events."""
        self._led = led

    def attach_oneshot(self, orch) -> None:
        """Attach the Phase 5G one-shot orchestrator so a second Action press can
        end a push-to-talk capture and process it (instead of cancelling)."""
        self._oneshot = orch

    def detach_oneshot(self, orch=None) -> None:
        if orch is None or self._oneshot is orch:
            self._oneshot = None

    def add_listener(self, cb) -> None:
        """Register cb(old_state, new_state, reason) invoked on every transition."""
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _notify(self, old: str, new: str, reason: str) -> None:
        for cb in list(self._listeners):
            try:
                cb(old, new, reason)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[control] listener error: {type(exc).__name__}: {exc}")

    def set_sink(self, sink) -> None:
        """Register the active audio session's downlink sink (enqueue(pcm))."""
        self._sink = sink

    def add_control_writer(self, writer) -> None:
        self._control_writers.append(writer)

    def remove_control_writer(self, writer) -> None:
        if writer in self._control_writers:
            self._control_writers.remove(writer)

    def send_to_echo(self, msg_type: int, payload: bytes = b"") -> int:
        n = 0
        for w in list(self._control_writers):
            try:
                w.write(HDR.pack(MAGIC, msg_type, 0, 0, len(payload)) + payload)
                n += 1
            except Exception:
                self.remove_control_writer(w)
        return n

    def _chime(self, kind: str) -> None:
        if self._sink is not None:
            try:
                self._sink.enqueue(make_chime(kind))
            except Exception as exc:  # noqa: BLE001
                self._log(f"[control] chime {kind} failed: {type(exc).__name__}: {exc}")

    def play_chime(self, kind: str) -> None:
        """Public chime trigger (used by the echo_play_chime Hermes tool)."""
        self._chime(kind)

    # ---- Phase 5D.5 semantic LED layer --------------------------------------
    def led_set_state(self, state: str, source: str = "controller") -> None:
        if self._led is not None:
            self._led.set_state(state, source=source)

    def led_flash(self, kind: str = "interrupt", ms: int = 180) -> None:
        if self._led is not None:
            self._led.flash(kind, ms=ms)

    def response_generation_started(self) -> None:
        """Question accepted, answer being produced -> LED_THINKING."""
        if self._led is not None:
            self._led.response_generation_started()

    def response_generation_done(self) -> None:
        if self._led is not None:
            self._led.response_generation_done()

    def playback_started(self) -> None:
        """Answer audio actually started -> LED_SPEAKING."""
        if self._led is not None:
            self._led.playback_started()

    def playback_finished(self) -> None:
        """Answer audio drained -> restore controller-derived base state."""
        if self._led is not None:
            self._led.playback_finished()

    def interaction_interrupted(self) -> None:
        """Semantic cancellation cue -> short interrupt flash."""
        if self._led is not None:
            self._led.interaction_interrupted()

    def led_status(self):
        return self._led.status() if self._led is not None else None

    def led_demo(self, seconds: float = 20.0) -> None:
        """Showcase animation (test aid)."""
        if self._led is not None:
            self._led.start_demo(seconds)

    def led_demo_stop(self) -> None:
        if self._led is not None:
            self._led.stop_demo()

    def led_set_mode(self, mode: str):
        if self._led is not None:
            return self._led.owner_mgr.set_mode(mode)
        return None

    def led_ownership(self):
        return self._led.owner_mgr.status() if self._led is not None else None

    def _go(self, new: str, reason: str, chime: Optional[str] = None) -> None:
        old = self.state
        self.state = new
        self._log(f"[state] {old} -> {new} reason={reason}")
        if chime:
            self._chime(chime)
        self._notify(old, new, reason)

    def _reject(self, target: str, reason: str) -> None:
        self._log(f"[state] REJECT {self.state} -> {target} reason={reason} (invalid)")

    # ---- API ----------------------------------------------------------------
    def get_state(self) -> str:
        return self.state

    def is_mic_muted(self) -> bool:
        return self.mic_muted

    def wake(self, source: str = "wakeword") -> bool:
        if self.mic_muted:
            self._reject(self.LISTEN_ONE_SHOT, f"{source},muted")
            return False
        if self.state == self.IDLE:
            self._go(self.LISTEN_ONE_SHOT, source, chime="listen")
            return True
        self._reject(self.LISTEN_ONE_SHOT, source)
        return False

    def enter_question_mode(self, reason: str = "test_command") -> bool:
        if self.mic_muted:
            self._reject(self.QUESTION_ACTIVE, f"{reason},muted")
            return False
        if self.state == self.LISTEN_ONE_SHOT:
            self._go(self.QUESTION_ACTIVE, reason, chime="question")
            return True
        self._reject(self.QUESTION_ACTIVE, reason)
        return False

    def enter_live_mode(self, reason: str = "test_command") -> bool:
        if self.mic_muted:
            self._reject(self.LIVE_MODE, f"{reason},muted")
            return False
        if self.state in (self.LISTEN_ONE_SHOT, self.QUESTION_ACTIVE):
            self._go(self.LIVE_MODE, reason, chime="live")
            return True
        self._reject(self.LIVE_MODE, reason)
        return False

    def exit_live_mode(self, reason: str = "test_command") -> bool:
        if self.state == self.LIVE_MODE:
            self._go(self.IDLE, reason, chime="idle")
            return True
        if self.state == self.LIVE_MUTED:
            # cannot leave the privacy state by software; arm exit for unmute
            self._post_mute_target = self.IDLE
            self._log(f"[state] LIVE_MUTED exit-pending reason={reason} (mute has priority)")
            return True
        self._reject(self.IDLE, reason)
        return False

    def interrupt(self, reason: str = "interrupt") -> bool:
        if self.mic_muted:
            self._reject("INTERRUPT", f"{reason},muted")
            return False
        if self.state in (self.LISTEN_ONE_SHOT, self.QUESTION_ACTIVE):
            self._go(self.IDLE, reason, chime="interrupt")
            self.interaction_interrupted()
            return True
        if self.state == self.LIVE_MODE:
            self.interrupted = True
            self._log(f"[state] LIVE_MODE interrupt reason={reason} (marked interrupted)")
            self.interaction_interrupted()
            return True
        self._reject("INTERRUPT", reason)
        return False

    def set_volume(self, value: int, source: str = "api") -> int:
        try:
            v = int(round(float(value)))
        except Exception:
            self._reject(f"VOLUME {value!r}", source)
            return self.volume
        v = max(0, min(int(round(VOLUME_MAX)), v))
        self._vol_exact = float(v)
        return self._apply_volume(v, source)

    def _apply_volume(self, v: int, source: str) -> int:
        old = self.volume
        self.volume = v
        self._log(f"[control] volume old={old} new={v} source={source}")
        # In raw/endpoint mode the stock audio path is bypassed, so the Echo
        # control daemon always applies the level (tinymix PCM Playback Volume).
        self.send_to_echo(T_SET_VOLUME, struct.pack("<B", v))
        return v

    def set_physical_mute(self, muted: bool) -> bool:
        muted = bool(muted)
        if muted:
            if self.mic_muted:
                return True
            self._pre_mute_state = self.state
            self.mic_muted = True
            prev = self.state
            self.state = self.LIVE_MUTED
            self._log(f"[state] {prev} -> LIVE_MUTED reason=physical_mute")
            self._notify(prev, self.LIVE_MUTED, "physical_mute")
            return True
        if not self.mic_muted:
            return False  # already unmuted; only the hardware can change physical state
        self.mic_muted = False
        target = self.IDLE
        if self._post_mute_target is None and self._pre_mute_state == self.LIVE_MODE:
            target = self.LIVE_MODE
        self._pre_mute_state = None
        self._post_mute_target = None
        prev = self.state
        self.state = target
        self._log(f"[state] {prev} -> {target} reason=physical_unmute")
        if target == self.LIVE_MODE:
            self._chime("live")
        self._notify(prev, target, "physical_unmute")
        return True

    # ---- physical buttons ---------------------------------------------------
    def action_button(self, hold_ms: int) -> bool:
        """Action button release. hold_ms >= ACTION_LONG_MS = long press."""
        if self.mic_muted:
            self._reject("ACTION", f"long={hold_ms >= ACTION_LONG_MS},muted")
            return False
        if hold_ms >= ACTION_LONG_MS:
            if self.state == self.IDLE:
                self._log("[state] IDLE action long-press reason=action_long_press (no-op)")
                return True
            self._go(self.IDLE, "action_long_press", chime="idle")
            self.interaction_interrupted()
            return True
        # short press
        if self.state == self.IDLE:
            self._go(self.LISTEN_ONE_SHOT, "action_short_press", chime="listen")
            return True
        if self.state == self.LISTEN_ONE_SHOT:
            # Phase 5G push-to-talk: a second Action press ends the utterance and
            # processes it (instead of cancelling), so the user — not the VAD
            # trailing-silence timer — decides when they are done speaking.
            if self._oneshot is not None and self._oneshot.finish_push_to_talk():
                return True
            self._go(self.IDLE, "action_short_press", chime="interrupt")
            self.interaction_interrupted()
            return True
        if self.state == self.QUESTION_ACTIVE:
            self._go(self.IDLE, "action_short_press", chime="interrupt")
            self.interaction_interrupted()
            return True
        if self.state == self.LIVE_MODE:
            self.interrupted = True
            self._log("[state] LIVE_MODE interrupt reason=action_short_press (marked interrupted)")
            return True
        self._reject("ACTION", "action_short_press")
        return False

    def volume_button(self, direction: int, value: Optional[int] = None) -> None:
        """Physical volume button observed (Phase 5G).

        Raw/endpoint mode bypasses the stock audio path, so Hiyo owns the volume:
        move the level by one step, apply it (server -> device tinymix), play the
        stock volume tone and show the stock ring indicator."""
        if value is not None:
            self.set_volume(value, "physical_button")
        else:
            self._vol_exact = max(0.0, min(VOLUME_MAX, self._vol_exact + direction * VOLUME_STEP))
            self._apply_volume(int(round(self._vol_exact)), "physical_button")
        self.play_volume_tone()
        if self._led is not None:
            self._led.show_volume(self.volume)
        self._log(f"[control] volume button {'up' if direction > 0 else 'down'} -> {self.volume}")

    def play_volume_tone(self) -> None:
        """Play the stock volume-adjust tone through the active downlink sink."""
        if self._sink is None:
            return
        tone = load_volume_tone()
        if not tone:
            return
        try:
            self._sink.enqueue(tone)
            self._log(f"[control] volume tone queued ({len(tone)/48:.0f}ms)")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[control] volume tone failed: {type(exc).__name__}: {exc}")

    def set_error(self, reason: str = "test") -> None:
        self._go(self.ERROR_RECOVERY, reason, chime="error")

    def _queue_pcm(self, pcm) -> bool:
        """Enqueue decoded 48 kHz mono PCM into the active downlink sink."""
        sink = self._sink
        if sink is None:
            return False
        if hasattr(sink, "enqueue"):
            sink.enqueue(pcm)
        elif hasattr(sink, "push_48k"):
            sink.push_48k(pcm.tobytes())
        else:
            return False
        return True

    def announce(self, url_or_path: str) -> bool:
        """Fetch a media URL/path and play it through the active downlink sink.

        Used by the HA media_player `play_media` action.  Returns True if audio
        was queued.  The Echo keeps its normal AEC/downlink path, so this is
        safe while idle.  The controller enters ANNOUNCING (mapped to `playing`
        in HA) and returns to IDLE when the audio has had time to drain.
        """
        if self._sink is None:
            self._log("[control] announce: no sink")
            return False
        pcm = load_media_48k(url_or_path)
        if not pcm:
            self._log(f"[control] announce: could not decode {url_or_path[:80]}")
            return False
        try:
            if not self._queue_pcm(pcm):
                self._log(f"[control] announce: sink {type(self._sink).__name__} unusable")
                return False
            dur = len(pcm) / 48000.0
            self._log(f"[control] announce queued {dur:.1f}s from {url_or_path[:60]}")
            self._announce_until = time.time() + dur + 0.3
            self.now_playing = _media_label(url_or_path)
            if self.state == self.IDLE:
                self._go(self.ANNOUNCING, "announce", chime=None)
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"[control] announce failed: {type(exc).__name__}: {exc}")
            return False

    def say(self, text: str) -> bool:
        """Speak `text` through the Hiyo voice (Breeze TTS -> Seed-VC -> downlink).

        Used by HA TTS announcements.  Runs synchronously (call from an executor
        or the admin handler which already runs off the audio loop).
        """
        text = (text or "").strip()
        if not text or self._sink is None:
            self._log("[control] say: empty text or no sink")
            return False
        pcm = synth_say_48k(text)
        if not pcm:
            self._log("[control] say: synthesis failed")
            return False
        try:
            if not self._queue_pcm(pcm):
                return False
            dur = len(pcm) / 48000.0
            self._log(f"[control] say queued {dur:.1f}s: {text[:40]!r}")
            self._announce_until = time.time() + dur + 0.3
            self.now_playing = text[:120]
            if self.state == self.IDLE:
                self._go(self.ANNOUNCING, "say", chime=None)
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"[control] say failed: {type(exc).__name__}: {exc}")
            return False

    def tick(self) -> None:
        """Called periodically: leave ANNOUNCING once the audio has drained."""
        if self.state == self.ANNOUNCING and self._announce_until and time.time() >= self._announce_until:
            self._announce_until = 0.0
            self.now_playing = ""
            self.response_complete("announce_done")

    def attach_radio(self, start_cb, stop_cb) -> None:
        """echo_server registers the radio start/stop implementations."""
        self._radio_start = start_cb
        self._radio_stop = stop_cb

    def stream(self, url: str) -> bool:
        """Start a live radio stream (no auto-end; stop with `stopstream`)."""
        if self._radio_start is None:
            self._log("[control] stream: not wired")
            return False
        ok = bool(self._radio_start(url))
        if ok:
            self.now_playing = _media_label(url)
        return ok

    def stop_stream(self) -> bool:
        if self._radio_stop is None:
            return False
        self._announce_until = 0.0
        self.now_playing = ""
        return bool(self._radio_stop())

    def enter_announcing(self, reason: str = "announce") -> None:
        """Enter ANNOUNCING for an open-ended stream (no automatic timer)."""
        self._announce_until = 0.0
        if self.state == self.IDLE:
            self._go(self.ANNOUNCING, reason, chime=None)

    def exit_announcing(self, reason: str = "announce_stop") -> None:
        if self.state == self.ANNOUNCING:
            self.response_complete(reason)

    def recover(self, reason: str = "recover") -> None:
        if self.state == self.ERROR_RECOVERY:
            self._go(self.IDLE, reason, chime="idle")
        else:
            self._reject(self.IDLE, reason)

    # ---- Phase 5D one-shot lifecycle ---------------------------------------
    def response_complete(self, reason: str = "response_complete") -> bool:
        """QUESTION_ACTIVE/LISTEN_ONE_SHOT -> IDLE after playback fully drained.
        No chime: completion is silent (the spoken answer already ended)."""
        if self.mic_muted:
            self._reject(self.IDLE, f"{reason},muted")
            return False
        if self.state in (self.QUESTION_ACTIVE, self.LISTEN_ONE_SHOT, self.ANNOUNCING):
            self._go(self.IDLE, reason)
            return True
        self._reject(self.IDLE, reason)
        return False

    def abort_on_mute(self) -> None:
        """When an interaction is cancelled because of a physical mute, make the
        eventual unmute land in IDLE instead of resurrecting the dead state."""
        if self.mic_muted:
            self._post_mute_target = self.IDLE
            self._log("[state] LIVE_MUTED exit-pending reason=interaction_aborted")


# --- control connection handler (Echo -> server) ------------------------------
async def handle_control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         controller: EchoController) -> None:
    peer = writer.get_extra_info("peername")
    controller.add_control_writer(writer)
    controller._log(f"[control] Echo control daemon connected: {peer}")
    try:
        while True:
            header = await reader.readexactly(HDR.size)
            magic, msg_type, flags, seq, length = HDR.unpack(header)
            if magic != MAGIC:
                controller._log(f"[control] bad magic {magic:#06x}; dropping {peer}")
                break
            payload = await reader.readexactly(length) if length else b""
            if msg_type == T_CONTROL_HELLO:
                controller._log(f"[control] HELLO from {peer}")
            elif msg_type == T_BUTTON_ACTION_DOWN:
                controller._log("[control] action DOWN")
            elif msg_type == T_BUTTON_ACTION_UP:
                hold = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                controller._log(f"[control] action UP hold={hold}ms")
                controller.action_button(hold)
            elif msg_type == T_BUTTON_VOLUME_UP:
                controller._log("[control] volume UP (physical_button)")
                controller.volume_button(+1)
            elif msg_type == T_BUTTON_VOLUME_DOWN:
                controller._log("[control] volume DOWN (physical_button)")
                controller.volume_button(-1)
            elif msg_type == T_MIC_MUTE_STATE:
                muted = bool(payload[0]) if payload else False
                controller.set_physical_mute(muted)
            elif msg_type == T_VOLUME_VALUE:
                if payload:
                    controller.set_volume(payload[0], "physical_button")
            elif msg_type == T_LED_OWNER_STATE:
                owner = payload[0] if payload else 0
                mode = payload[1] if len(payload) > 1 else 0
                controller._log(f"[led-owner] device owner={'HIYO' if owner else 'STOCK'} "
                                f"mode={'exclusive' if mode else 'cooperative'}")
                if controller._led is not None:
                    controller._led.owner_mgr.on_device_state(owner, mode)
            else:
                controller._log(f"[control] unknown type {msg_type:#04x}")
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001
        controller._log(f"[control] error: {type(exc).__name__}: {exc}")
    finally:
        controller.remove_control_writer(writer)
        controller._log(f"[control] Echo control daemon disconnected: {peer}")
        if not controller._control_writers and controller._led is not None:
            # no more device control channel: hand the ring back to stock
            controller._led.owner_mgr.ensure_stock("control_loss")
        try:
            writer.close()
        except Exception:
            pass


# --- text admin console (tests / future Hermes API) ---------------------------
_ADMIN_HELP = (
    "commands: state | wake [src] | question | live | exit [reason] | interrupt |"
    " volume <n> [src] | mute <0|1> | action <short|long> | announce <url> | error | recover |"
    " led <state> | ledflash <kind> | ledsuspend | ledresume | ledstatus |"
    " ledevent <generation_started|generation_done|playback_started|playback_finished|interrupt> |"
    " leddemo <secs> | leddemostop | ledmode <cooperative|exclusive> | ledowner |"
    " context | prompt | route <text> | turn <text> | tool <name> [json] | help | quit"
)


async def handle_admin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       controller: EchoController) -> None:
    async def send(s: str):
        writer.write((s + "\n").encode())
        await writer.drain()

    def eh() -> EchoHermes:
        return EchoHermes(controller, log=controller._log)

    await send(f"echo-controller admin ready. {_ADMIN_HELP}")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            raw = line.decode(errors="replace").strip()
            parts = raw.split()
            if not parts:
                continue
            cmd, args = parts[0].lower(), parts[1:]
            if cmd == "state":
                await send(f"state={controller.get_state()} volume={controller.volume} "
                           f"muted={int(controller.mic_muted)} interrupted={int(controller.interrupted)} "
                           f"playing={controller.now_playing}")
            elif cmd == "wake":
                await send(f"ok={controller.wake(args[0] if args else 'wakeword')}")
            elif cmd == "question":
                await send(f"ok={controller.enter_question_mode(args[0] if args else 'test_command')}")
            elif cmd == "live":
                await send(f"ok={controller.enter_live_mode(args[0] if args else 'test_command')}")
            elif cmd == "exit":
                await send(f"ok={controller.exit_live_mode(args[0] if args else 'test_command')}")
            elif cmd == "interrupt":
                await send(f"ok={controller.interrupt()}")
            elif cmd == "volume":
                try:
                    v = int(args[0])
                except Exception:
                    await send("err usage: volume <n>")
                    continue
                await send(f"ok={controller.set_volume(v, args[1] if len(args) > 1 else 'api')}")
            elif cmd == "mute":
                await send(f"ok={controller.set_physical_mute(args and args[0] == '1')}")
            elif cmd == "action":
                kind = args[0] if args else "short"
                await send(f"ok={controller.action_button(ACTION_LONG_MS if kind == 'long' else 100)}")
            elif cmd == "error":
                controller.set_error()
                await send("ok=True")
            elif cmd == "recover":
                controller.recover()
                await send("ok=True")
            elif cmd == "announce":
                # usage: announce <url-or-path>   (media decoded via ffmpeg)
                target = raw.split(None, 1)[1].strip() if len(args) >= 1 else ""
                if not target:
                    await send("err usage: announce <url-or-path>")
                    continue
                loop = asyncio.get_running_loop()
                ok = await loop.run_in_executor(None, controller.announce, target)
                await send(f"ok={ok}")
            elif cmd == "tick":
                controller.tick()
                await send("ok=True")
            elif cmd == "say":
                # usage: say <text>   (Breeze TTS + Seed-VC -> downlink)
                target = raw.split(None, 1)[1].strip() if len(args) >= 1 else ""
                if not target:
                    await send("err usage: say <text>")
                    continue
                # Synthesis blocks; run it off the event loop.
                loop = asyncio.get_running_loop()
                ok = await loop.run_in_executor(None, controller.say, target)
                await send(f"ok={ok}")
            elif cmd == "stream":
                # usage: stream <url>   (live radio -> downlink, no auto-end)
                target = raw.split(None, 1)[1].strip() if len(args) >= 1 else ""
                if not target:
                    await send("err usage: stream <url>")
                    continue
                await send(f"ok={controller.stream(target)}")
            elif cmd == "stopstream":
                await send(f"ok={controller.stop_stream()}")
            elif cmd == "led":
                if not args:
                    await send(f"state={controller.led_status()}")
                else:
                    controller.led_set_state(args[0], source="admin")
                    await send(f"ok=True state={controller.led_status()}")
            elif cmd == "ledflash":
                controller.led_flash(args[0] if args else "interrupt")
                await send("ok=True")
            elif cmd == "ledevent":
                ev = args[0] if args else ""
                if ev == "generation_started":
                    controller.response_generation_started()
                elif ev == "generation_done":
                    controller.response_generation_done()
                elif ev == "playback_started":
                    controller.playback_started()
                elif ev == "playback_finished":
                    controller.playback_finished()
                elif ev == "interrupt":
                    controller.interaction_interrupted()
                else:
                    await send(f"err unknown ledevent '{ev}'")
                    continue
                await send(f"ok=True state={controller.led_status()}")
            elif cmd == "ledsuspend":
                if controller._led is not None:
                    controller._led.suspend_for_physical_mute(reason="admin")
                await send("ok=True")
            elif cmd == "ledresume":
                if controller._led is not None:
                    controller._led.resume_after_physical_mute()
                await send("ok=True")
            elif cmd == "ledstatus":
                await send("json " + json.dumps({"led": controller.led_status()}))
            elif cmd == "leddemo":
                try:
                    secs = float(args[0]) if args else 20.0
                except Exception:
                    secs = 20.0
                controller.led_demo(secs)
                await send(f"ok=True demo_seconds={secs}")
            elif cmd == "leddemostop":
                controller.led_demo_stop()
                await send("ok=True")
            elif cmd == "ledmode":
                await send(f"mode={controller.led_set_mode(args[0] if args else 'cooperative')}")
            elif cmd == "ledowner":
                await send("json " + json.dumps({"ownership": controller.led_ownership()}))
            elif cmd == "context":
                await send("json " + json.dumps({
                    "context": eh().context(),
                    "context_dict": runtime_context_dict(controller),
                    "state": controller.get_state(),
                }))
            elif cmd == "prompt":
                await send("json " + json.dumps({"prompt": ECHO_PROMPT_FRAGMENT}))
            elif cmd == "route":
                await send("json " + json.dumps(eh().route(raw[len("route"):].strip())))
            elif cmd == "turn":
                await send("json " + json.dumps(eh().turn(raw[len("turn"):].strip())))
            elif cmd == "tool":
                if not args:
                    await send("err usage: tool <name> [json-args]")
                    continue
                name = args[0]
                argo = raw.split(name, 1)[1].strip() if name in raw else ""
                try:
                    a = json.loads(argo) if argo else {}
                except Exception:
                    a = {}
                await send("json " + json.dumps(dispatch_tool(controller, name, a)))
            elif cmd == "help":
                await send(_ADMIN_HELP)
            elif cmd in ("quit", "exit_console"):
                break
            else:
                await send(f"err unknown command '{cmd}'. {_ADMIN_HELP}")
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass

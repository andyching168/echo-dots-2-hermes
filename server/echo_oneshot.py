#!/usr/bin/env python3
"""Phase 5D — Echo one-shot Question Mode orchestrator.

Lifecycle:
    IDLE --wake/action--> LISTEN_ONE_SHOT --VAD utterance--> STT --> EchoRouter
      --question--> QUESTION_ACTIVE --> Hermes(platform=echo) --> text
      --> Breeze TTS --> Seed-VC (Hiyori) --> 48k downlink --> drain
      --> QUESTION_ACTIVE -> IDLE reason=response_complete

Runs inside the echo_server process (same asyncio loop) so it shares the proven
AEC-clean uplink and the framed downlink. No new ALSA path; no GPT Live session.
"""

from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import os
import time
import urllib.request
import wave
from collections import deque

import numpy as np

from echo_hermes import EchoRouter

# ── defaults (recorded in the Phase 5D report) ───────────────────────────────
SR16 = 16000
SR48 = 48000
FRAME16 = 160                      # 10 ms
PREROLL_MS = 400
TRAILING_SILENCE_MS = 1000
HARD_MAX_MS = 15000
NO_SPEECH_MS = 10000
GUARD_MS = 800                     # skip acknowledgement chime at trigger
WAKE_TAIL_MAX_MS = 1200            # unused (kept for reference)
WAKE_TAIL_WINDOW_S = 3.0
MIN_QUERY_MS = 500                 # bursts shorter than this are not a question
# Phase 5G push-to-talk: when listening is triggered by one of these reasons, the
# VAD trailing-silence endpointing is disabled and the utterance runs until the
# Action button is pressed again (bounded by PTT_MAX_MS). Default: Action button.
PTT_REASONS = {r.strip() for r in os.getenv("ECHO_PTT_REASONS", "action_short_press").split(",") if r.strip()}
PTT_MAX_MS = int(float(os.getenv("ECHO_PTT_MAX_MS", "60000")))
PTT_MIN_MS = int(float(os.getenv("ECHO_PTT_MIN_MS", "300")))
PTT_GUARD_MS = int(float(os.getenv("ECHO_PTT_GUARD_MS", str(GUARD_MS))))  # skip trigger chime
DUMP_DIR = os.getenv("ECHO_ONESHOT_DUMP_DIR", os.path.expanduser("~/voice/echo-bridge/logs"))
ABS_FLOOR = 180                    # int16 RMS
NOISE_MULT = 2.5
THR_CAP = 600.0
HERMES_TIMEOUT = float(os.getenv("ECHO_HERMES_TIMEOUT", "240.0"))
TTS_TIMEOUT = 60.0
STT_TIMEOUT = 30.0
VC_TIMEOUT = 90.0
DRAIN_TIMEOUT = 60.0
DRAIN_TAIL_MS = 400

STT_URL = os.getenv("ECHO_STT_URL", "http://127.0.0.1:8797/transcribe")
TURN_URL = os.getenv("ECHO_TURN_URL", "http://127.0.0.1:8796/turn")
TURN_BASE = TURN_URL.rsplit("/", 1)[0]
CANCEL_URL = os.getenv("ECHO_TURN_CANCEL_URL", f"{TURN_BASE}/cancel")
GADGET_DIRECT = os.getenv("ECHO_GADGET_DIRECT", "1").strip().lower() not in ("0", "false", "no", "off", "")
GADGET_LOCAL_HOST = os.getenv("ECHO_GADGET_LOCAL_HOST", "127.0.0.1")
GADGET_LOCAL_PORT = int(os.getenv("ECHO_GADGET_LOCAL_PORT", "8798"))
SESSION_KEY = os.getenv("ECHO_QSESSION_KEY", "echo")
BREEZE_URL = os.getenv("ECHO_BREEZE_URL", "http://127.0.0.1:10901/synthesize")
HERMES_TTS_URL = os.getenv("ECHO_HERMES_TTS_URL", "http://127.0.0.1:8795/synthesize")
VC_WS = os.getenv("ECHO_VC_WS", "ws://127.0.0.1:8790")

# Spoken filler: so the user knows the request was heard while Hermes works.
FILLER_ENABLED = os.getenv("ECHO_FILLER", "1").strip().lower() not in ("0", "false", "no", "off", "")
FILLER_TEXT = os.getenv("ECHO_FILLER_TEXT", "好的，我看看。")


# ── downlink source: bounded 48 kHz mono, flushable, drain-aware ─────────────
class OneshotSource:
    label = "oneshot"

    def __init__(self, cap_frames: int = 9000):
        self._buf = bytearray()
        self._cap = cap_frames * 480 * 2
        self.pushed_bytes = 0
        self.underruns = 0
        self.dropped_bytes = 0
        self.playing = False

    def enqueue(self, pcm: array.array) -> None:
        self.push_48k(pcm.tobytes())

    def push_48k(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        self.pushed_bytes += len(pcm)
        if len(self._buf) > self._cap:
            drop = len(self._buf) - self._cap
            del self._buf[:drop]
            self.dropped_bytes += drop

    def pending_samples(self) -> int:
        return len(self._buf) // 2

    def flush(self) -> None:
        self._buf.clear()
        self.playing = False

    def next_frame(self):
        need = 480 * 2
        if len(self._buf) >= need:
            raw = bytes(self._buf[:need])
            del self._buf[:need]
            self.playing = True
        else:
            raw = bytes(self._buf) + b"\x00" * (need - len(self._buf))
            if self._buf:
                self.underruns += 1
            self._buf.clear()
        a = array.array("h")
        a.frombytes(raw)
        return a

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()


def resample_pcm16(pcm: bytes, sr_in: int, sr_out: int) -> bytes:
    if sr_in == sr_out or not pcm:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n_out = int(round(len(x) * sr_out / sr_in))
    if n_out <= 0:
        return b""
    idx = np.arange(n_out, dtype=np.float64) * (sr_in / sr_out)
    i0 = idx.astype(np.int64)
    i1 = np.minimum(i0 + 1, len(x) - 1)
    frac = (idx - i0).astype(np.float32)
    y = x[i0] * (1.0 - frac) + x[i1] * frac
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


_WAKE_RE = None


def _strip_wake(text: str) -> str:
    """Drop a leading wake-phrase mis-transcription from the STT text."""
    global _WAKE_RE
    if _WAKE_RE is None:
        import re

        _WAKE_RE = re.compile(
            r"^\s*(okay|ok|hi\s*yo|okay\s+hi\s*yo|okay\s+y'?all|okay\s+yo|"
            r"ok\s+ayo|okay\s+hi)[\s,\.!\?、。，！？]*",
            re.IGNORECASE)
    return _WAKE_RE.sub("", text or "").strip()


def pcm16_to_wav(pcm: bytes, sr: int = SR16) -> bytes:
    b = io.BytesIO()
    w = wave.open(b, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm)
    w.close()
    return b.getvalue()


def wav_to_pcm16(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"unsupported sample width {sw}")
    if ch > 1:
        x = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).mean(axis=1)
        raw = np.clip(x, -32768, 32767).astype("<i2").tobytes()
    return raw, sr


def _trim_silence(pcm: bytes, thr: float = ABS_FLOOR) -> bytes:
    """Trim leading/trailing near-silence from a push-to-talk recording, keeping
    50 ms of padding either side.  Returns b'' if the whole clip is silent."""
    if not pcm:
        return b""
    x = np.frombuffer(pcm, dtype="<i2")
    n = len(x) // FRAME16
    if n == 0:
        return pcm
    y = x[: n * FRAME16].reshape(n, FRAME16).astype(np.float32)
    rms = np.sqrt((y ** 2).mean(axis=1))
    idx = np.where(rms >= thr)[0]
    if len(idx) == 0:
        return b""
    start = max(0, int(idx[0]) - 5)
    end = min(n, int(idx[-1]) + 6)
    return x[start * FRAME16: end * FRAME16].astype("<i2").tobytes()


# ── deterministic energy VAD ─────────────────────────────────────────────────
class VadCollector:
    def __init__(self, abs_floor=ABS_FLOOR):
        self.abs_floor = abs_floor
        self.noise = 60.0
        self.reset()

    def reset(self):
        self.preroll = deque(maxlen=int(PREROLL_MS / 10))
        self.utt = bytearray()
        self.in_speech = False
        self.silence = 0
        self.guard = int(GUARD_MS / 10)
        self.noise = 60.0

    def _thr(self):
        return max(self.abs_floor, min(self.noise * NOISE_MULT, THR_CAP))

    def feed(self, frame: bytes) -> dict | None:
        """frame = 10 ms PCM16 16k. Returns a dict on events, else None."""
        x = np.frombuffer(frame, dtype="<i2")
        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if len(x) else 0.0
        if self.guard > 0:
            self.guard -= 1
            if rms < self.abs_floor:
                self.noise = 0.9 * self.noise + 0.1 * rms
            return None
        thr = self._thr()
        if not self.in_speech:
            self.preroll.append(bytes(frame))
            if rms < self.abs_floor:
                self.noise = 0.9 * self.noise + 0.1 * rms
            if rms >= thr:
                self.in_speech = True
                self.silence = 0
                self.utt = bytearray()
                for f in self.preroll:
                    self.utt.extend(f)
                return {"event": "speech_start", "thr": int(thr), "rms": int(rms)}
            return None
        # in speech
        self.utt.extend(frame)
        if rms < thr:
            self.silence += 1
        else:
            self.silence = 0
        if self.silence * 10 >= TRAILING_SILENCE_MS:
            return self._finish("silence")
        if len(self.utt) // 2 * 1000 // SR16 >= HARD_MAX_MS:
            return self._finish("max")
        return None

    def force_end(self):
        if self.in_speech and self.utt:
            return self._finish("forced")
        return None

    def _finish(self, why):
        pcm = bytes(self.utt[: -(self.silence * FRAME16 * 2)]) if self.silence else bytes(self.utt)
        dur = len(pcm) / 2 / SR16
        self.in_speech = False
        self.utt = bytearray()
        self.preroll.clear()
        return {"event": "speech_end", "pcm": pcm, "duration": dur, "why": why}


# ── HTTP helpers (urllib in executor; no extra deps) ─────────────────────────
async def _http_post(url: str, payload: dict, timeout: float) -> bytes:
    loop = asyncio.get_running_loop()

    def _do() -> bytes:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    return await loop.run_in_executor(None, _do)


async def _gadget_local(payload: dict, timeout: float) -> dict:
    """Call the local Gadget shadow bridge directly over newline-delimited JSON."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(GADGET_LOCAL_HOST, GADGET_LOCAL_PORT), timeout=min(timeout, 5.0))
    try:
        writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not raw:
            raise RuntimeError("empty Gadget bridge response")
        return json.loads(raw.decode("utf-8"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def direct_tts(text: str) -> tuple[bytes, int]:
    """Project-specific direct HTTP TTS (historically Breeze)."""
    wav = await _http_post(BREEZE_URL, {"text": text}, TTS_TIMEOUT)
    return wav_to_pcm16(wav)


async def hermes_tts(text: str) -> tuple[bytes, int]:
    """Use Hermes' configured ``tts.provider`` through the localhost bridge."""
    wav = await _http_post(HERMES_TTS_URL, {"text": text}, TTS_TIMEOUT)
    return wav_to_pcm16(wav)


async def seed_vc(pcm24: bytes, on_chunk=None) -> bytes:
    """Stream 24 kHz PCM through the Seed-VC server; return 22050 Hz PCM.
    ``on_chunk(bytes)`` (optional) is called with each 22050 chunk as it arrives,
    so the caller can start playback before the whole utterance is converted."""
    from websockets.asyncio.client import connect

    out = bytearray()
    last_rx = {"t": time.time()}
    input_done = {"v": False}
    async with connect(VC_WS, open_timeout=5, close_timeout=2, max_size=8 * 1024 * 1024,
                       proxy=None) as vc:
        await vc.send(json.dumps({"type": "reset"}))

        async def sender():
            step = 24000 // 10 * 2  # 100 ms
            for i in range(0, len(pcm24), step):
                chunk = pcm24[i:i + step]
                await vc.send(json.dumps({"type": "audio", "mimeType": "audio/pcm;rate=24000",
                                          "data": base64.b64encode(chunk).decode()}))
                await asyncio.sleep(0.01)
            # trailing silence so the model flushes the last phonemes
            tail = b"\x00" * (24000 * 2 // 2)  # 0.5 s
            for i in range(0, len(tail), step):
                await vc.send(json.dumps({"type": "audio", "mimeType": "audio/pcm;rate=24000",
                                          "data": base64.b64encode(tail[i:i + step]).decode()}))
                await asyncio.sleep(0.01)
            input_done["v"] = True

        send_task = asyncio.ensure_future(sender())
        deadline = time.time() + VC_TIMEOUT
        try:
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(vc.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    if input_done["v"] and (time.time() - last_rx["t"]) > 1.2:
                        break
                    continue
                if not isinstance(raw, str):
                    continue
                m = json.loads(raw)
                if m.get("type") == "audio" and m.get("data"):
                    chunk = base64.b64decode(m["data"])
                    out.extend(chunk)
                    last_rx["t"] = time.time()
                    if on_chunk is not None:
                        on_chunk(chunk)
        finally:
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass
    return bytes(out)


# ── orchestrator ─────────────────────────────────────────────────────────────
class OneShotOrchestrator:
    def __init__(self, controller, log=None, use_vc: bool = True, tts_backend: str = "direct"):
        self.ctl = controller
        self.use_vc = bool(use_vc)
        self.tts_backend = str(tts_backend or "direct").strip().lower()
        if self.tts_backend not in ("direct", "hermes"):
            raise ValueError(f"unsupported TTS backend: {self.tts_backend}")
        self.source = OneshotSource()
        self.router = EchoRouter()
        self._log = log or (lambda m: print(m, flush=True))
        self._vad = VadCollector()
        self._gen = 0
        self._capturing = False
        self._skip_next = False
        self._burst_start_s = 0.0
        self._tasks = set()
        self._watchdog = None
        self._t_wake = None
        self._metrics = {}
        self._playback_announced = False
        self._hermes_inflight = False
        self._live = None  # Phase 5E LiveModeSupervisor (attached by echo_server)
        self._live_active = False
        self._ptt = False
        self._ptt_buf = bytearray()
        self._ptt_max_ms = PTT_MAX_MS
        controller.add_listener(self._on_state)
        controller.attach_oneshot(self)   # Phase 5G: Action press can end a PTT capture

    def attach_live(self, supervisor) -> None:
        """Attach the Phase 5E persistent Live Mode supervisor."""
        self._live = supervisor

    def close(self):
        self.ctl.remove_listener(self._on_state)
        self.ctl.detach_oneshot(self)
        self._cancel()

    # ---- logging ----
    def _metrics_log(self, extra=""):
        self._log("[metrics] " + " ".join(f"{k}={v}" for k, v in self._metrics.items())
                  + (f" {extra}" if extra else ""))

    # ---- feed ----
    def feed(self, pcm16: bytes) -> None:
        if not self._capturing or self.ctl.is_mic_muted():
            return
        self._samples_fed += len(pcm16) // 2
        if self._ptt:
            # Push-to-talk: record everything between the two Action presses; the
            # VAD never ends the turn on its own (only the hard cap does).
            self._ptt_buf.extend(pcm16)
            if self._samples_fed * 1000 // SR16 >= self._ptt_max_ms:
                self._log(f"[ptt] hard cap {self._ptt_max_ms}ms reached")
                self._finish_ptt("max")
            return
        ev = self._vad.feed(pcm16)
        if ev is None:
            return
        if ev["event"] == "speech_start":
            self._burst_start_s = self._samples_fed / SR16
            self._metrics["speech_start_audio_s"] = self._burst_start_s
            self._log(f"[vad] speech_start audio_t={self._burst_start_s:.2f}s "
                      f"thr={ev.get('thr')} rms={ev.get('rms')}")
        elif ev["event"] == "speech_end":
            dur = ev["duration"]
            # Ignore the wake-phrase / chime / short-noise bursts: keep listening.
            if ev["why"] != "max" and dur * 1000 < MIN_QUERY_MS:
                self._log(f"[vad] ignore short burst duration={dur:.2f}s "
                          f"start={self._burst_start_s:.2f}s")
                self._vad.utt = bytearray()
                self._vad.preroll.clear()
                self._vad.in_speech = False
                self._vad.silence = 0
                self._vad.guard = 0
                if self._watchdog:
                    self._watchdog.cancel()
                self._watchdog = self._spawn(self._no_speech_watchdog(self._gen))
                return
            # Optional: discard a first wake-tail burst (disabled by default; the
            # 800 ms chime guard already covers the wake phrase).
            if (self._skip_next and ev["why"] != "max" and dur * 1000 < WAKE_TAIL_MAX_MS
                    and self._burst_start_s < WAKE_TAIL_WINDOW_S):
                self._log(f"[vad] discard wake tail duration={dur:.2f}s "
                          f"start={self._burst_start_s:.2f}s")
                self._skip_next = False
                self._vad.utt = bytearray()
                self._vad.preroll.clear()
                self._vad.in_speech = False
                self._vad.silence = 0
                self._vad.guard = 0
                if self._watchdog:
                    self._watchdog.cancel()
                self._watchdog = self._spawn(self._no_speech_watchdog(self._gen))
                return
            self._capturing = False
            if self._watchdog:
                self._watchdog.cancel()
                self._watchdog = None
            self._log(f"[vad] speech_end duration={dur:.2f}s why={ev['why']}")
            self._dump_utterance(ev["pcm"], dur)
            self._spawn(self._process(ev["pcm"], self._gen))

    def _dump_utterance(self, pcm: bytes, dur: float) -> None:
        try:
            os.makedirs(DUMP_DIR, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(DUMP_DIR, f"p5d_utt_{ts}.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR16); w.writeframes(pcm)
            self._log(f"[vad] utterance saved {path} ({dur:.2f}s)")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[vad] dump failed: {exc}")

    # ---- state listener ----
    def _on_state(self, old: str, new: str, reason: str) -> None:
        if new == self.ctl.LIVE_MODE:
            # Phase 5E: Live Mode owns the interaction; the one-shot pipeline
            # stands down completely (no VAD/STT/_fail clobbering the session).
            self._live_active = True
            self._cancel(flush=True)
            return
        if old == self.ctl.LIVE_MODE and new != self.ctl.LIVE_MODE:
            self._live_active = False
        if new == self.ctl.LISTEN_ONE_SHOT:
            self._start_capture(reason)
        elif new == self.ctl.LIVE_MUTED:
            self._log("[state] physical mute during interaction -> cancel + abort")
            self._cancel()
            self.ctl.abort_on_mute()
        elif (new in (self.ctl.IDLE, self.ctl.ERROR_RECOVERY)
              and old in (self.ctl.LISTEN_ONE_SHOT, self.ctl.QUESTION_ACTIVE)
              and reason != "response_complete"):
            self._cancel()

    # ---- lifecycle ----
    def _start_capture(self, reason: str) -> None:
        self._cancel(flush=False)  # keep the acknowledgement chime queued
        self._gen += 1
        self._ptt = reason in PTT_REASONS
        self._ptt_buf = bytearray()
        self._ptt_max_ms = PTT_MAX_MS
        self._vad.reset()
        self._capturing = True
        self._skip_next = False
        self._burst_start_s = 0.0
        self._samples_fed = 0
        self._t_wake = time.time()
        self._metrics = {"wake_t": time.strftime("%H:%M:%S")}
        # Push-to-talk has no "no speech" timeout: it waits for the second press
        # (bounded by PTT_MAX_MS).  VAD mode keeps the 10 s no-speech watchdog.
        self._watchdog = None if self._ptt else self._spawn(self._no_speech_watchdog(self._gen))
        self._log(f"[oneshot] listening (reason={reason} ptt={self._ptt}) gen={self._gen}")

    def finish_push_to_talk(self) -> bool:
        """Phase 5G: Action pressed again during a push-to-talk capture -> end the
        utterance and process it.  Returns True if a PTT capture was active."""
        if not self._ptt or not self._capturing:
            return False
        self._finish_ptt("action")
        return True

    def _finish_ptt(self, why: str) -> bool:
        """Finalise a push-to-talk capture (Action press or hard cap)."""
        # Skip the acknowledgement-chime guard (the same window the VAD path ignores)
        # so the trigger beep cannot pollute the STT input.
        skip = (PTT_GUARD_MS // 10) * FRAME16 * 2
        buf = bytes(self._ptt_buf)
        pcm = _trim_silence(buf[skip:] if len(buf) > skip else b"")
        self._ptt_buf = bytearray()
        self._capturing = False
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        dur = len(pcm) / 2 / SR16
        if not pcm or dur * 1000 < PTT_MIN_MS:
            self._log(f"[ptt] finish why={why}: no usable speech ({dur:.2f}s) -> idle")
            self.ctl.response_complete(f"ptt_{why}_empty")
            return False
        self._log(f"[ptt] finish why={why} duration={dur:.2f}s")
        self._dump_utterance(pcm, dur)
        self._spawn(self._process(pcm, self._gen))
        return True

    async def _no_speech_watchdog(self, gen: int) -> None:
        try:
            await asyncio.sleep(NO_SPEECH_MS / 1000.0)
            if gen != self._gen or not self._capturing:
                return
            self._capturing = False
            ev = self._vad.force_end()
            if ev is not None:
                self._log(f"[vad] force_end watchdog duration={ev['duration']:.2f}s")
                self._spawn(self._process(ev["pcm"], gen))
                return
            self._log(f"[vad] no speech within {NO_SPEECH_MS} ms -> recover")
            self._fail("no_speech", gen)
        except asyncio.CancelledError:
            pass

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel(self, flush: bool = True) -> None:
        self._gen += 1
        self._capturing = False
        self._ptt = False
        self._ptt_buf = bytearray()
        if self._watchdog:
            self._watchdog.cancel()
            self._watchdog = None
        # If a Hermes turn is in flight, ask the turn server to interrupt it and
        # discard the partial turn from the Question-Mode history (Phase 5D.6).
        self._request_hermes_cancel()
        cur = asyncio.current_task()
        for t in list(self._tasks):
            if t is not cur and not t.done():
                t.cancel()
        self._tasks.clear()
        if flush:
            try:
                self.source.flush()
            except Exception:
                pass

    def _request_hermes_cancel(self) -> None:
        """Fire-and-forget /cancel so a cancelled turn is not committed."""
        if not self._hermes_inflight:
            return
        async def _do():
            try:
                if GADGET_DIRECT:
                    try:
                        await _gadget_local({"op": "cancel"}, 5.0)
                        self._log("[gadget] cancel requested directly")
                        return
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"[gadget] direct cancel failed, using legacy facade: {exc}")
                await _http_post(CANCEL_URL, {"session_key": SESSION_KEY}, 5.0)
                self._log("[qsession] cancel requested via compatibility facade")
            except Exception as exc:  # noqa: BLE001
                self._log(f"[qsession] cancel request failed: {exc}")
        try:
            t = asyncio.ensure_future(_do())
            t.add_done_callback(lambda _t: None)
        except RuntimeError:
            pass

    def _fail(self, reason: str, gen: int) -> None:
        if self._live_active or self.ctl.get_state() in (self.ctl.LIVE_MODE, self.ctl.LIVE_MUTED):
            # Live Mode is authoritative; a one-shot failure must never tear it down.
            self._log(f"[oneshot] error={reason} ignored (live mode owns state)")
            return
        self._log(f"[oneshot] error={reason} -> ERROR_RECOVERY -> IDLE")
        self.ctl.play_chime("error")
        self.ctl.set_error(reason)
        self.ctl.recover(f"recover_after_{reason}")
        self._metrics_log(f"result=error:{reason}")

    # ---- capture bookkeeping: count samples fed for audio-time logs ----
    _samples_fed = 0

    # ---- main processing ----
    async def _process(self, pcm: bytes, gen: int) -> None:
        try:
            if self._live_active or self.ctl.get_state() in (self.ctl.LIVE_MODE, self.ctl.LIVE_MUTED):
                self._log("[oneshot] suppress one-shot turn (live mode active)")
                return
            t_end = time.time()
            self._log(f"[stt] transcribing {len(pcm)/2/SR16:.2f}s utterance")
            try:
                stt = json.loads(await _http_post(STT_URL, {
                    "wav_base64": base64.b64encode(pcm16_to_wav(pcm)).decode()}, STT_TIMEOUT))
            except Exception as exc:  # noqa: BLE001
                self._log(f"[stt] failed: {exc}")
                self._fail("stt_failure", gen)
                return
            if gen != self._gen:
                return
            text = _strip_wake((stt.get("text") or "").strip())
            self._metrics["stt_ms"] = stt.get("latency_ms")
            self._log(f"[stt] text={text!r} lang={stt.get('language')} "
                      f"prob={stt.get('language_probability')} latency_ms={stt.get('latency_ms')}")
            if not text:
                self._fail("empty_transcript", gen)
                return
            self._metrics["speech_end_to_text_ms"] = int((time.time() - t_end) * 1000)

            route = self.router.route(text, self.ctl.get_state())
            self._log(f"[router] route={route['route']} reason={route.get('reason')} matched={route.get('matched')}")

            if route["route"] == EchoRouter.ROUTE_LIVE:
                if await self._handle_live_route(gen):
                    return
                self._log("[router] Live disabled/unavailable; treating request as a normal question")
            if route["route"] in (EchoRouter.ROUTE_EXIT, EchoRouter.ROUTE_EXIT_CANDIDATE):
                self._log("[router] exit/soft phrase in one-shot listening -> end interaction")
                self.ctl.response_complete("router_exit")
                self._metrics_log("result=exit")
                return

            # question
            self.ctl.enter_question_mode("router_question")
            if gen != self._gen:
                return
            self.ctl.response_generation_started()
            self._playback_announced = False
            # Speak a short filler immediately so the user knows the request was
            # heard while the (possibly slow) Hermes turn runs.
            filler_task = None
            if FILLER_ENABLED and FILLER_TEXT:
                filler_task = self._spawn(self._play_filler(gen))
            answer, hermes = await self._hermes_turn(text, gen)
            if filler_task is not None:
                try:
                    await filler_task
                except Exception:  # noqa: BLE001
                    pass
            if gen != self._gen:
                return
            if not answer:
                self._fail("hermes_empty", gen)
                return
            self._metrics["hermes_ms"] = hermes.get("latency_ms")

            audio22 = await self._tts_vc(answer, gen)
            if gen != self._gen:
                return
            if not audio22:
                self._fail("tts_or_vc_failure", gen)
                return
            self._metrics["first_audio_ms"] = int((time.time() - t_end) * 1000)

            self._log(f"[playback] start streamed (~{len(audio22)/2/22050:.2f}s answer)")
            ok = await self._wait_drained(gen)
            if gen != self._gen:
                return
            self._log(f"[playback] done drained={ok}")
            await asyncio.sleep(DRAIN_TAIL_MS / 1000.0)
            if gen != self._gen:
                return
            self.ctl.playback_finished()
            self.ctl.response_complete()
            self._metrics["total_ms"] = int((time.time() - self._t_wake) * 1000) if self._t_wake else None
            self._metrics_log("result=answered")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._log(f"[oneshot] unexpected: {type(exc).__name__}: {exc}")
            if gen == self._gen:
                self._fail(f"{type(exc).__name__}", gen)

    async def _handle_live_route(self, gen: int) -> None:
        """Phase 5E: enter a real persistent GPT Live session via the supervisor.

        The temporary Phase 5D behaviour (enter -> chime -> immediately exit) is
        replaced: the bounded Question session is discarded, one persistent GPT
        Live session is created, and the controller stays in LIVE_MODE until an
        explicit exit / Action-long / idle timeout / failure.
        """
        if self._live is None:
            self._log("[oneshot] live route requested but Live is disabled/unavailable")
            return False
        self._log("[oneshot] live route: resetting bounded Question session")
        await self._reset_question_session()
        ok = await self._live.enter("router_live_mode")
        if not ok or not self._live.is_active():
            self._log("[oneshot] live session creation failed -> ERROR_RECOVERY -> IDLE")
            self._metrics_log("result=live_session_failed")
            return True
        sid = getattr(self._live.session, "session_id", None)
        self._log(f"[oneshot] LIVE_MODE active live_session={sid} (persistent realtime session)")
        self._metrics_log(f"result=live_mode session={sid}")
        return True

    async def _reset_question_session(self) -> None:
        try:
            if GADGET_DIRECT:
                try:
                    out = await _gadget_local({"op": "new"}, 10.0)
                    if out.get("ok"):
                        self._log("[gadget] Question session reset directly (enter live mode)")
                        return
                    raise RuntimeError(out.get("error") or "gadget reset failed")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"[gadget] direct reset failed, using legacy facade: {exc}")
            await _http_post(f"{TURN_BASE}/reset",
                             {"session_key": SESSION_KEY, "reason": "enter_live_mode"}, 10.0)
            self._log("[qsession] Question session reset via compatibility facade")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[qsession] reset on live entry failed: {exc}")

    async def _hermes_turn(self, text: str, gen: int):
        t0 = time.time()
        self._hermes_inflight = True
        try:
            j = None
            if GADGET_DIRECT:
                try:
                    j = await _gadget_local({"op": "turn", "message": text, "timeout_s": HERMES_TIMEOUT},
                                            HERMES_TIMEOUT + 5.0)
                    if not j.get("ok"):
                        raise RuntimeError(j.get("error") or "direct Gadget turn failed")
                    j = {
                        **j,
                        "session_id": j.get("turn") or j.get("request_id"),
                        "reused": True,
                        "turn_index": None,
                        "native_context": True,
                        "direct_gadget": True,
                    }
                    self._log("[gadget] turn completed directly via local bridge")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"[gadget] direct turn failed, using compatibility facade: {exc}")
                    j = None
            if j is None:
                raw = await _http_post(TURN_URL, {"message": text, "session_key": SESSION_KEY}, HERMES_TIMEOUT)
                j = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[hermes] request failed: {exc}")
            return "", {"latency_ms": int((time.time() - t0) * 1000)}
        finally:
            self._hermes_inflight = False
        answer = str(j.get("answer") or "").strip()
        lat = int((time.time() - t0) * 1000)
        sid = j.get("session_id")
        self._log(f"[qsession] session={sid} reused={j.get('reused')} "
                  f"turn={j.get('turn_index')} age_note={'same' if j.get('reused') else 'new'}")
        if j.get("reset"):
            self._log("[qsession] explicit reset acknowledged -> next wake starts a new session")
        tool_calls = j.get("tool_calls") or []
        if tool_calls:
            self._log(f"[hermes] tool_calls={tool_calls}")
        self._log(f"[hermes] response chars={len(answer)} latency_ms={lat} "
                  f"platform={j.get('platform')} direct_gadget={j.get('direct_gadget', False)} "
                  f"hook_ran={j.get('hook_ran')} injected={j.get('injected')} "
                  f"native_context={j.get('native_context')}")
        return answer, {"latency_ms": lat, "platform": j.get("platform"),
                        "direct_gadget": bool(j.get("direct_gadget")),
                        "hook_ran": j.get("hook_ran"), "injected": j.get("injected"),
                        "native_context": j.get("native_context"),
                        "session_id": sid, "reused": j.get("reused"),
                        "turn_index": j.get("turn_index"), "tool_calls": tool_calls}

    async def _play_filler(self, gen: int) -> None:
        """Speak a short filler while the (possibly slow) Hermes turn runs."""
        try:
            pcm, sr = await (hermes_tts(FILLER_TEXT) if self.tts_backend == "hermes" else direct_tts(FILLER_TEXT))
            if self.use_vc:
                audio = await seed_vc(pcm)
                audio_sr = 22050
            else:
                audio = pcm
                audio_sr = sr
        except Exception as exc:  # noqa: BLE001
            self._log(f"[filler] failed: {exc}")
            return
        if gen != self._gen or not audio:
            return
        if not self._playback_announced:
            self._playback_announced = True
            self.ctl.playback_started()
        self.source.push_48k(resample_pcm16(audio, audio_sr, SR48))
        self._log(f"[filler] spoke {FILLER_TEXT!r} ({len(audio)/2/audio_sr:.2f}s vc={self.use_vc})")

    async def _tts_vc(self, text: str, gen: int) -> bytes:
        t0 = time.time()
        try:
            pcm24, sr = await (hermes_tts(text) if self.tts_backend == "hermes" else direct_tts(text))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[tts] failed: {exc}")
            return b""
        self._metrics["tts_ms"] = int((time.time() - t0) * 1000)
        self._log(f"[tts] backend={self.tts_backend} {len(pcm24)/2/sr:.2f}s audio in {self._metrics['tts_ms']}ms (sr={sr})")

        if not self.use_vc:
            if not self._playback_announced:
                self._playback_announced = True
                self.ctl.playback_started()
            self.source.push_48k(resample_pcm16(pcm24, sr, SR48))
            self._metrics["vc_ms"] = 0
            self._log(f"[tts] raw TTS playback (voice conversion disabled, sr={sr})")
            # Keep the historical 22050 return-rate contract used only for duration/logging.
            return resample_pcm16(pcm24, sr, 22050)

        def _push(chunk22: bytes) -> None:
            # stream Seed-VC output to the downlink as it arrives (48k)
            if not self._playback_announced:
                self._playback_announced = True
                self.ctl.playback_started()
            self.source.push_48k(resample_pcm16(chunk22, 22050, SR48))

        try:
            out22 = await seed_vc(pcm24, on_chunk=_push)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[seed-vc] failed: {exc}")
            return b""
        if not out22:
            return b""
        self._metrics["vc_ms"] = int((time.time() - t0) * 1000) - self._metrics.get("tts_ms", 0)
        self._log(f"[seed-vc] {len(out22)/2/22050:.2f}s @22050 (streamed to 48k)")
        return out22

    async def _seed_vc(self, pcm24: bytes) -> bytes:
        return await seed_vc(pcm24)

    async def _wait_drained(self, gen: int) -> bool:
        t0 = time.time()
        # wait until playback actually started (or audio never queued)
        while time.time() - t0 < DRAIN_TIMEOUT:
            if gen != self._gen:
                return False
            if self.source.pending_samples() == 0 and self.source.playing:
                return True
            await asyncio.sleep(0.02)
        return False

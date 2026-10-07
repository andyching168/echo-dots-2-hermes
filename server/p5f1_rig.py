#!/usr/bin/env python3
"""Phase 5F.1 — no-billing real-device barge rig.

Provides a drop-in ``LiveModeSupervisor`` session factory that drives the **real**
Echo audio path (D24 AEC-clean uplink, D23 downlink, real ``BargeInDetector``,
real ``T_FLUSH`` ring flush) but sources the downlink from a local 48 kHz Hiyori
WAV instead of a GPT Live session.  No network/vendor call is made, so the
distance/level/volume/false-trigger detector matrix costs nothing in API time.

Interruption semantics are preserved locally: ``begin_barge_in`` bumps the playback
generation, flushes the queue, and pauses playback for a short interval (the
"assistant stopped, user is speaking" window), exactly as the downstream code
expects.  This is a *detector/acoustics* rig, not a replacement for the real GPT
Live conversation validation.
"""

from __future__ import annotations

import asyncio
import time
import wave

import echo_gpt

DOWNLINK_FRAME = echo_gpt.DOWNLINK_FRAME  # 480 samples @ 48 kHz
FEED_CHUNK = DOWNLINK_FRAME * 2           # bytes (10 ms)
LOOKAHEAD_MS = 200.0                      # keep the ring fed without overflowing cap
QUIET_RESUME_S = 1.2                      # user-silence before the rig resumes talking
MAX_RESUME_WAIT_S = 10.0                   # safety: never wait forever


def load_wav48(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, "rig wav must be 16-bit PCM"
        assert w.getnchannels() == 1, "rig wav must be mono"
        assert w.getframerate() == 48000, "rig wav must be 48 kHz"
        return w.readframes(w.getnframes())


class RigLiveSession:
    label = "rig"

    def __init__(self, diag_dir, tag, use_vc=False, *, delegate=None, turn_url=None,
                 session_key=None, log=None, on_user_transcript=None,
                 on_output_audio=None, on_delegation=None, on_session_event=None,
                 on_closed=None, on_fatal=None, wav: str = "",
                 detector_getter=None, **kw) -> None:
        self.wav = wav
        self._detector_getter = detector_getter
        self._log = log or (lambda m: print(m, flush=True))
        self.on_output_audio = on_output_audio
        self.on_user_transcript = on_user_transcript
        self.on_session_event = on_session_event
        self.on_closed = on_closed
        self.on_fatal = on_fatal
        self.session_id = f"rig-{int(time.time())}"
        self.created_at = time.time()
        self.closed_at = 0.0
        self.closed = False
        self.delegations = 0
        self.tool_call_count = 0
        self.playback_generation = 0
        self.delegation_generation = 0
        self.mic_enabled = True
        self.queue = echo_gpt.QueueSource()
        self.last_downlink_push_mono = time.monotonic()
        self._t0_mono = time.monotonic()
        self._pcm = b""
        self._pos = 0
        self._task = None
        self._wait_quiet = False
        self._quiet_since = None
        self._wait_deadline = 0.0
        self._last_activity = time.monotonic()
        self.pushes = 0

    async def start(self) -> None:
        self._pcm = load_wav48(self.wav)
        self._log(f"[rig] session={self.session_id} wav={self.wav} "
                  f"({len(self._pcm)/2/48000:.1f}s) started")
        # prime ~200 ms so the downlink never starves at t=0
        for _ in range(int(LOOKAHEAD_MS / 10)):
            self._push_chunk()
        self._task = asyncio.ensure_future(self._feeder())
        if self.on_output_audio:
            try:
                self.on_output_audio(True)
            except Exception:  # noqa: BLE001
                pass

    def _push_chunk(self) -> None:
        if self._pos + FEED_CHUNK > len(self._pcm):
            self._pos = 0
        self.queue.push_48k(self._pcm[self._pos:self._pos + FEED_CHUNK])
        self._pos += FEED_CHUNK
        self.pushes += 1
        self.last_downlink_push_mono = time.monotonic()

    async def _feeder(self) -> None:
        try:
            while not self.closed:
                now = time.monotonic()
                if self._wait_quiet:
                    # Wait until the user has stopped (detector hangover quiet)
                    # before resuming playback, so the rig never talks over them.
                    d = self._detector_getter() if self._detector_getter else None
                    active = bool(d is not None and d.speech_active)
                    if active:
                        self._quiet_since = None
                    elif self._quiet_since is None:
                        self._quiet_since = now
                    quiet_done = (self._quiet_since is not None
                                  and now - self._quiet_since > QUIET_RESUME_S)
                    if quiet_done or now > self._wait_deadline:
                        self._wait_quiet = False
                        self._log("[rig] user quiet -> resume playback")
                    else:
                        await asyncio.sleep(0.05)
                        continue
                if self.queue.pending_ms() < LOOKAHEAD_MS:
                    self._push_chunk()
                    await asyncio.sleep(0.005)
                else:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

    def begin_barge_in(self, source: str = "local_vad") -> int:
        self.playback_generation += 1
        self.delegation_generation += 1
        self.queue.flush()
        self.queue.mark_flushed()
        self._wait_quiet = True
        self._quiet_since = None
        self._wait_deadline = time.monotonic() + MAX_RESUME_WAIT_S
        self._log(f"[rig] barge source={source} gen={self.playback_generation} "
                  f"waiting for user to stop")
        return self.playback_generation

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = bool(enabled)

    def push_uplink(self, pcm16: bytes) -> None:
        self._last_activity = time.monotonic()

    def note_activity(self) -> None:
        self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def send_client_event(self, event: dict) -> bool:
        self._log(f"[rig] client_event {event.get('type')}")
        return True

    def mark(self, event: str, **fields) -> None:
        pass

    async def close(self, reason: str = "close") -> None:
        self.closed = True
        self.closed_at = time.time()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.queue.flush()
        self._log(f"[rig] session closed reason={reason}")
        if self.on_closed:
            try:
                self.on_closed(reason, self)
            except Exception:  # noqa: BLE001
                pass


class RigSessionFactory:
    """Callable matching the ``session_factory`` signature used by the supervisor."""

    def __init__(self, wav: str, detector_getter=None) -> None:
        self.wav = wav
        self.detector_getter = detector_getter

    def __call__(self, *a, **k):
        k["wav"] = self.wav
        if self.detector_getter is not None:
            k["detector_getter"] = self.detector_getter
        return RigLiveSession(*a, **k)

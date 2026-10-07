#!/usr/bin/env python3
"""Phase 5F.1 — opt-in JSONL diagnostic trace for the Echo-side barge detector.

This module is **inert unless** the ``ECHO_TRACE`` environment variable names a
file.  With ``ECHO_TRACE`` unset the deployed runtime behaviour is byte-for-byte
the Phase 5F behaviour: :class:`TraceDetector` is only selected when tracing is
enabled, and every :func:`trace` call is a no-op.

It exists so the real-room session can capture, for every 10 ms frame the
detector sees while Hiyo is audible, the exact ``rms / threshold / noise floor /
run / speech / armed / playing`` values, plus the barge and device-flush events.
The real ``BargeInDetector`` in ``echo_barge.py`` is **not modified**.
"""

from __future__ import annotations

import json
import os
import time

import echo_barge

_PATH = os.environ.get("ECHO_TRACE", "")
_T0 = time.monotonic()
_FH = None


def enabled() -> bool:
    return bool(_PATH)


def trace(event: str, **fields) -> None:
    if not _PATH:
        return
    global _FH
    if _FH is None:
        try:
            _FH = open(_PATH, "a", buffering=1)
        except Exception:
            return
    rec = {"t": round(time.monotonic() - _T0, 6), "wall": round(time.time(), 6),
           "event": event}
    rec.update(fields)
    try:
        _FH.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


class TraceDetector(echo_barge.BargeInDetector):
    """A ``BargeInDetector`` that additionally writes a per-frame trace.

    Detection behaviour is inherited unchanged from ``BargeInDetector.feed``;
    this subclass only observes.  Frames are traced while playback is active and
    for a short tail after it stops, so the run of frames leading into an onset
    and the post-barge quiet frames are both recorded.
    """

    TAIL_S = 2.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._tail_until = 0.0

    def set_playing(self, playing: bool, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        if not playing:
            self._tail_until = now + self.TAIL_S
        super().set_playing(playing, now)
        if echo_trace_enabled():
            trace("det.playing", playing=int(bool(playing)), armed=int(bool(self.armed)),
                  noise=round(self.noise, 1))

    def feed(self, frame: bytes, now: float | None = None) -> bool:
        import numpy as np

        now = now if now is not None else time.monotonic()
        x = np.frombuffer(frame, dtype="<i2")
        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if len(x) else 0.0
        thr = self._thr()
        run_before = self._run
        armed_before = self.armed
        fired = super().feed(frame, now)
        if echo_trace_enabled() and (self.playing or now < self._tail_until):
            trace("det.frame", rms=round(rms, 1), thr=round(thr, 1),
                  noise=round(self.noise, 1), run=run_before, speech=self._speech,
                  armed=int(armed_before), playing=int(bool(self.playing)),
                  fired=int(bool(fired)))
        return fired


def echo_trace_enabled() -> bool:
    return bool(_PATH)

#!/usr/bin/env python3
"""Phase 5F — Echo-side barge-in detector (AEC-clean uplink energy VAD).

Listens on the **existing proven AEC path** (Echo D24 → Ch6 mic + Ch7 reference →
Speex AEC → 16 kHz clean uplink).  It never opens a second microphone path and never
uses the raw mic: the detector must hear the user, not Hiyo's own speaker.

The detector adapts its noise floor while the assistant is not speaking and only
arms while the Echo is actively playing.  A short run of consecutive loud frames is
required so keyboard/tap transients cannot trigger.  With AEC the assistant's own
playback is cancelled, so its leakage does not register as speech.
"""

from __future__ import annotations

import time

import numpy as np

SR16 = 16000
FRAME_MS = 10


class BargeInDetector:
    # Phase 5F.1 real-room tuning (evidence: phase5f1/REPORT.md):
    #   * abs_floor / min_speech_level lowered 420 -> 340: quiet speech at 1-2 m
    #     was sitting right at/under the old binding floor (runs peaked 332-400)
    #     and was missed; 340 recovers it.  Offline replay showed only +1
    #     deliberate transient false-trigger and ZERO self-playback triggers
    #     (AEC residual median ~60, and a clean-room silence run never fired).
    #   * onset_frames stays 5 (50 ms algorithmic latency).  Raising it to 6
    #     collapsed detection (see report), so it must NOT be increased to fight
    #     transient false triggers.
    #   * min_speech_frames was dead configuration (never read by feed()); removed.
    def __init__(self, *, abs_floor: float = 340.0, noise_mult: float = 3.0,
                 thr_cap: float = 4000.0, onset_frames: int = 5,
                 min_speech_level: int = 340, hangover_s: float = 0.9) -> None:
        self.abs_floor = abs_floor
        self.noise_mult = noise_mult
        self.thr_cap = thr_cap
        self.onset_frames = onset_frames
        self.min_speech_level = min_speech_level
        self.hangover_s = hangover_s

        self.noise = 80.0
        self.playing = False
        self.armed = True
        self._run = 0
        self._speech = 0
        self._last_voice = 0.0
        self.events: list[dict] = []

    # ── lifecycle informed by downlink activity ──────────────────────────────
    def set_playing(self, playing: bool, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        if playing and not self.playing:
            self._run = 0
            self._speech = 0
            self.events.append({"m": now, "event": "detect.arm"})
        self.playing = playing
        if not playing:
            self._run = 0

    def _thr(self) -> float:
        return max(self.abs_floor, min(self.noise * self.noise_mult, self.thr_cap))

    @property
    def speech_active(self) -> bool:
        return (time.monotonic() - self._last_voice) < self.hangover_s

    def feed(self, frame: bytes, now: float | None = None) -> bool:
        """Feed one 10 ms PCM16 16 kHz frame.  Returns True on the single frame
        where a barge-in onset is confirmed."""
        now = now if now is not None else time.monotonic()
        x = np.frombuffer(frame, dtype="<i2")
        if not len(x):
            return False
        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))
        thr = self._thr()

        if rms >= thr:
            self._run += 1
            self._speech += 1
            self._last_voice = now
        else:
            self._run = 0
            # Only adapt the floor when nobody is speaking; freeze it during speech
            # so a loud answer cannot raise the trigger threshold.
            if not self.speech_active:
                self.noise = 0.95 * self.noise + 0.05 * rms

        if (self.playing and self.armed and self._run >= self.onset_frames
                and rms >= self.min_speech_level and self._speech >= self.onset_frames
                and self._run == self.onset_frames):
            self.armed = False
            ev = {"m": now, "event": "detect.onset", "rms": int(rms),
                  "thr": int(thr), "noise": int(self.noise), "run": self._run}
            self.events.append(ev)
            return True
        # Re-arm once the user has been quiet for the hangover window.
        if not self.armed and (now - self._last_voice) >= self.hangover_s:
            self.armed = True
            self.events.append({"m": now, "event": "detect.rearm"})
        return False

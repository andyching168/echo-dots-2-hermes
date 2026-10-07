#!/usr/bin/env python3
"""Phase 5D.5 — Echo LED State Projector + animation renderer.

Layering (single authoritative projection path):

    EchoController state / semantic events
        |
        v
    LedProjector                (this module: semantic states + rendering)
        |  T_LED_FRAME (72-char IS31FL3236 hex frame)
        v
    echo_control daemon         (sole LED owner/arbitrator on the device)
        |  /sys/bus/i2c/devices/0-003f/frame
        v
    IS31FL3236 12x RGB ring

Higher layers never pass RGB values or frames: they call the semantic API
(``set_state`` / ``flash`` / ``suspend_for_physical_mute`` / ...) or let the
controller forward semantic sub-events (``playback_started`` etc.).

Physical mute precedence is enforced on the device by ``echo_control``: it is
the same process that reads the mute key, so it stops writing frames *before*
it asserts ``muteState`` (stock red).  This projector also suspends so it stops
feeding frames, but the device owner is the authority.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Callable, List, Optional, Tuple

from echo_led_owner import (ACTIVE_STATES, HIYO, MODE_COOP, MODE_EXCL, STOCK,
                            LedOwnershipManager)

try:
    from volume_frames import VOLUME_STEP_FRAMES, N_VOLUME_STEPS
except Exception:  # noqa: BLE001 - overlay is optional
    VOLUME_STEP_FRAMES, N_VOLUME_STEPS = {}, 30

# Stock volume ring overlay: how long each volume step stays visible.
VOLUME_SHOW_S = 1.6

# --- wire type (kept in sync with echo_controller.py / echo_control.c) --------
T_LED_FRAME = 0x91

# --- geometry / rendering ----------------------------------------------------
N_LEDS = 12
RENDER_FPS = 15
FRAME_HEX_LEN = N_LEDS * 6

# Global brightness scale.  Echo Dot LEDs are viewed at close range, so this is
# deliberately conservative and never runs full-intensity white continuously.
GLOBAL_BRIGHTNESS = 0.40

# --- semantic states (never hardware commands) -------------------------------
LED_IDLE = "idle"
LED_LISTENING = "listening"
LED_THINKING = "thinking"
LED_SPEAKING = "speaking"
LED_LIVE = "live"
LED_MUTED = "muted"
LED_ERROR = "error"
LED_INTERRUPT = "interrupt"

ALL_STATES = (LED_IDLE, LED_LISTENING, LED_THINKING, LED_SPEAKING,
              LED_LIVE, LED_MUTED, LED_ERROR, LED_INTERRUPT)

# Controller state -> semantic LED state (base projection).
STATE_MAP = {
    "IDLE": LED_IDLE,
    "LISTEN_ONE_SHOT": LED_LISTENING,
    "QUESTION_ACTIVE": LED_THINKING,
    "LIVE_MODE": LED_LIVE,
    "LIVE_MUTED": LED_MUTED,
    "ERROR_RECOVERY": LED_ERROR,
}


def _mul(rgb: Tuple[int, int, int], f: float) -> Tuple[int, int, int]:
    r, g, b = rgb
    return (max(0, min(255, int(r * f))),
            max(0, min(255, int(g * f))),
            max(0, min(255, int(b * f))))


def _hex(rgb_list: List[Tuple[int, int, int]]) -> str:
    return "".join("%02x%02x%02x" % c for c in rgb_list)


def _hsv(h: float, s: float, v: float) -> Tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


class LedProjector:
    """Renders semantic LED states into 12xRGB frames and streams them.

    The projector never writes hardware.  It only sends frames to the device
    owner (``echo_control``) which performs the actual sysfs write and the
    physical-mute arbitration.
    """

    def __init__(self, controller, log: Optional[Callable[[str], None]] = None,
                 brightness: float = GLOBAL_BRIGHTNESS, mode: str = MODE_COOP):
        self.ctl = controller
        self._log = log or (lambda m: print(m, flush=True))
        self._brightness = max(0.05, min(1.0, float(brightness)))
        self.owner_mgr = LedOwnershipManager(controller, mode=mode, log=self._log)
        self._base = LED_IDLE
        self._override: Optional[str] = None
        self._flash_kind: Optional[str] = None
        self._flash_until = 0.0
        self._flash_dur = 0.0
        self._suspended = False
        self._suspended_reason = None
        self._demo_until = 0.0
        self._vol_frame: Optional[str] = None
        self._vol_until = 0.0
        self._vol_acquired = False
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self.frames_sent = 0
        controller.add_listener(self._on_state)

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
            self._log(f"[led] projector started fps={RENDER_FPS} "
                      f"brightness={int(self._brightness * 100)}% "
                      f"base_state={self._base}")

    async def aclose(self, off: bool = True) -> None:
        self._stop = True
        try:
            self.owner_mgr.ensure_stock("shutdown")
        except Exception:
            pass
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        try:
            self.ctl.remove_listener(self._on_state)
        except Exception:
            pass

    # ---- semantic API (higher layers call only these) -----------------------
    def set_state(self, state: str, source: str = "api") -> str:
        """Set the base semantic state (idle/listening/thinking/speaking/
        live/error).  Accepts a controller state name too (case-insensitive)."""
        state = STATE_MAP.get(state, STATE_MAP.get(str(state).upper(), state))
        if state not in ALL_STATES:
            self._log(f"[led] REJECT state={state!r} source={source}")
            return self._base
        if state == LED_MUTED:
            self.suspend_for_physical_mute(reason=source)
            self._base = LED_MUTED
            return self._base
        self._override = None
        if state != self._base:
            self._base = state
            self._log(f"[led] base_state={state} source={source}")
        if not self._suspended:
            if state == LED_IDLE:
                self.owner_mgr.release_to_stock(source)
            else:
                self.owner_mgr.acquire_hiyo(source)
        return self._base

    def flash(self, kind: str = LED_INTERRUPT, ms: int = 180) -> None:
        self._flash_kind = kind
        self._flash_dur = max(0.05, ms / 1000.0)
        self._flash_until = time.monotonic() + self._flash_dur
        self._log(f"[led] flash={kind}")

    def show_volume(self, level: int) -> None:
        """Overlay the stock volume ring for `level` (0-100) for VOLUME_SHOW_S.

        Uses the exact stock ``volume_step-NN`` frames so the indicator matches the
        original firmware's look.  Higher priority than the base/flash states; the
        physical-mute suspension (checked in the render loop) still wins."""
        try:
            lvl = max(0, min(100, int(level)))
        except Exception:  # noqa: BLE001
            return
        step = int(round(lvl * N_VOLUME_STEPS / 100.0))
        step = max(1, min(N_VOLUME_STEPS, step))
        frame = VOLUME_STEP_FRAMES.get(step)
        if not frame:
            return
        self._vol_frame = frame
        self._vol_until = time.monotonic() + VOLUME_SHOW_S
        # The stock volume animation is dead in raw/endpoint mode, so borrow the
        # ring for the overlay even when idle, then hand it back when it ends.
        self._vol_acquired = (self._base == LED_IDLE)
        if not self._suspended:
            self.owner_mgr.acquire_hiyo("volume_ui")
        self._log(f"[led] volume overlay level={lvl} step={step}")

    def _release_volume_overlay_if_done(self, t: float) -> None:
        if self._vol_acquired and t >= self._vol_until:
            self._vol_acquired = False
            if self._base == LED_IDLE and not self._suspended:
                self.owner_mgr.release_to_stock("volume_ui_done")

    def suspend_for_physical_mute(self, reason: str = "physical_mute") -> None:
        if self._suspended:
            return
        self._suspended = True
        self._suspended_reason = reason
        self._vol_acquired = False
        self._log(f"[led] suspended reason={reason}")

    def resume_after_physical_mute(self) -> None:
        if not self._suspended:
            return
        self._suspended = False
        self._suspended_reason = None
        self._log(f"[led] resumed state={self.effective_state()}")

    def playback_started(self) -> None:
        """First answer audio actually started playing -> speaking."""
        if self._base == LED_THINKING:
            self._override = LED_SPEAKING
            self._log("[led] state=speaking source=playback")

    def playback_finished(self) -> None:
        """Answer audio drained -> restore the controller-derived base state."""
        if self._override is not None:
            self._override = None
            self._log(f"[led] state={self._base} source=playback_done")

    def response_generation_started(self) -> None:
        """Question accepted, answer being produced -> thinking."""
        self._override = None
        if self._base != LED_THINKING:
            self._base = LED_THINKING
            self._log("[led] state=thinking source=oneshot")

    def response_generation_done(self) -> None:
        self._override = None

    def interaction_interrupted(self) -> None:
        self.flash(LED_INTERRUPT, ms=180)

    # ---- showcase / demo mode (test aid; does not affect normal operation) --
    def start_demo(self, seconds: float = 20.0) -> None:
        self._demo_until = time.monotonic() + max(1.0, min(600.0, float(seconds)))
        self._log(f"[led] demo started {seconds:.0f}s")

    def stop_demo(self) -> None:
        if self._demo_until:
            self._demo_until = 0.0
            self._log("[led] demo stopped")

    # ---- controller listener ------------------------------------------------
    def _on_state(self, old: str, new: str, reason: str) -> None:
        if new == "LIVE_MUTED":
            self._base = LED_MUTED
            self.suspend_for_physical_mute(reason="physical_mute")
            self.owner_mgr.physical_mute_changed(True)
            return
        if old == "LIVE_MUTED" and new != "LIVE_MUTED":
            self._base = STATE_MAP.get(new, LED_IDLE)
            self.resume_after_physical_mute()
            self.owner_mgr.physical_mute_changed(False)
            if new in ACTIVE_STATES and not self._suspended:
                self.owner_mgr.acquire_hiyo(f"unmute_resume:{new}")
            return
        mapped = STATE_MAP.get(new, LED_IDLE)
        if mapped != self._base or self._override is not None:
            self._base = mapped
            self._override = None
            self._log(f"[led] base_state={mapped} source=controller")
        if self._suspended:
            return
        if new in ACTIVE_STATES:
            self.owner_mgr.acquire_hiyo(new)
        elif new == "IDLE":
            self.owner_mgr.release_to_stock(reason or "idle")

    # ---- state / rendering --------------------------------------------------
    def effective_state(self) -> str:
        if self._suspended:
            return LED_MUTED
        return self._override or self._base

    def status(self) -> dict:
        return {
            "state": self.effective_state(),
            "base_state": self._base,
            "override": self._override,
            "suspended": self._suspended,
            "suspended_reason": self._suspended_reason,
            "brightness": round(self._brightness, 3),
            "fps": RENDER_FPS,
            "frames_sent": self.frames_sent,
            "demo": self._demo_until > 0,
            "volume_until": self._vol_until,
            "ownership": self.owner_mgr.status(),
        }

    def _render_demo(self, t: float) -> List[Tuple[int, int, int]]:
        """Rotating rainbow with a bright comet head -- showcase only."""
        n = N_LEDS
        head = (t * 4.0) % n
        out = []
        for i in range(n):
            r, g, b = _hsv(i / n + t * 0.12, 1.0, 1.0)
            d = min((i - head) % n, (head - i) % n)
            fade = max(0.12, 1.0 - d / 4.0)
            out.append(_mul((r, g, b), fade))
        return out

    def _render_state(self, state: str, t: float) -> List[Tuple[int, int, int]]:
        n = N_LEDS
        if state == LED_IDLE or state == LED_MUTED:
            return [(0, 0, 0)] * n
        if state == LED_LISTENING:
            base = (0, 150, 210)
            env = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.50 * t))
            return [_mul(base, env)] * n
        if state == LED_THINKING:
            base = (95, 65, 210)
            env = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.35 * t))
            head = int(t * 3) % n
            return [_mul(base, min(1.0, env + (0.25 if i == head else 0.0)))
                    for i in range(n)]
        if state == LED_SPEAKING:
            base = (110, 215, 255)
            env = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(2 * math.pi * 1.30 * t))
            head = int(t * 9) % n
            return [_mul(base, min(1.0, env + (0.35 if i == head else 0.0)))
                    for i in range(n)]
        if state == LED_LIVE:
            return [_mul((70, 45, 165), 0.65)] * n
        if state == LED_ERROR:
            base = (255, 140, 0)
            env = 0.30 + 0.70 * abs(math.sin(2 * math.pi * 1.0 * t))
            return [_mul(base, env)] * n
        return [(0, 0, 0)] * n

    def _render_frame(self, t: float) -> str:
        # Stock volume overlay has the highest priority (matches the firmware,
        # where the volume layer outranks listening/thinking states).
        if self._vol_until and t < self._vol_until and self._vol_frame:
            return self._vol_frame
        if self._demo_until and t < self._demo_until:
            return _hex([_mul(c, self._brightness) for c in self._render_demo(t)])
        if self._flash_until and t < self._flash_until:
            rem = (self._flash_until - t) / max(1e-6, self._flash_dur)
            env = max(0.0, min(1.0, rem))
            base = (255, 140, 0) if self._flash_kind == LED_ERROR else (255, 255, 255)
            rgb = [_mul(base, env)] * N_LEDS
        else:
            self._flash_until = 0.0
            rgb = self._render_state(self.effective_state(), t)
        g = self._brightness
        return _hex([_mul(c, g) for c in rgb])

    async def _run(self) -> None:
        interval = 1.0 / RENDER_FPS
        try:
            while not self._stop:
                if not self._suspended:
                    t = time.monotonic()
                    self._release_volume_overlay_if_done(t)
                    frame = self._render_frame(t)
                    self.ctl.send_to_echo(T_LED_FRAME, frame.encode("ascii"))
                    self.frames_sent += 1
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        finally:
            # Clean shutdown: restore stock/default (idle = off) unless the
            # device is physically muted (stock red must stay truthful).
            if not self._suspended:
                try:
                    self.ctl.send_to_echo(T_LED_FRAME, ("00" * (N_LEDS * 3)).encode("ascii"))
                except Exception:
                    pass
            self._log("[led] projector stopped")

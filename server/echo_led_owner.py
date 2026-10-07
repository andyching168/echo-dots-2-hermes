#!/usr/bin/env python3
"""Phase 5D.5B — LED ownership manager (server-side intent + status).

There are only two valid LED owners: STOCK and HIYO.  This manager owns the
*decision* of when Hiyo needs exclusive ownership; the device daemon executes
`stop/start ledcontroller` and hosts the independent watchdog.

Higher layers must never call `stop`/`start ledcontroller` directly — only this
manager (via the device daemon) changes ownership.

Policies:
    cooperative  never stops stock; Hiyo renders as a guest (safe fallback)
    exclusive    stops stock while a Hiyo state is visible; watchdog-protected
"""

from __future__ import annotations

from typing import Callable, Optional

# --- wire types (kept in sync with echo_control.c) ---------------------------
T_LED_OWN = 0x92          # server -> Echo: u8 acquire(1=HIYO,0=STOCK)
T_LED_MODE_CFG = 0x93     # server -> Echo: u8 mode(0=cooperative,1=exclusive)
T_LED_OWNER_STATE = 0x17  # Echo -> server: u8 owner, u8 mode

STOCK = "STOCK"
HIYO = "HIYO"
MODE_COOP = "cooperative"
MODE_EXCL = "exclusive"
MODES = (MODE_COOP, MODE_EXCL)

# Controller states during which Hiyo wants the ring.
ACTIVE_STATES = ("LISTEN_ONE_SHOT", "QUESTION_ACTIVE", "LIVE_MODE", "ERROR_RECOVERY")


class LedOwnershipManager:
    def __init__(self, controller, mode: str = MODE_COOP,
                 log: Optional[Callable[[str], None]] = None):
        self.ctl = controller
        self._log = log or (lambda m: print(m, flush=True))
        self._mode = mode if mode in MODES else MODE_COOP
        self._owner = STOCK          # logical/desired owner
        self._device_owner = STOCK   # last owner reported by the device
        self._muted = False

    # ---- queries ------------------------------------------------------------
    def mode(self) -> str:
        return self._mode

    def is_exclusive(self) -> bool:
        return self._mode == MODE_EXCL

    def owner(self) -> str:
        return self._owner

    def status(self) -> dict:
        return {"mode": self._mode, "owner": self._owner,
                "device_owner": self._device_owner, "muted": self._muted}

    # ---- policy -------------------------------------------------------------
    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            return self._mode
        if mode != self._mode:
            if self._owner == HIYO:
                self.release_to_stock("mode_change")
            self._mode = mode
            self._send(T_LED_MODE_CFG, 1 if mode == MODE_EXCL else 0)
            self._log(f"[led-owner] mode={mode}")
        return self._mode

    # ---- API ----------------------------------------------------------------
    def acquire_hiyo(self, reason: str = "wake") -> bool:
        if self._muted:
            self._log(f"[led-owner] acquire refused reason={reason} (physical mute)")
            return False
        already = (self._owner == HIYO)
        self._owner = HIYO
        if self.is_exclusive():
            if not already:
                self._log(f"[led-owner] STOCK -> HIYO reason={reason}")
            self._send(T_LED_OWN, 1)   # always: idempotent + self-healing
        elif not already:
            # cooperative: stock stays running; Hiyo is a guest renderer
            self._log(f"[led-owner] render granted reason={reason} (cooperative, stock kept)")
        return True

    def release_to_stock(self, reason: str = "idle") -> bool:
        if self._owner == HIYO:
            self._log(f"[led-owner] HIYO -> STOCK reason={reason}")
        self._owner = STOCK
        if self.is_exclusive():
            self._send(T_LED_OWN, 0)   # always: idempotent
        return True

    def ensure_stock(self, reason: str = "ensure_stock") -> bool:
        return self.release_to_stock(reason)

    def physical_mute_changed(self, muted: bool) -> None:
        self._muted = bool(muted)
        if muted:
            self.ensure_stock("physical_mute")
        # on unmute the projector re-acquires only if it re-enters an active state

    def on_device_state(self, owner: int, mode: int) -> None:
        self._device_owner = HIYO if owner else STOCK
        dev_mode = MODE_EXCL if mode else MODE_COOP
        if dev_mode != self._mode:
            self._log(f"[led-owner] device mode={dev_mode}")
            self._mode = dev_mode

    # ---- internals ----------------------------------------------------------
    def _send(self, msg_type: int, value: int) -> None:
        try:
            self.ctl.send_to_echo(msg_type, bytes([value]))
        except Exception as exc:  # noqa: BLE001
            self._log(f"[led-owner] send {msg_type:#04x} failed: {exc}")

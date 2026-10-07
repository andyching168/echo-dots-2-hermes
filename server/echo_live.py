#!/usr/bin/env python3
"""Phase 5E — Echo Live Mode supervisor.

Bridges the authoritative :class:`echo_controller.EchoController` to the proven
vendor-only GPT Live prototype in :mod:`echo_gpt` (no client WebRTC leg, no new
voice path).  Responsibilities:

* own exactly **one persistent GPT Live session** for the lifetime of
  ``LIVE_MODE`` (created on entry, closed on exit — never per utterance);
* swap the Echo downlink source from the one-shot queue to the GPT/Seed-VC
  queue while live, and restore it on exit;
* forward the AEC-clean 16 kHz uplink to the vendor mic while live (and stop
  forwarding while physically muted);
* route ``session.delegation.created`` through the Echo turn server
  (``platform="echo"`` + ``hermes-echo`` runtime context), and speak the reply
  back with ``session.commentary.append``;
* drive the semantic LED sub-states (live/listening/thinking/speaking);
* deterministic exits: explicit phrases, Action-long, idle timeout, mute-death,
  vendor/controller failure.

The controller is and stays authoritative: this class only reacts to its state
transitions and never decides interaction state on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Callable, Optional

import echo_gpt
from echo_barge import BargeInDetector
from echo_hermes import EchoRouter

DEFAULT_IDLE_TIMEOUT = 120.0
LED_LIVE = "live"
LED_LISTENING = "listening"
LED_THINKING = "thinking"
LED_SPEAKING = "speaking"


class LiveModeSupervisor:
    def __init__(self, controller, source_swap: Callable, default_source,
                 log: Optional[Callable[[str], None]] = None,
                 diag_dir: Optional[str] = None, use_vc: bool = True,
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 turn_url: Optional[str] = None, session_key: str = "echo-live",
                 cancel_url: Optional[str] = None, session_factory=None,
                 barge: bool = True, barge_detector=None,
                 flush_downlink: Optional[Callable[[], None]] = None) -> None:
        self.ctl = controller
        self._source_swap = source_swap
        self._default_source = default_source
        self._log = log or (lambda m: print(m, flush=True))
        self._diag_dir = diag_dir or os.path.expanduser("~/voice/echo-bridge/logs/p5e_live")
        self.use_vc = use_vc
        self.idle_timeout = float(idle_timeout)
        self.turn_url = (turn_url or os.environ.get("ECHO_TURN_URL", "http://127.0.0.1:8796/turn")).rstrip("/")
        self.session_key = session_key
        self._cancel_url = cancel_url or (self.turn_url.rsplit("/", 1)[0] + "/cancel")
        self._session_factory = session_factory or echo_gpt.GptLiveSession
        self.router = EchoRouter()
        # ── Phase 5F: Echo-side barge-in ──────────────────────────────────────
        self._barge_enabled = barge
        # Phase 5F.1: use the tracing subclass only when ECHO_TRACE is set; the
        # production detector and its behaviour are unchanged otherwise.
        if barge_detector is None:
            try:
                import echo_trace
                if echo_trace.enabled():
                    barge_detector = echo_trace.TraceDetector()
            except Exception:  # noqa: BLE001
                barge_detector = None
        self.detector = barge_detector or BargeInDetector()
        self._flush_downlink = flush_downlink
        self._barge_count = 0
        self._last_barge_mono = 0.0
        self._in_barge = False
        self._t_last_downlink_mono = time.monotonic()
        self._downlink_was_playing = False

        self.session: Optional[echo_gpt.GptLiveSession] = None
        self._enter_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._closing = False
        self._started_at: Optional[float] = None
        self._substate = LED_LIVE
        self._recent_user = ""
        self._turns = 0
        self.stats = {"enters": 0, "closes": 0, "user_turns": 0, "delegations": 0,
                      "tool_calls": 0, "first_audio_ms": None, "exit_reason": None}
        controller.add_listener(self._on_state)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def detach(self) -> None:
        try:
            self.ctl.remove_listener(self._on_state)
        except Exception:  # noqa: BLE001
            pass
        for t in (self._enter_task, self._idle_task, self._monitor_task):
            if t is not None and not t.done():
                t.cancel()

    def is_active(self) -> bool:
        return self.session is not None and not self.session.closed

    def status(self) -> dict:
        s = self.session
        return {
            "active": self.is_active(),
            "session_id": getattr(s, "session_id", None),
            "live_session_id": getattr(s, "session_id", None),
            "started_at": self._started_at,
            "turns": self._turns,
            "delegations": getattr(s, "delegations", 0),
            "tool_calls": getattr(s, "tool_call_count", 0),
            "substate": self._substate,
            "idle_s": round(s.idle_seconds(), 1) if s else None,
            "exit_reason": self.stats["exit_reason"],
            "barge_count": self._barge_count,
            "playback_generation": getattr(s, "playback_generation", None),
            "downlink_playing": self._downlink_was_playing,
        }

    async def enter(self, reason: str = "router_live_mode") -> bool:
        """Enter LIVE_MODE and start exactly one persistent GPT Live session."""
        if self.is_active():
            return True
        ok = self.ctl.enter_live_mode(reason)
        if not ok and self.ctl.get_state() != self.ctl.LIVE_MODE:
            self._log(f"[live] enter refused reason={reason} state={self.ctl.get_state()}")
            return False
        if self._enter_task is not None and not self._enter_task.done():
            await self._enter_task
        elif self.session is None:
            await self._start(reason)
        return self.is_active()

    async def _start(self, reason: str) -> None:
        if self.session is not None:
            return
        tag = f"p5f_live_{time.strftime('%Y%m%d_%H%M%S')}"
        self._log(f"[live] create tag={tag} reason={reason} vc={self.use_vc} delegate=turn key={self.session_key}")
        sess = self._session_factory(
            self._diag_dir, tag, use_vc=self.use_vc,
            delegate="turn", turn_url=self.turn_url, session_key=self.session_key,
            log=self._log,
            on_user_transcript=self._on_user,
            on_output_audio=self._on_output_audio,
            on_delegation=self._on_delegation,
            on_session_event=self._on_session_event,
            on_closed=self._on_session_closed,
            on_fatal=self._on_fatal,
        )
        self.session = sess
        try:
            await sess.start()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[live] session start FAILED: {type(exc).__name__}: {exc}")
            self.session = None
            try:
                await sess.close("start_failed")
            except Exception:  # noqa: BLE001
                pass
            # Never leave the controller in LIVE_MODE without a session.
            self.ctl.set_error("live_session_failed")
            self.ctl.recover("recover_after_live_session_failed")
            return
        self._started_at = time.time()
        self.stats["enters"] += 1
        self.stats["exit_reason"] = None
        self._substate = LED_LIVE
        self._recent_user = ""
        self._barge_count = 0
        self._last_barge_mono = 0.0
        try:
            if self.detector is not None:
                self.detector.armed = True
        except Exception:  # noqa: BLE001
            pass
        self._source_swap(sess.queue)
        self.ctl.led_set_state(LED_LIVE, source="live")
        sess.note_activity()
        self._idle_task = asyncio.ensure_future(self._idle_watch())
        self._monitor_task = asyncio.ensure_future(self._monitor())
        self._log(f"[live] waiting session={sess.session_id}")
        try:
            self.ctl.play_chime("live")
        except Exception:  # noqa: BLE001
            pass

    async def _close(self, reason: str) -> None:
        if self._closing:
            return
        self._closing = True
        self.stats["closes"] += 1
        self.stats["exit_reason"] = reason
        for t in (self._idle_task, self._monitor_task):
            if t is not None and not t.done():
                t.cancel()
        self._idle_task = self._monitor_task = None
        sess = self.session
        self.session = None
        if sess is not None:
            await self._cancel_delegation()
            try:
                sess.set_mic_enabled(False)
                sess.queue.flush()
                await sess.close(reason)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[live] close error: {type(exc).__name__}: {exc}")
        try:
            self._source_swap(self._default_source)
            if hasattr(self._default_source, "flush"):
                self._default_source.flush()
        except Exception:  # noqa: BLE001
            pass
        self._log(f"[live] closed reason={reason} turns={self._turns} "
                  f"delegations={self.stats['delegations']} tool_calls={self.stats['tool_calls']}")
        self._closing = False

    async def stop(self, reason: str = "stop") -> None:
        await self._close(reason)

    # ── controller listener ──────────────────────────────────────────────────
    def _on_state(self, old: str, new: str, reason: str) -> None:
        if new == self.ctl.LIVE_MODE and old != self.ctl.LIVE_MUTED:
            # Entry (router/tool) — the supervisor owns session creation.
            if self.session is None and (self._enter_task is None or self._enter_task.done()):
                self._enter_task = asyncio.ensure_future(self._start(reason))
            return
        if new == self.ctl.LIVE_MUTED:
            self._log("[live] suspend reason=physical_mute")
            if self.session is not None:
                self.session.set_mic_enabled(False)
                self.session.send_client_event({"type": "session.input_audio.mute"})
            return
        if old == self.ctl.LIVE_MUTED and new == self.ctl.LIVE_MODE:
            if self.session is not None and not self.session.closed:
                self.session.set_mic_enabled(True)
                self.session.note_activity()
                self.session.send_client_event({"type": "session.input_audio.unmute"})
                self.ctl.led_set_state(LED_LIVE, source="live")
                self._log("[live] resume reason=physical_unmute")
            else:
                self._log("[live] resume impossible (session dead) -> IDLE")
                self.ctl.exit_live_mode("live_session_dead")
            return
        if old in (self.ctl.LIVE_MODE, self.ctl.LIVE_MUTED) and new in (self.ctl.IDLE, self.ctl.ERROR_RECOVERY):
            asyncio.ensure_future(self._close(reason))

    # ── vendor callbacks ─────────────────────────────────────────────────────
    def _on_user(self, delta: str) -> None:
        if self.session is None:
            return
        self.session.note_activity()
        self._recent_user = (self._recent_user + delta)[-80:]
        # A user utterance while Hiyo is speaking is a barge-in: stop the current
        # response first (flush stale audio, LED_LISTENING) but KEEP the session.
        if self._downlink_was_playing:
            self.interrupt_current_response("user_transcript")
        else:
            self._led(LED_LISTENING)
        # Deterministic exit phrases are acted on from the vendor transcript so
        # Live Mode ends for real (not just as spoken words), including while the
        # assistant was speaking.
        r = self.router.route(self._recent_user, "LIVE_MODE")
        if r["route"] == EchoRouter.ROUTE_EXIT:
            self._log(f"[live] exit phrase matched={r.get('matched')!r} (controller shutdown)")
            try:
                if self.detector is not None:
                    self.detector.set_playing(False)
            except Exception:  # noqa: BLE001
                pass
            self.ctl.exit_live_mode("user_requested")
        elif "ok hiyo" in self._recent_user.lower() or "okay hiyo" in self._recent_user.lower():
            self._log("[live] wake word heard while live (ignored, no second session)")
        self._recent_user = "" if r["route"] != EchoRouter.ROUTE_QUESTION else self._recent_user

    def _on_output_audio(self, started: bool) -> None:
        self._led(LED_SPEAKING)
        if self.stats["first_audio_ms"] is None and self._started_at:
            self.stats["first_audio_ms"] = int((time.time() - self._started_at) * 1000)

    def _on_delegation(self, did, prompt: str) -> None:
        self._turns += 1
        self.stats["user_turns"] = self._turns
        self.stats["delegations"] += 1
        self._led(LED_THINKING)
        self._log(f"[live] user_turn={self._turns} session={self.session.session_id if self.session else None}")

    def _on_session_event(self, et: str, m: dict) -> None:
        if et == "error":
            fatal = bool((m.get("error") or {}).get("fatal"))
            self._log(f"[live] session_event error fatal={fatal} msg={(m.get('error') or {}).get('message')}")
            if fatal:
                self._on_fatal("vendor_error")

    def _on_fatal(self, why: str) -> None:
        self._log(f"[live] FATAL vendor reason={why} -> exit")
        if self.ctl.get_state() == self.ctl.LIVE_MODE:
            self.ctl.exit_live_mode(f"live_session_failed:{why}")
        elif self.ctl.get_state() == self.ctl.LIVE_MUTED:
            # mute keeps priority; the session is gone, force IDLE on unmute path
            asyncio.ensure_future(self._close(f"fatal_muted:{why}"))

    def _on_session_closed(self, reason: str, sess) -> None:
        self._log(f"[live] vendor session closed reason={reason} "
                  f"lifetime_s={(sess.closed_at - sess.created_at) if sess.closed_at else 0:.1f}")

    # ── helpers ──────────────────────────────────────────────────────────────
    def feed(self, pcm16: bytes) -> None:
        """Feed the AEC-clean 16 kHz uplink to the vendor mic and the barge detector.

        A single uplink source (the proven Speex AEC residual) drives both; no
        second microphone path is opened.
        """
        sess = self.session
        if sess is None or sess.closed:
            return
        if self.ctl.get_state() != self.ctl.LIVE_MODE:
            return
        if self.ctl.is_mic_muted():
            return
        sess.push_uplink(pcm16)
        if self._barge_enabled and self.detector is not None:
            try:
                if self.detector.feed(pcm16):
                    self.interrupt_current_response("local_vad")
            except Exception:  # noqa: BLE001
                pass

    # ── Phase 5F: barge-in ───────────────────────────────────────────────────
    def interrupt_current_response(self, source: str = "local_vad") -> None:
        """Stop the current response without leaving LIVE_MODE or recreating the
        GPT Live session.

        Steps: invalidate the playback generation, cancel the current Seed-VC
        stream output, flush the queued downlink playback, ask the vendor to stop
        speaking, and show LED_LISTENING.  Physical mute / Action-long keep their
        higher precedence and are handled by the controller, not here.
        """
        sess = self.session
        live_states = (self.ctl.LIVE_MODE, self.ctl.LIVE_MUTED)
        if sess is None or sess.closed or self.ctl.get_state() not in live_states:
            return
        # Debounce: a local onset and the vendor transcript for the same utterance
        # arrive within a few hundred ms, and a single spoken interruption can
        # produce several transcript deltas.  Treat the whole user utterance as one
        # barge so the playback generation advances once per interruption.
        now = time.monotonic()
        speech_active = False
        try:
            speech_active = bool(self.detector and self.detector.speech_active)
        except Exception:  # noqa: BLE001
            speech_active = False
        if self._in_barge and (speech_active or (now - self._last_barge_mono) < 1.5):
            self._log(f"[live] barge_ignored source={source} (same utterance "
                      f"{1000*(now-self._last_barge_mono):.0f}ms)")
            return
        if source == "local_vad":
            self._barge_count += 1
        self._in_barge = True
        self._last_barge_mono = now
        self._led(LED_LISTENING)
        self._downlink_was_playing = False
        try:
            if self.detector is not None:
                self.detector.set_playing(False, now)
        except Exception:  # noqa: BLE001
            pass
        # 1) local playback generation + Seed-VC output gate + queue flush
        gen = sess.begin_barge_in(source)
        try:
            import echo_trace
            echo_trace.trace("barge.interrupt", source=source, gen=gen,
                             queue_ms=round(sess.queue.pending_ms(), 1),
                             session=sess.session_id)
        except Exception:  # noqa: BLE001
            pass
        # 2) deterministic device playback flush (T_FLUSH) if wired
        if self._flush_downlink is not None:
            try:
                self._flush_downlink()
            except Exception:  # noqa: BLE001
                pass
        # 3) Phase 5F.1B context correction. The vendor keeps the interrupted turn
        #    in the Live session context (interruption does not cancel context), so
        #    without an explicit nudge the model can finish the old answer and/or
        #    merge the old and new requests.  Append a *supersede* instruction on
        #    the first barge of each utterance (the debounce above guarantees this
        #    runs once per utterance, whichever source fired first).  This does not
        #    recreate the session or delete context; it only marks the interrupted
        #    turn as obsolete for the model's next response.
        sess.send_client_event({
            "type": "session.instructions.append", "delegation_id": None,
            "event_id": f"barge-{gen}",
            "content": (
                "The user interrupted the previous response. Treat the interrupted "
                "assistant response and any unfinished earlier request as superseded: "
                "do not continue, complete, repeat, or merge them. Stop the previous "
                "answer, listen to the user, and respond only to the user's new "
                "utterance unless the user explicitly refers back to the earlier one."
            ),
        })
        self._downlink_was_playing = False
        self._log(f"[live] barge_interrupt source={source} gen={gen} "
                  f"queue_ms={sess.queue.pending_ms():.0f} session={sess.session_id} "
                  f"(LIVE_MODE preserved)")

    def flush_downlink_for_test(self) -> None:
        if self._flush_downlink is not None:
            self._flush_downlink()

    def note_downlink_frame(self) -> None:
        """Called by the server's downlink task whenever it sends audio frames."""
        self._t_last_downlink_mono = time.monotonic()

    def _led(self, state: str) -> None:
        if state != self._substate:
            self._substate = state
            self.ctl.led_set_state(state, source="live")

    async def _idle_watch(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                sess = self.session
                if sess is None or sess.closed:
                    return
                if self.ctl.is_mic_muted():
                    sess.note_activity()  # a muted device cannot be idle-timed-out silently
                    continue
                idle = sess.idle_seconds()
                if idle >= self.idle_timeout:
                    self._log(f"[live] idle_timeout idle_s={idle:.1f} limit={self.idle_timeout:.0f}")
                    if self.ctl.get_state() == self.ctl.LIVE_MODE:
                        self.ctl.exit_live_mode("idle_timeout")
                    return
        except asyncio.CancelledError:
            pass

    async def _monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.05)
                sess = self.session
                if sess is None or sess.closed:
                    return
                now = time.monotonic()
                # Watch a push *sequence number* rather than the total, so a stale
                # GPT direct response (one big push burst, then silence) is detected
                # in ~250 ms even though its vendor->VC frames keep arriving.
                seq = sess.queue.downlink_push_seq
                if seq != getattr(self, "_seq_seen", -1):
                    self._seq_seen = seq
                    self._t_last_downlink_mono = now
                since = now - self._t_last_downlink_mono
                # The Echo is only "audible" when audio was pushed very recently AND
                # the vendor produced audio recently; otherwise a queued answer that
                # has finished generating must not keep the detector armed (that
                # caused false barge-ins during the user's next utterance).
                vendor_since = now - sess.last_downlink_push_mono
                playing = (since < 0.25 and vendor_since < 0.35
                           and sess.queue.pending_ms() > 0.0)
                # End the barge window once the user has been quiet and playback has
                # resumed (or there is nothing left to interrupt).
                if self._in_barge:
                    quiet = True
                    try:
                        quiet = not (self.detector and self.detector.speech_active)
                    except Exception:  # noqa: BLE001
                        pass
                    if quiet and (now - self._last_barge_mono) > 1.0:
                        self._in_barge = False
                if playing != self._downlink_was_playing:
                    self._downlink_was_playing = playing
                    try:
                        self.detector.set_playing(playing, now)
                    except Exception:  # noqa: BLE001
                        pass
                if not playing:
                    continue
                # 5F watchdog: assistant audio still queued but the vendor has
                # produced nothing for a while -> flush stale playback rather than
                # let a stopped response keep speaking.
                if since > 0.3 and sess.queue.pending_ms() > 0.0:
                    self._log(f"[live] stale_playback_flush since_push_ms={since*1000:.0f} "
                              f"queue_ms={sess.queue.pending_ms():.0f}")
                    sess.begin_barge_in("stale_watchdog")
                    if self._flush_downlink is not None:
                        try:
                            self._flush_downlink()
                        except Exception:  # noqa: BLE001
                            pass
                    self._downlink_was_playing = False
                    continue
                if self._substate == LED_THINKING and sess.queue.pending_ms() > 5.0:
                    self._led(LED_SPEAKING)
                if self._substate == LED_SPEAKING and sess.queue.pending_ms() <= 0.0:
                    self._led(LED_LIVE)
        except asyncio.CancelledError:
            pass

    async def _cancel_delegation(self) -> None:
        if self.session is None or self.session.delegations == 0:
            return
        try:
            import urllib.request
            req = urllib.request.Request(
                self._cancel_url,
                data=json.dumps({"session_key": self.session_key}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=5).read())
            self._log("[live] delegation cancel requested")
        except Exception:  # noqa: BLE001
            pass

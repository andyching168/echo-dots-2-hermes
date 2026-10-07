"""Vendor-only GPT Live bridge for the Echo thin satellite.

Creates an independent OpenAI GPT Live WebRTC session for one Echo TCP audio session.
No client-side WebRTC leg, no changes to the Desktop path. Credentials and session config
are resolved server-side through the existing Hermes tools (`tools.voice_live`).

Audio:
  Echo uplink PCM16 16k mono  -> EchoMicTrack (resampled to 48k) -> vendor PC
  vendor audio track          -> [optional Seed-VC] -> 48k mono queue -> Echo downlink

Phase 4 used this as an always-on ``--gptlive`` mode.  Phase 5E reuses it unchanged
for the audio/delegation core and adds two Echo-specific adapters:

* ``delegate="turn"`` routes ``session.delegation.created`` to the Echo turn server
  (``:8796``) instead of the generic Hermes gateway session, so the delegated turn is
  ``platform="echo"`` and the ``hermes-echo`` plugin injects the live
  ``[Echo Runtime Context]`` (``interaction_mode: live_mode``).
* mic gating (``set_mic_enabled``), user-activity tracking and lifecycle callbacks used
  by ``echo_live.LiveModeSupervisor`` to keep the persistent session in step with the
  authoritative ``EchoController``.

The default (``delegate="gateway"``, no callbacks) reproduces the Phase 4 behaviour.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
import wave
from collections import Counter
from fractions import Fraction
from typing import Optional

import aiohttp
import av
from av.audio.resampler import AudioResampler
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

DOWNLINK_FRAME = 480          # 10 ms @ 48 kHz
DOWNLINK_CAP_FRAMES = 24      # 240 ms bounded queue (matches Echo cap)
MIC_FRAME_48 = 960            # 20 ms @ 48 kHz (aiortc frame size)


class QueueSource:
    """Bounded 48 kHz mono downlink source fed by the GPT (or Seed-VC) output."""

    label = "gptlive"

    def __init__(self, cap_frames: int = DOWNLINK_CAP_FRAMES, prime_frames: int = 0) -> None:
        self._buf = bytearray()
        self._cap = cap_frames * DOWNLINK_FRAME * 2
        self._prime = prime_frames * DOWNLINK_FRAME * 2
        self._primed = self._prime <= 0
        self.pushed_bytes = 0
        self.dropped_bytes = 0
        self.underruns = 0
        self.prime_waits = 0
        self.reprimes = 0
        self.downlink_push_seq = 0
        self.last_push_mono = time.monotonic()

    def push_48k(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        self.pushed_bytes += len(pcm)
        self.downlink_push_seq += 1
        self.last_push_mono = time.monotonic()
        if len(self._buf) > self._cap:
            drop = len(self._buf) - self._cap
            del self._buf[:drop]
            self.dropped_bytes += drop

    def enqueue(self, pcm) -> None:
        """Controller chime compatibility: accepts array.array or bytes."""
        try:
            raw = pcm.tobytes()
        except AttributeError:
            raw = bytes(pcm)
        self.push_48k(raw)

    def pending(self) -> int:
        """Bytes currently queued for playout (Phase 5E drain detection)."""
        return len(self._buf)

    def pending_ms(self) -> float:
        return len(self._buf) / (48000 * 2) * 1000.0

    def flush(self) -> None:
        self._buf.clear()

    def mark_flushed(self) -> None:
        """Reset the freshly-emptied queue as the new baseline, so the supervisor
        does not immediately re-trigger a stale-playback flush."""
        self.downlink_push_seq += 1

    def next_frame(self):
        need = DOWNLINK_FRAME * 2
        if not self._primed:
            if len(self._buf) >= self._prime:
                self._primed = True
            else:
                import array
                self.prime_waits += 1
                return array.array("h", bytes(need))
        if len(self._buf) < need and self._prime > 0:
            # ran dry; re-prime before emitting the next burst to keep it smooth
            self.reprimes += 1
            self._primed = False
            import array
            return array.array("h", bytes(need))
        if len(self._buf) >= need:
            raw = bytes(self._buf[:need])
            del self._buf[:need]
        else:
            raw = bytes(self._buf) + b"\x00" * (need - len(self._buf))
            self._buf.clear()
            self.underruns += 1
        import array

        a = array.array("h")
        a.frombytes(raw)
        return a


class EchoMicTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._buf = bytearray()
        self._pts = 0
        self._resampler = AudioResampler(format="s16", layout="mono", rate=48000)
        self.frames_in = 0

    def push_16k(self, pcm: bytes) -> None:
        n = len(pcm) // 2
        if n <= 0:
            return
        src = av.AudioFrame(format="s16", layout="mono", samples=n)
        src.sample_rate = 16000
        src.planes[0].update(pcm)
        for out in self._resampler.resample(src):
            raw = bytes(out.planes[0])[: out.samples * 2]
            if self._q.full():
                try:
                    self._q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._q.put_nowait(raw)
            except asyncio.QueueFull:
                pass
        self.frames_in += 1

    async def recv(self):
        need = MIC_FRAME_48 * 2
        while len(self._buf) < need:
            try:
                chunk = await asyncio.wait_for(self._q.get(), timeout=0.02)
                self._buf.extend(chunk)
            except asyncio.TimeoutError:
                break
        if len(self._buf) >= need:
            raw = bytes(self._buf[:need])
            del self._buf[:need]
        else:
            raw = bytes(self._buf) + b"\x00" * (need - len(self._buf))
            self._buf.clear()
        frame = av.AudioFrame(format="s16", layout="mono", samples=MIC_FRAME_48)
        frame.sample_rate = 48000
        frame.pts = self._pts
        frame.time_base = Fraction(1, 48000)
        frame.planes[0].update(raw)
        self._pts += MIC_FRAME_48
        return frame


def _resolve_voice_live():
    import sys

    home = os.path.expanduser("~")
    agent = os.path.join(home, ".hermes", "hermes-agent")
    if agent not in sys.path:
        sys.path.insert(0, agent)
    from tools import voice_live  # type: ignore

    return voice_live


class GptLiveSession:
    """Vendor-only GPT Live session; never creates a client-side WebRTC leg."""

    def __init__(self, diag_dir: str, tag: str, use_vc: bool = False, *,
                 delegate: str = "gateway", turn_url: Optional[str] = None,
                 session_key: str = "echo-live", log=None, keep_wavs: bool = True,
                 mic_enabled: bool = True,
                 on_user_transcript=None, on_output_transcript=None,
                 on_delegation=None, on_output_audio=None,
                 on_session_event=None, on_closed=None, on_fatal=None) -> None:
        self.diag_dir = diag_dir
        self.tag = tag
        self.use_vc = use_vc
        self.queue = QueueSource(cap_frames=80, prime_frames=35) if use_vc else QueueSource()
        self.mic = EchoMicTrack()
        self.vendor = None
        self.dc = None
        self.session_id = None
        self.event_counts: Counter = Counter()
        self.delegations = 0
        self.vendor_frames = 0
        self.down_frames = 0
        self.track_task = None
        self.closed = False
        self.started = False
        self.close_reason = None
        self.created_at = time.time()
        self.closed_at = None
        self.mic_enabled = bool(mic_enabled)
        self.last_user_activity = time.time()
        self.user_transcript = ""
        self.assistant_transcript = ""
        self._output_audio_started = False
        self.tool_events: list = []
        self.tool_call_count = 0
        # Echo-specific adapters (Phase 5E). Defaults preserve Phase 4 behaviour.
        self.delegate = delegate
        self.turn_url = (turn_url or os.environ.get("ECHO_TURN_URL", "http://127.0.0.1:8796/turn")).rstrip("/")
        self.session_key = session_key
        self._log = log or (lambda m: print(m, flush=True))
        self._on_user_transcript = on_user_transcript
        self._on_output_transcript = on_output_transcript
        self._on_delegation = on_delegation
        self._on_output_audio = on_output_audio
        self._on_session_event = on_session_event
        self._on_closed = on_closed
        self._on_fatal = on_fatal
        os.makedirs(diag_dir, exist_ok=True)
        self.wav_gpt = None
        self.wav_down = None
        self.wav_vc = None
        if keep_wavs:
            self.wav_gpt = wave.open(os.path.join(diag_dir, f"{tag}_gpt_raw_48k.wav"), "wb")
            self.wav_gpt.setnchannels(1); self.wav_gpt.setsampwidth(2); self.wav_gpt.setframerate(48000)
            self.wav_down = wave.open(os.path.join(diag_dir, f"{tag}_echo_downlink_48k.wav"), "wb")
            self.wav_down.setnchannels(1); self.wav_down.setsampwidth(2); self.wav_down.setframerate(48000)
            if use_vc:
                self.wav_vc = wave.open(os.path.join(diag_dir, f"{tag}_seedvc_22k.wav"), "wb")
                self.wav_vc.setnchannels(1); self.wav_vc.setsampwidth(2); self.wav_vc.setframerate(22050)
        # Phase 4B delegation
        self.voice_live = None
        self.pending_user = ""
        self.hermes_session = None
        self.hermes_live_session = None
        self.last_turn: dict = {}
        self.hermes_url = os.environ.get("HERMES_BASE_URL", "http://127.0.0.1:8642").rstrip("/")
        self.hermes_key = self._load_hermes_key()
        self._reply_seq = 0
        self.trace = open(os.path.join(diag_dir, f"{tag}_delegation_trace.jsonl"), "w")
        # ── Phase 5F: barge-in / stale-audio protection + unified event trace ──
        self.t0_mono = time.monotonic()
        self.events_path = os.path.join(diag_dir, f"{tag}_events.jsonl")
        self._events_fh = open(self.events_path, "w")
        self.playback_generation = 0          # bumped on every barge-in
        self.delegation_generation = 0         # bumped on every barge-in
        self._vc_reset_needed = False          # ask the Seed-VC stream to reset
        self._vc_accept = True                 # gate: drop VC output while False
        self._vc_reset_sent_mono = 0.0
        self.VC_DRAIN_AFTER_RESET_S = 0.6      # discard in-flight pre-reset chunks
        self.last_vendor_audio_mono = self.t0_mono
        self.last_downlink_push_mono = self.t0_mono
        self.barge_count = 0

    # ── Phase 5F trace ────────────────────────────────────────────────────────
    def mark(self, event: str, **fields) -> None:
        """Append one timestamped record (session-relative + wall clock)."""
        try:
            rec = {"m": round(time.monotonic() - self.t0_mono, 6),
                   "t": round(time.time(), 6), "event": event}
            rec.update(fields)
            self._events_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._events_fh.flush()
        except Exception:  # noqa: BLE001
            pass

    def begin_barge_in(self, source: str = "local_vad") -> int:
        """Local barge-in: invalidate current playback and stop feeding old VC audio.

        Does NOT recreate the GPT Live session or stop Hermes work.  Returns the
        new playback generation.
        """
        self.playback_generation += 1
        self.delegation_generation += 1
        self.queue.flush()
        self.queue.mark_flushed()
        self._vc_reset_needed = True
        # Generation-stamped gate: the receiver records the generation in effect
        # when it reads each Seed-VC chunk and drops it if a barge has since
        # advanced the generation.  This is the authoritative stale-audio guard; a
        # new generation's audio (new answer) flows again automatically.
        self._vc_accept = False
        self._output_audio_started = False
        self.barge_count += 1
        self.mark("barge.begin", source=source, gen=self.playback_generation,
                  queue_after=self.queue.pending())
        return self.playback_generation

    def set_accept_vc(self, accept: bool, reason: str = "") -> None:
        if accept and not self._vc_accept:
            self._vc_accept = True
            self.mark("barge.accept_vc", reason=reason)

    async def _reopen_vc_after_drain(self) -> None:
        """Re-open the Seed-VC gate after the reset drain window.  Stale chunks that
        arrive meanwhile are dropped by the receiver; the fresh answer then plays."""
        try:
            await asyncio.sleep(self.VC_DRAIN_AFTER_RESET_S)
        except asyncio.CancelledError:
            return
        self.set_accept_vc(True, reason="post_barge_drain_done")

    @staticmethod
    def _load_hermes_key() -> str:
        k = os.environ.get("HERMES_AGENT_API_SERVER_KEY", "")
        if k:
            return k
        cfg = os.path.expanduser("~/.hermes/hermes-live/config.env")
        try:
            for line in open(cfg):
                line = line.strip()
                if line.startswith("HERMES_AGENT_API_SERVER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        return ""

    async def start(self) -> None:
        voice_live = _resolve_voice_live()
        self.voice_live = voice_live
        live = voice_live._live_section()
        api_key, _ = voice_live._resolve_credentials(live)
        # This stack IS its own server-side media bridge (mic in -> Seed-VC -> speaker out), so it
        # must reach the vendor directly. `voice.gpt_live.base_url` belongs to Hermes' desktop
        # GPT-Live path (pointed at the local 8791 bridge); inheriting it here would double-bridge
        # and double-convert the voice, so pin the vendor URL instead (env-overridable).
        base_url = os.environ.get("ECHO_LIVE_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if not api_key:
            raise RuntimeError("no OpenAI key resolved server-side (voice.gpt_live.api_key / OPENAI_API_KEY)")
        session_cfg = voice_live.build_session_config()

        ice = RTCConfiguration([RTCIceServer(urls=["stun:stun.l.google.com:19302"])])
        self.vendor = RTCPeerConnection(configuration=ice)
        self.vendor.addTrack(self.mic)
        self.dc = self.vendor.createDataChannel("oai-events")

        @self.dc.on("message")
        def _on_msg(message):
            try:
                m = json.loads(message)
                et = str(m.get("type", "?"))
            except Exception:
                m, et = {}, "?"
            self.event_counts[et] += 1
            self.mark("vendor." + et,
                      delta=(str(m.get("delta"))[:80] if m.get("delta") is not None else None),
                      delegation_id=((m.get("delegation") or {}).get("id") if m.get("delegation") else None),
                      code=((m.get("error") or {}).get("code") if m.get("error") else None))
            if et == "session.input_transcript.delta":
                delta = str(m.get("delta") or "")
                # A new user utterance begins a new turn: stop discarding Seed-VC
                # output so the assistant's next answer can be heard.
                self.set_accept_vc(True, reason="input_transcript")
                self.pending_user += delta
                self.user_transcript += delta
                self.last_user_activity = time.time()
                if self._on_user_transcript is not None:
                    try:
                        self._on_user_transcript(delta)
                    except Exception:  # noqa: BLE001
                        pass
            elif et == "session.output_transcript.delta":
                # The model is speaking again -> a new response started; stop
                # discarding Seed-VC output for the interrupted one.
                self.set_accept_vc(True, reason="output_transcript")
                self.assistant_transcript += str(m.get("delta") or "")
                if self._on_output_transcript is not None:
                    try:
                        self._on_output_transcript(str(m.get("delta") or ""))
                    except Exception:  # noqa: BLE001
                        pass
            if et == "session.delegation.created":
                self.delegations += 1
                did = (m.get("delegation") or {}).get("id")
                self.trace.write(json.dumps({"event": "delegation.created",
                                             "delegation_keys": sorted((m.get("delegation") or {}).keys()),
                                             "id": did, "prompt": self.pending_user.strip()[:400]}) + "\n")
                self.trace.flush()
                if self._on_delegation is not None:
                    try:
                        self._on_delegation(did, self.pending_user.strip())
                    except Exception:  # noqa: BLE001
                        pass
                asyncio.ensure_future(self._handle_delegation(did))
            elif et in ("session.started", "session.closed", "error"):
                if self._on_session_event is not None:
                    try:
                        self._on_session_event(et, m)
                    except Exception:  # noqa: BLE001
                        pass
            if len(self.event_counts) <= 40:
                print(f"[dc] {et}", flush=True)


        @self.vendor.on("track")
        def _on_track(track):
            print(f"[vendor] track {track.kind}", flush=True)
            if track.kind == "audio":
                if self.use_vc:
                    self.track_task = asyncio.ensure_future(self._consume_vc(track))
                else:
                    self.track_task = asyncio.ensure_future(self._consume(track))

        @self.vendor.on("connectionstatechange")
        async def _on_state():
            print(f"[vendor] state={self.vendor.connectionState}", flush=True)
            # Phase 5F: a "closed"/"failed" transition is only fatal for our session
            # if we were actually connected and are not already shutting down. The
            # connection legitimately passes through "closed" during teardown.
            if (self.vendor.connectionState in ("failed", "closed") and not self.closed
                    and self.session_id and self.started
                    and self._on_fatal is not None):
                try:
                    self._on_fatal(str(self.vendor.connectionState))
                except Exception:  # noqa: BLE001
                    pass

        offer = await self.vendor.createOffer()
        await self.vendor.setLocalDescription(offer)
        answer = await self._openai_session(api_key, base_url, session_cfg, self.vendor.localDescription.sdp)
        self.session_id = str((answer.get("session") or {}).get("id") or "")
        self.started = True
        transport = answer.get("transport") or {}
        await self.vendor.setRemoteDescription(
            RTCSessionDescription(sdp=transport["sdp"], type="answer")
        )
        print(f"[gpt] session={self.session_id} model={session_cfg.get('model')} "
              f"voice={(session_cfg.get('audio') or {}).get('output', {}).get('voice')} "
              f"delegation={(session_cfg.get('delegation') or {}).get('type')} vc={self.use_vc}", flush=True)
        self._log(f"[live] connected session={self.session_id} delegate={self.delegate} vc={self.use_vc}")


    async def _openai_session(self, api_key, base_url, session_cfg, sdp):
        import aiohttp

        payload = json.dumps({"session": session_cfg, "transport": {"type": "webrtc", "sdp": sdp}}).encode()
        url = f"{base_url.rstrip('/')}/live/sessions"
        async with aiohttp.ClientSession() as http:
            async with http.post(url, data=payload,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"OpenAI live session failed ({resp.status}): {body[:500]}")
                return json.loads(body)

    def _notify_output_audio(self, raw: bytes) -> None:
        if not self._output_audio_started:
            self._output_audio_started = True
        if self._on_output_audio is not None:
            try:
                self._on_output_audio(self._output_audio_started)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _wav_write(w, raw: bytes) -> None:
        if w is None:
            return
        try:
            w.writeframes(raw)
        except Exception:  # noqa: BLE001  (wave closed during teardown)
            pass

    async def _consume(self, track) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=48000)
        first = True
        try:
            while not self.closed:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    raw = bytes(out.planes[0])[: out.samples * 2]
                    self.vendor_frames += 1
                    self.last_vendor_audio_mono = time.monotonic()
                    self._wav_write(self.wav_gpt, raw)
                    if first:
                        first = False
                        self.mark("vendor_audio.first")
                    self.queue.push_48k(raw)
                    self.last_downlink_push_mono = time.monotonic()
                    self._wav_write(self.wav_down, raw)
                    self.down_frames += 1
                    self._notify_output_audio(raw)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[gpt] consume stopped: {type(exc).__name__}: {exc}", flush=True)

    async def _handle_delegation(self, did) -> None:
        prompt = self.pending_user.strip()
        self.pending_user = ""
        if not prompt:
            prompt = "(the user is waiting for a response)"
        dgen = self.delegation_generation
        self.mark("delegation.begin", id=did, dgen=dgen, prompt=prompt[:120])
        self._log(f"[live] delegation_created id={did} prompt={prompt[:120]!r}")
        t0 = time.time()
        try:
            result = await self._hermes_chat(prompt)
        except Exception as exc:  # noqa: BLE001
            result = {"answer": f"Sorry, I could not complete that request ({type(exc).__name__}).",
                      "error": type(exc).__name__}
        self.last_turn = result
        reply = str(result.get("answer") or "")
        for ev in (result.get("tool_events") or []):
            if ev.get("kind") == "call":
                self.tool_call_count += 1
                self.mark("tool.call", name=ev.get("name"))
                self._log(f"[tool-call] name={ev.get('name')} args={ev.get('args', '')}")
            elif ev.get("kind") == "result":
                self.mark("tool.result", name=ev.get("name"),
                          summary=str(ev.get("summary", ""))[:160])
                self._log(f"[tool-result] name={ev.get('name')} summary={ev.get('summary', '')}")
        if dgen != self.delegation_generation:
            # A barge-in happened while Hermes was working: never speak the stale
            # result.  Safe read-only tool work is allowed to finish.
            self.mark("delegation.stale_discard", id=did, dgen=dgen,
                      now_gen=self.delegation_generation, answer_chars=len(reply))
            self._log(f"[live] delegation result discarded (interrupted) id={did} gen={dgen}->{self.delegation_generation}")
            return
        self._log(f"[live] delegation_done id={did} answer_chars={len(reply)} "
                  f"latency_ms={result.get('latency_ms', int((time.time() - t0) * 1000))} "
                  f"platform={result.get('platform')} "
                  f"hook_ran={result.get('hook_ran')} injected={result.get('injected')} "
                  f"context_in_prompt={result.get('context_in_prompt')} "
                  f"tools={result.get('tool_calls')}")
        self._log(f"[live] response_started delegation={did}")
        self.mark("delegation.response_started", id=did, answer_chars=len(reply))
        self.set_accept_vc(True, reason="delegation_commentary")
        self.trace.write(json.dumps({"event": "commentary.append", "id": did,
                                     "prompt_len": len(prompt), "reply_len": len(reply),
                                     "platform": result.get("platform"),
                                     "hook_ran": result.get("hook_ran"),
                                     "injected": result.get("injected"),
                                     "context_in_prompt": result.get("context_in_prompt"),
                                     "tool_calls": result.get("tool_calls")}) + "\n")
        self.trace.flush()
        for chunk in _chunk_reply(reply):
            self._reply_seq += 1
            try:
                self.dc.send(json.dumps({
                    "type": "session.commentary.append",
                    "delegation_id": did,
                    "event_id": f"say-{self._reply_seq}",
                    "content": chunk,
                }))
            except Exception as exc:  # noqa: BLE001
                print(f"[deleg] commentary send failed: {exc}", flush=True)
                break
        self._log(f"[live] response_done delegation={did} chunks={self._reply_seq}")
        print(f"[deleg] replied {len(reply)} chars in {self._reply_seq} chunks", flush=True)


    async def _hermes_chat(self, message: str) -> dict:
        """Delegate one turn to Hermes.

        ``delegate="turn"`` posts to the Echo turn server (``platform=echo`` +
        ``hermes-echo`` context injection); ``delegate="gateway"`` keeps the
        Phase 4 gateway-session behaviour.  Returns a dict either way.
        """
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession() as http:
            if self.delegate == "turn":
                body = {"message": message, "session_key": self.session_key}
                async with http.post(self.turn_url, json=body, timeout=timeout) as r:
                    j = await r.json()
                if not j.get("ok"):
                    raise RuntimeError(str(j.get("error") or "echo turn server error"))
                j.setdefault("answer", "")
                return j
            # Phase 4 gateway-session path (unchanged).
            headers = {"Authorization": f"Bearer {self.hermes_key}", "Content-Type": "application/json"}
            if not self.hermes_session:
                async with http.post(f"{self.hermes_url}/api/sessions",
                                     json={"title": "Echo GPT Live"}, headers=headers, timeout=timeout) as r:
                    j = await r.json()
                    self.hermes_session = str(((j.get("session") or {}).get("id")) or "")
                    print(f"[hermes] session={self.hermes_session}", flush=True)
            note = ""
            if self.voice_live is not None:
                try:
                    note = self.voice_live.voice_live_turn_note()
                except Exception:
                    note = ""
            body = {"message": (note + "\n\n" + message) if note else message}
            async with http.post(f"{self.hermes_url}/api/sessions/{self.hermes_session}/chat",
                                 json=body, headers=headers, timeout=timeout) as r:
                j = await r.json()
            return {"answer": str(((j.get("message") or {}).get("content")) or ""),
                    "platform": "gateway", "session_id": self.hermes_session}


    async def _consume_vc(self, track) -> None:
        """Route vendor audio through the existing Seed-VC server (ws://127.0.0.1:8790).

        Phase 5F: a barge-in sends ``{"type":"reset"}`` on the *existing* per-session
        Seed-VC socket (never restarting the shared service) and gates output via
        ``_vc_accept`` so no stale Seed-VC audio is queued for the interrupted
        response.  The connection and the GPT Live session are preserved.
        """
        import base64
        from websockets.asyncio.client import connect

        to48 = AudioResampler(format="s16", layout="mono", rate=48000)
        to24 = AudioResampler(format="s16", layout="mono", rate=24000)
        rs22 = AudioResampler(format="s16", layout="mono", rate=48000)
        try:
            async with connect("ws://127.0.0.1:8790", open_timeout=5, close_timeout=2,
                               max_size=4 * 1024 * 1024, proxy=None) as vc:
                await vc.send(json.dumps({"type": "reset"}))

                async def sender():
                    try:
                        while not self.closed:
                            if self._vc_reset_needed:
                                self._vc_reset_needed = False
                                try:
                                    await vc.send(json.dumps({"type": "reset"}))
                                    self._vc_reset_sent_mono = time.monotonic()
                                    self.mark("vc.reset_sent")
                                except Exception:  # noqa: BLE001
                                    pass
                                # Drop Seed-VC output for a short drain window: these
                                # are chunks generated for the interrupted response.
                                # After the window the fresh answer is accepted.
                                asyncio.ensure_future(self._reopen_vc_after_drain())
                            frame = await track.recv()
                            for o48 in to48.resample(frame):
                                raw48 = bytes(o48.planes[0])[: o48.samples * 2]
                                self._wav_write(self.wav_gpt, raw48)
                                self.vendor_frames += 1
                                self.last_vendor_audio_mono = time.monotonic()
                                for o24 in to24.resample(o48):
                                    pcm = bytes(o24.planes[0])[: o24.samples * 2]
                                    await vc.send(json.dumps({
                                        "type": "audio", "mimeType": "audio/pcm;rate=24000",
                                        "data": base64.b64encode(pcm).decode("ascii"),
                                    }))
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001  (MediaStreamError on teardown, WS closed)
                        pass

                async def receiver():
                    try:
                        async for raw in vc:
                            if not isinstance(raw, str):
                                continue
                            m = json.loads(raw)
                            if m.get("type") != "audio" or not m.get("data"):
                                continue
                            # Generation guard: if a barge happened and this chunk
                            # still belongs to the interrupted generation (its
                            # arrival is before the re-accept), drop it.  A fresh
                            # answer re-opens acceptance via set_accept_vc().
                            if not self._vc_accept:
                                self.mark("vc.discard", gen=self.playback_generation)
                                continue
                            pcm = base64.b64decode(m["data"])
                            self._wav_write(self.wav_vc, pcm)
                            n = len(pcm) // 2
                            if n <= 0:
                                continue
                            src = av.AudioFrame(format="s16", layout="mono", samples=n)
                            src.sample_rate = 22050
                            src.planes[0].update(pcm)
                            for o48 in rs22.resample(src):
                                raw48 = bytes(o48.planes[0])[: o48.samples * 2]
                                self.queue.push_48k(raw48)
                                self.last_downlink_push_mono = time.monotonic()
                                self._wav_write(self.wav_down, raw48)
                                self.down_frames += 1
                                self._notify_output_audio(raw48)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        pass

                a = asyncio.ensure_future(sender())
                b = asyncio.ensure_future(receiver())
                try:
                    await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in (a, b):
                        if not t.done():
                            t.cancel()
                    for t in (a, b):
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                        except Exception:  # noqa: BLE001
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[gpt+vc] stopped: {type(exc).__name__}: {exc}", flush=True)

    def push_uplink(self, pcm16_16k: bytes) -> None:
        if not self.mic_enabled:
            return
        self.mic.push_16k(pcm16_16k)

    def set_mic_enabled(self, enabled: bool) -> None:
        self.mic_enabled = bool(enabled)

    def note_activity(self) -> None:
        self.last_user_activity = time.time()

    def idle_seconds(self) -> float:
        return time.time() - self.last_user_activity

    def send_client_event(self, event: dict) -> bool:
        try:
            if self.dc is not None and self.dc.readyState == "open":
                self.dc.send(json.dumps(event))
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def close(self, reason: str = "close") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        self.closed_at = time.time()
        self.mark("session.close", reason=reason)
        # Best-effort vendor graceful close before tearing the transport down.
        self.send_client_event({"type": "session.close"})
        if self.track_task:
            self.track_task.cancel()
        if self.dc:
            try:
                self.dc.close()
            except Exception:
                pass
        if self.vendor:
            try:
                await self.vendor.close()
            except Exception:
                pass
        try:
            self.trace.close()
        except Exception:
            pass
        try:
            self._events_fh.close()
        except Exception:
            pass
        for w in (self.wav_gpt, self.wav_down, self.wav_vc):
            try:
                if w:
                    w.close()
            except Exception:
                pass
        life = (self.closed_at - self.created_at) if self.closed_at else 0.0
        print(f"[gpt] queue prime_waits={self.queue.prime_waits} reprimes={self.queue.reprimes} "
              f"underruns={self.queue.underruns} drop_bytes={self.queue.dropped_bytes}", flush=True)
        print(f"[gpt] closed session={self.session_id} vendor_audio_frames={self.vendor_frames} "
              f"down_frames={self.down_frames} delegations={self.delegations} "
              f"events={dict(self.event_counts.most_common(12))}", flush=True)
        self._log(f"[live] close reason={reason} session={self.session_id} "
                  f"duration_s={life:.1f} delegations={self.delegations} "
                  f"vendor_frames={self.vendor_frames} down_frames={self.down_frames}")
        if self._on_closed is not None:
            try:
                self._on_closed(reason, self)
            except Exception:  # noqa: BLE001
                pass



def _chunk_reply(text: str, max_len: int = 240):
    import re
    parts = re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+", text.strip())
    out = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if len(buf) + len(p) + 1 <= max_len:
            buf = (buf + " " + p).strip()
        else:
            if buf:
                out.append(buf)
            buf = p if len(p) <= max_len else p[:max_len]
    if buf:
        out.append(buf)
    return out or ([text[:max_len]] if text else [])

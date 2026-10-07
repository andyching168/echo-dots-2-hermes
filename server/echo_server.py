#!/usr/bin/env python3
"""Phase 2 Echo Dot 2 full-duplex audio tunnel: uplink receiver + downlink sender.

Uplink (Phase 1, unchanged): framed PCM16 16 kHz mono near-end (AEC residual) from
the Echo, written to WAV with framing/timing diagnostics.

Downlink (Phase 2): deterministic 48 kHz mono PCM16 test audio (tone / pink /
speech / alternating speech+silence / sequence), paced at 10 ms frames on the same
full-duplex TCP connection. No OpenAI, no Seed-VC, no Hermes logic.

Wire protocol (little-endian, v0):
    magic u16 = 0xE0D1 | type u8 | flags u8 | seq u32 | len u16 | payload

Types:
    0x01 UPLINK    client->server : u64 echo_mono_us | int16 PCM 16k mono (160 smp)
    0x02 DOWNLINK  server->client : int16 PCM 48k mono (480 smp = 10 ms)
    0x03 METER     client->server : v2 stats (see MeterV2)
    0x80 FLUSH     server->client : drop buffered playback
    0x84 HELLO     server->client : u64 server_epoch_us
    0x85 HELLO_ACK client->server : u64 echo_mono_us_at_hello
"""

from __future__ import annotations

import argparse
import array
import asyncio
import math
import os
import struct
import time
import wave
from dataclasses import dataclass, field
from typing import Optional

MAGIC = 0xE0D1
HDR = struct.Struct("<HBBIH")
T_UPLINK = 0x01
T_DOWNLINK = 0x02
T_METER = 0x03
T_FLUSH = 0x80
T_HELLO = 0x84
T_HELLO_ACK = 0x85
METER_V2 = struct.Struct("<IIiIIIIIIIii")
# fields: cap_errs, up_frames, erle_milli,
#         dl_recv, dl_gaps, dl_dupes,
#         underruns, drops, q_depth,
#         q_max, ref_rms, out_rms

SR = 48000
FRAME = 480
TICK = 0.001
MAX_CATCHUP = 200
SRC_FILES = os.path.expanduser("~/voice/echo-bridge/test_audio")

# Phase 5A wake-word runtime (optional). wakeword.py + wakeword_models/ live
# next to this file; run this server under the isolated wake-word venv when
# --wakeword is used (it carries openwakeword/onnxruntime/numpy). The uplink
# framing, AEC and downlink cadence are unchanged.
WAKEWORD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wakeword_models")

# Single-active-audio-client guard: a new Echo audio connection supersedes the
# previous one so the shared controller never ends up with two orchestrators.
_ACTIVE_AUDIO = {"writer": None, "task": None, "gen": 0}
# If no uplink frame arrives for this long, treat the connection as dead and
# tear down its orchestrator (prevents stale half-open connections).
UPLINK_IDLE_TIMEOUT = 30.0


class Source:
    label = "?"

    def next_frame(self) -> array.array:
        raise NotImplementedError

    def skip(self, n: int) -> None:
        raise NotImplementedError


class BufferSource(Source):
    """Looped precomputed buffer; frame extraction is pure array slicing (fast)."""

    def __init__(self, label: str, pcm: array.array) -> None:
        if len(pcm) < FRAME:
            raise ValueError(f"{label}: buffer too short ({len(pcm)})")
        self.label = label
        self.pcm = pcm
        self.pos = 0

    def next_frame(self) -> array.array:
        n = len(self.pcm)
        end = self.pos + FRAME
        if end <= n:
            frame = self.pcm[self.pos:end]
            self.pos = end if end < n else 0
            return frame
        head = self.pcm[self.pos:]
        tail = self.pcm[: end - n]
        frame = array.array("h", head)
        frame.extend(tail)
        self.pos = end - n
        return frame

    def skip(self, n: int) -> None:
        self.pos = (self.pos + n) % len(self.pcm)


def make_tone(freq: float = 1000.0, amp: float = 0.1, sec: float = 1.0) -> array.array:
    n = int(SR * sec)
    a = array.array("h", bytes(n * 2))
    w = 2.0 * math.pi * freq / SR
    for i in range(n):
        a[i] = int(amp * 32767.0 * math.sin(w * i))
    return a


def make_pink(amp: float = 0.18, sec: float = 10.0, seed: int = 123456789) -> array.array:
    n = int(SR * sec)
    a = array.array("h", bytes(n * 2))
    state = seed & 0x7FFFFFFF
    b = [0.0] * 7
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        w = state / 0x3FFFFFFF - 1.0
        b[0] = 0.99886 * b[0] + w * 0.0555179
        b[1] = 0.99332 * b[1] + w * 0.0750759
        b[2] = 0.96900 * b[2] + w * 0.1538520
        b[3] = 0.86650 * b[3] + w * 0.3104856
        b[4] = 0.55000 * b[4] + w * 0.5329522
        b[5] = -0.7616 * b[5] - w * 0.0168980
        pink = (b[0] + b[1] + b[2] + b[3] + b[4] + b[5] + b[6] + w * 0.5362) * 0.11
        b[6] = w * 0.115926
        a[i] = int(max(-1.0, min(1.0, pink * amp / 0.5)) * 32767.0)
    return a


def load_wav_mono(path: str, sr: int = SR) -> array.array:
    w = wave.open(path, "rb")
    ch = w.getnchannels()
    sw = w.getsampwidth()
    rate = w.getframerate()
    raw = w.readframes(w.getnframes())
    w.close()
    if sw != 2:
        raise ValueError(f"{path}: only 16-bit supported")
    src = array.array("h")
    src.frombytes(raw)
    if ch > 1:
        mono = array.array("h", bytes((len(src) // ch) * 2))
        for i in range(len(mono)):
            acc = 0
            base = i * ch
            for c in range(ch):
                acc += src[base + c]
            mono[i] = acc // ch
        src = mono
    if rate != sr:
        ratio = sr / rate
        outn = int(len(src) / ratio)
        out = array.array("h", bytes(outn * 2))
        for i in range(outn):
            x = i * ratio
            i0 = int(x)
            i1 = min(i0 + 1, len(src) - 1)
            frac = x - i0
            out[i] = int(src[i0] * (1 - frac) + src[i1] * frac)
        src = out
    if len(src) < FRAME:
        raise ValueError(f"{path}: too short")
    return src


def make_alt(speech: array.array, on_s: float = 3.0, off_s: float = 2.0, cycles: int = 6) -> array.array:
    on = int(on_s * SR)
    off = int(off_s * SR)
    out = array.array("h")
    for _ in range(cycles):
        take = min(on, len(speech))
        out.extend(speech[:take])
        if take < on:
            out.extend(array.array("h", bytes((on - take) * 2)))
        out.extend(array.array("h", bytes(off * 2)))
    return out


class SequenceSource(Source):
    def __init__(self, segments) -> None:
        self.segments = segments
        self.idx = 0
        self.remaining = segments[0][0]
        self.label = segments[0][1].label

    def next_frame(self) -> array.array:
        seg = self.segments[self.idx]
        frame = seg[1].next_frame()
        self.remaining -= 1
        if self.remaining <= 0:
            self.idx = (self.idx + 1) % len(self.segments)
            self.remaining = self.segments[self.idx][0]
            self.label = self.segments[self.idx][1].label
        return frame

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()


def build_source(name: str, speech_path: str) -> Source:
    speech = load_wav_mono(speech_path) if (name != "tone" and name != "pink" and os.path.exists(speech_path)) else None
    if name == "tone":
        return BufferSource("tone1k", make_tone())
    if name == "pink":
        return BufferSource("pink", make_pink())
    if name == "speech":
        if speech is None:
            raise SystemExit(f"speech source needs a WAV at {speech_path}")
        return BufferSource("speech", speech)
    if name == "alt":
        if speech is None:
            raise SystemExit(f"alt source needs a WAV at {speech_path}")
        return BufferSource("alt_speech_silence", make_alt(speech))
    tone = BufferSource("tone1k", make_tone())
    pink = BufferSource("pink", make_pink())
    alt = BufferSource("alt_speech_silence", make_alt(speech)) if speech is not None else BufferSource("tone440", make_tone(440.0, 0.1))
    sp = BufferSource("speech", speech) if speech is not None else BufferSource("tone660", make_tone(660.0, 0.1))
    segs = [
        (3 * SR // FRAME, tone),
        (3 * SR // FRAME, pink),
        (10 * SR // FRAME, alt),
        (10 * SR // FRAME, sp),
    ]
    return SequenceSource(segs)


@dataclass
class Stats:
    frames: int = 0
    samples: int = 0
    bytes_in: int = 0
    first_seq: Optional[int] = None
    last_seq: Optional[int] = None
    gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    parse_errors: int = 0
    last_arrival: Optional[float] = None
    inter_sum: float = 0.0
    inter_sq: float = 0.0
    inter_n: int = 0
    offset_us: Optional[int] = None
    lat_sum: float = 0.0
    lat_min: float = float("inf")
    lat_max: float = 0.0
    lat_n: int = 0
    dev_cap_errs: int = 0
    dev_up_frames: int = 0
    dev_erle_milli: int = 0
    dev_dl_recv: int = 0
    dev_dl_gaps: int = 0
    dev_dl_dupes: int = 0
    dev_underruns: int = 0
    dev_drops: int = 0
    dev_q_depth: int = 0
    dev_q_max: int = 0
    dev_ref_rms: int = 0
    dev_out_rms: int = 0
    downlink_frames: int = 0
    downlink_bytes: int = 0
    started: float = field(default_factory=time.time)

    def seq_seen(self, seq: int) -> None:
        if self.first_seq is None:
            self.first_seq = seq
            self.last_seq = seq
            return
        expected = (self.last_seq + 1) & 0xFFFFFFFF
        delta = (seq - expected) & 0xFFFFFFFF
        if seq == expected:
            pass
        elif seq == self.last_seq:
            self.duplicates += 1
        elif delta < 0x80000000:
            self.gaps += delta
        else:
            self.out_of_order += 1
        if ((seq - self.last_seq) & 0xFFFFFFFF) < 0x80000000:
            self.last_seq = seq

    def summary(self) -> str:
        wall = max(1e-9, time.time() - self.started)
        eff = self.samples / wall
        im = self.inter_sum / self.inter_n if self.inter_n else 0.0
        iv = (self.inter_sq / self.inter_n - im * im) if self.inter_n else 0.0
        lat = "n/a"
        if self.lat_n:
            lat = f"min={self.lat_min:.1f} mean={self.lat_sum / self.lat_n:.1f} max={self.lat_max:.1f} ms"
        return (
            f"UP frames={self.frames} samples={self.samples} bytes={self.bytes_in} "
            f"gaps={self.gaps} dupes={self.duplicates} ooo={self.out_of_order} parse_errors={self.parse_errors} "
            f"wall={wall:.2f}s eff_rate={eff:.1f}Hz inter_mean={im:.2f}ms std={iv ** 0.5:.2f}ms lat {lat}\n"
            f"     DL sent={self.downlink_frames} bytes={self.downlink_bytes} | "
            f"ECHO dl_recv={self.dev_dl_recv} dl_gaps={self.dev_dl_gaps} dl_dupes={self.dev_dl_dupes} "
            f"underruns={self.dev_underruns} drops={self.dev_drops} q_depth={self.dev_q_depth} q_max={self.dev_q_max} "
            f"ref_rms={self.dev_ref_rms} out_rms={self.dev_out_rms} erle={self.dev_erle_milli / 1000.0:.2f}dB "
            f"cap_errs={self.dev_cap_errs} up_frames={self.dev_up_frames}"
        )


class EchoSession:
    def __init__(self, wav_path: str, source: Source) -> None:
        self.stats = Stats()
        self.wav_path = wav_path
        self.wav = wave.open(wav_path, "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(16000)
        self.hello_us = int(time.time() * 1_000_000)
        self.source = source
        self.sendlog = ""
        self.dl_seq = 0
        self.last_label = None
        self.downlink_task: Optional[asyncio.Task] = None
        # Phase 5F: downlink writer + flush hooks for Echo-side barge-in.
        self.writer = None
        self.on_downlink_frame = None      # informs the supervisor the Echo is audible
        self.on_downlink_flush = None      # deterministic device playback flush (T_FLUSH)
        self.flush_requested = False
        # Phase 5D.6A instrumentation: optional raw capture of the exact downlink PCM.
        self.dl_capture_path = os.getenv("ECHO_DL_CAPTURE", "")
        self.dl_capture = None
        if self.dl_capture_path:
            try:
                self.dl_capture = open(self.dl_capture_path, "wb")
                print(f"[downlink] capturing raw PCM -> {self.dl_capture_path}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[downlink] capture open failed: {exc}", flush=True)

    def flush(self) -> None:
        try:
            self.wav.close()
        except Exception:
            pass
        if getattr(self, "dl_capture", None) is not None:
            try:
                self.dl_capture.close()
            except Exception:
                pass
            self.dl_capture = None

    def handle(self, msg_type: int, seq: int, payload: bytes) -> None:
        now = time.time()
        if self.stats.last_arrival is not None:
            dt = (now - self.stats.last_arrival) * 1000.0
            self.stats.inter_sum += dt
            self.stats.inter_sq += dt * dt
            self.stats.inter_n += 1
        self.stats.last_arrival = now

        if msg_type == T_UPLINK:
            if len(payload) < 8:
                self.stats.parse_errors += 1
                return
            echo_mono_us = struct.unpack_from("<Q", payload, 0)[0]
            pcm = payload[8:]
            self.stats.frames += 1
            self.stats.bytes_in += len(payload)
            self.stats.seq_seen(seq)
            self.wav.writeframes(pcm)
            self.stats.samples += len(pcm) // 2
            if self.stats.offset_us is not None:
                est = echo_mono_us + self.stats.offset_us
                lat_ms = (int(now * 1_000_000) - est) / 1000.0
                if -5.0 < lat_ms < 2000.0:
                    self.stats.lat_sum += lat_ms
                    self.stats.lat_min = min(self.stats.lat_min, lat_ms)
                    self.stats.lat_max = max(self.stats.lat_max, lat_ms)
                    self.stats.lat_n += 1
        elif msg_type == T_METER:
            if len(payload) >= METER_V2.size:
                (
                    self.stats.dev_cap_errs,
                    self.stats.dev_up_frames,
                    self.stats.dev_erle_milli,
                    self.stats.dev_dl_recv,
                    self.stats.dev_dl_gaps,
                    self.stats.dev_dl_dupes,
                    self.stats.dev_underruns,
                    self.stats.dev_drops,
                    self.stats.dev_q_depth,
                    self.stats.dev_q_max,
                    self.stats.dev_ref_rms,
                    self.stats.dev_out_rms,
                ) = METER_V2.unpack_from(payload, 0)
            else:
                self.stats.parse_errors += 1
        elif msg_type == T_HELLO_ACK:
            if len(payload) >= 8:
                echo_mono_hello = struct.unpack_from("<Q", payload, 0)[0]
                self.stats.offset_us = self.hello_us - echo_mono_hello
        else:
            self.stats.parse_errors += 1

    async def downlink(self, writer: asyncio.StreamWriter) -> None:
        start = asyncio.get_running_loop().time()
        sent = 0
        sl = open(self.sendlog, "w") if self.sendlog else None
        last_tick = None
        max_gap = 0.0
        catchup = 0
        try:
            while True:
                lt = asyncio.get_running_loop().time()
                if last_tick is not None:
                    g = lt - last_tick
                    if g > max_gap: max_gap = g
                    if g > 0.05: catchup += 1
                last_tick = lt
                target = int((lt - start) * SR) // FRAME
                need = target - sent
                if need < 0:
                    need = 0
                if need > MAX_CATCHUP:
                    need = MAX_CATCHUP
                for _ in range(need):
                    pcm = self.source.next_frame()
                    if self.dl_capture is not None:
                        self.dl_capture.write(pcm.tobytes())
                    writer.write(
                        HDR.pack(MAGIC, T_DOWNLINK, 0, self.dl_seq, FRAME * 2) + pcm.tobytes()
                    )
                    self.dl_seq = (self.dl_seq + 1) & 0xFFFFFFFF
                    sent += 1
                    self.stats.downlink_frames += 1
                    self.stats.downlink_bytes += FRAME * 2
                # Phase 5F: deterministic playback flush when a barge-in requested it.
                if self.flush_requested:
                    self.flush_requested = False
                    try:
                        writer.write(HDR.pack(MAGIC, T_FLUSH, 0, self.dl_seq, 0))
                        if self.on_downlink_flush is not None:
                            self.on_downlink_flush()
                    except Exception:  # noqa: BLE001
                        pass
                if need > 0 and self.on_downlink_frame is not None:
                    self.on_downlink_frame()
                if self.source.label != self.last_label:
                    self.last_label = self.source.label
                    print(f"[downlink] source -> {self.last_label}", flush=True)
                if sl is not None and need > 0:
                    sl.write(f"{lt:.6f},{need},{sent}\n")
                await writer.drain()
                await asyncio.sleep(TICK)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[downlink] stopped: {type(exc).__name__}: {exc}", flush=True)


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    outdir: str,
    source_name: str,
    speech_path: str,
    sendlog: str = "",
    gptlive: bool = False,
    vc: bool = False,
    oneshot_vc: bool | None = None,
    live_vc: bool | None = None,
    oneshot_tts: str = "direct",
    wakeword: bool = False,
    ww_threshold: float = 0.50,
    ww_cooldown: float = 2.0,
    ww_model: str = "ok_hiyo_v1_selfcontained.onnx",
    ww_confirm_frames: int = 4,
    ww_confirm_avg: float = 0.55,
    oneshot: bool = False,
    live: bool = False,
    live_idle_timeout: float = 120.0,
    barge: bool = True,
    controller=None,
    rig_wav: str = "",
    wakeword_wake: bool = False,
    spotify: bool = False,
) -> None:
    peer = writer.get_extra_info("peername")
    # Supersede any previous audio client: a stale/duplicate connection would
    # otherwise run a second orchestrator against the same shared controller.
    _ACTIVE_AUDIO["gen"] += 1
    _audio_gen = _ACTIVE_AUDIO["gen"]
    _prev_writer = _ACTIVE_AUDIO.get("writer")
    _prev_task = _ACTIVE_AUDIO.get("task")
    _ACTIVE_AUDIO["writer"] = writer
    _ACTIVE_AUDIO["task"] = asyncio.current_task()
    if _prev_writer is not None and _prev_writer is not writer:
        print("[audio] new client; closing previous audio connection", flush=True)
        try:
            _prev_writer.close()
        except Exception:  # noqa: BLE001
            pass
        if _prev_task is not None and not _prev_task.done():
            _prev_task.cancel()
    try:
        import socket as _socket

        _sock = writer.get_extra_info("socket")
        if _sock is not None:
            _sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
    except Exception:
        pass
    ts = time.strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(outdir, f"phase2_{ts}.wav")
    sess = EchoSession(wav_path, build_source(source_name, speech_path))
    sess.sendlog = sendlog
    gpt = None
    if gptlive:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import echo_gpt
        gpt = echo_gpt.GptLiveSession(outdir, f"phase4_{ts}", use_vc=vc)
        sess.source = gpt.queue

    ww_source = None
    ww_detector = None
    if wakeword and gptlive:
        print("[wakeword] --gptlive and --wakeword are mutually exclusive; disabling wakeword", flush=True)
        wakeword = False
    if wakeword_wake and gptlive:
        print("[wakeword-wake] --gptlive and --wakeword-wake are mutually exclusive; disabling wakeword-wake", flush=True)
        wakeword_wake = False
    if wakeword_wake and wakeword:
        print("[wakeword-wake] --wakeword and --wakeword-wake both set; using --wakeword-wake", flush=True)
        wakeword = False
    if wakeword:
        import wakeword as _wwmod
        ww_source = _wwmod.WakeWordSource()
        sess.source = ww_source
        ww_log = os.path.join(outdir, f"wakeword_{ts}.csv")
        _loop = asyncio.get_running_loop()

        if controller is not None:
            controller.set_sink(ww_source)

            def _ww_on_trigger(score, _loop=_loop, _ctl=controller):
                # detector worker thread -> event loop -> authoritative controller
                _loop.call_soon_threadsafe(_ctl.wake, "wakeword")
        else:
            # standalone fallback (Phase 5A behaviour)
            _ack_pcm = _wwmod.make_ack_chime()

            def _enqueue_ack(score, _src=ww_source, _ack=_ack_pcm):
                _src.enqueue(_ack)
                print(f"[wakeword] playing acknowledgement chime (score={score:.2f})", flush=True)

            def _ww_on_trigger(score, _loop=_loop, _fn=_enqueue_ack):
                _loop.call_soon_threadsafe(_fn, score)

        ww_detector = _wwmod.WakeWordDetector(
            model_paths=[os.path.join(WAKEWORD_DIR, ww_model)],
            melspec_path=os.path.join(WAKEWORD_DIR, "melspectrogram.onnx"),
            embedding_path=os.path.join(WAKEWORD_DIR, "embedding_model.onnx"),
            threshold=ww_threshold,
            cooldown=ww_cooldown,
            confirm_frames=ww_confirm_frames,
            confirm_avg=ww_confirm_avg,
            on_trigger=_ww_on_trigger,
            log_csv=ww_log,
        )
        print(
            f"[wakeword] detector ready label={ww_detector.label} "
            f"threshold={ww_threshold:.2f} cooldown={ww_cooldown:.2f}s "
            f"confirm={ww_confirm_frames}x frames avg>={ww_confirm_avg:.2f} log={ww_log}",
            flush=True,
        )

    orch = None
    if oneshot:
        _sys2 = __import__("sys")
        _sys2.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        if controller is None:
            print("[oneshot] requires --control; one-shot disabled", flush=True)
        else:
            import echo_oneshot
            _oneshot_vc = vc if oneshot_vc is None else bool(oneshot_vc)
            orch = echo_oneshot.OneShotOrchestrator(
                controller, use_vc=_oneshot_vc, tts_backend=oneshot_tts)
            sess.source = orch.source
            controller.set_sink(orch.source)
            voice_path = "TTS -> Seed-VC" if _oneshot_vc else "raw TTS"
            print(f"[oneshot] orchestrator active (VAD -> STT -> router -> Hermes -> {voice_path})",
                  flush=True)

    live_sup = None
    if live and orch is not None:
        _sys3 = __import__("sys")
        _sys3.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import echo_live

        def _live_swap(src, _sess=sess, _ctl=controller):
            old = _sess.source
            if old is not src and hasattr(old, "flush"):
                try:
                    old.flush()
                except Exception:  # noqa: BLE001
                    pass
            _sess.source = src
            _ctl.set_sink(src)

        def _live_flush(_sess=sess):
            # Ask the downlink task to emit a T_FLUSH (drop device ring buffer) and
            # record it against the current playback generation.
            _sess.flush_requested = True
            src = _sess.source
            if hasattr(src, "mark_flushed"):
                try:
                    src.mark_flushed()
                except Exception:  # noqa: BLE001
                    pass
            try:
                import echo_trace
                echo_trace.trace("flush.requested", dl_seq=_sess.dl_seq)
            except Exception:  # noqa: BLE001
                pass

        live_sup = echo_live.LiveModeSupervisor(
            controller, source_swap=_live_swap, default_source=orch.source,
            log=lambda m: print(m, flush=True), diag_dir=outdir,
            use_vc=(vc if live_vc is None else bool(live_vc)),
            idle_timeout=live_idle_timeout, barge=barge,
            flush_downlink=(_live_flush if barge else None),
        )
        # The downlink task informs the supervisor when the Echo is actually audible,
        # so barge-in only arms while Hiyo is speaking.
        def _on_dl_frame(_sup=live_sup):
            _sup.note_downlink_frame()
        if rig_wav:
            # Phase 5F.1: no-billing detector rig — real device audio path, local
            # 48 kHz Hiyori WAV source, no GPT Live session / no API usage.
            try:
                import p5f1_rig
                live_sup._session_factory = p5f1_rig.RigSessionFactory(
                    rig_wav, detector_getter=lambda _s=live_sup: _s.detector)
                print(f"[rig] no-billing session factory active wav={rig_wav}",
                      flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[rig] setup failed: {type(exc).__name__}: {exc}", flush=True)

        sess.on_downlink_frame = _on_dl_frame

        def _on_dl_flush(_sess=sess):
            try:
                import echo_trace
                echo_trace.trace("flush.sent", dl_seq=_sess.dl_seq)
            except Exception:  # noqa: BLE001
                pass

        sess.on_downlink_flush = _on_dl_flush
        orch.attach_live(live_sup)
    print(f"[live] supervisor attached (idle_timeout={live_idle_timeout:.0f}s barge={barge})",
          flush=True)

    # ------------------------------------------------------------------
    # Phase 5G: Spotify Connect music source mixed into the same downlink.
    # spotifyd (pipe) -> ffmpeg (48k mono) runs on the server; the mixed frames
    # ride the existing echo_probe downlink, so the device is unchanged.
    # Music is ducked (silenced) while Hiyo is interacting with the user.
    # ------------------------------------------------------------------
    spotify_src = None
    if spotify and oneshot and orch is not None:
        import echo_spotify
        spotify_src = echo_spotify.SpotifySource(log=lambda m: print(m, flush=True))

        def _duck() -> bool:
            # Duck while the user is being served so music never masks the answer.
            try:
                return controller.get_state() in (controller.LISTEN_ONE_SHOT,
                                                  controller.QUESTION_ACTIVE,
                                                  controller.LIVE_MODE)
            except Exception:  # noqa: BLE001
                return False

        _hiyo_src = orch.source
        _mix = echo_spotify.MixSource(_hiyo_src, spotify_src, _duck,
                                      log=lambda m: print(m, flush=True))
        sess.source = _mix
        controller.set_sink(_mix)
        if "spotify_src" not in _ACTIVE_AUDIO:
            _ACTIVE_AUDIO["spotify_src"] = spotify_src
            spotify_src.start()
            print("[spotify] Connect source active (spotifyd -> ffmpeg -> downlink mix)",
                  flush=True)
        else:
            spotify_src = _ACTIVE_AUDIO["spotify_src"]
        print(f"[spotify] --live enabled; Live Mode output bypasses the music mix "
              f"(listen to music by pressing Action / saying the wake word)",
              flush=True) if live_sup is not None else None

    # ------------------------------------------------------------------
    # Phase 5H: on-demand internet-radio streaming for the HA media_player.
    # A `RadioMixSource` wraps the current Hiyo source and mixes a live stream;
    # start/stop are driven by the controller `stream`/`stopstream` commands.
    # ------------------------------------------------------------------
    if oneshot and orch is not None:
        import echo_spotify as _es
        _radio_state = {"src": None, "base_src": None}

        def _radio_duck() -> bool:
            try:
                return controller.get_state() in (controller.LISTEN_ONE_SHOT,
                                                  controller.QUESTION_ACTIVE,
                                                  controller.LIVE_MODE)
            except Exception:  # noqa: BLE001
                return False

        def _radio_start(url: str) -> bool:
            try:
                if _radio_state["src"] is not None:
                    _radio_stop()
                base = orch.source          # the oneshot/spotify mix
                rs = _es.LiveStreamSource(url, log=lambda m: print(m, flush=True))
                rs.start()
                mix = _es.RadioMixSource(base, rs, _radio_duck,
                                         log=lambda m: print(m, flush=True))
                _radio_state["src"] = rs
                _radio_state["base_src"] = base
                sess.source = mix
                controller.set_sink(mix)
                controller.enter_announcing("radio")
                print(f"[radio] streaming {url[:70]}", flush=True)
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"[radio] start failed: {type(exc).__name__}: {exc}", flush=True)
                return False

        def _radio_stop() -> bool:
            rs = _radio_state.get("src")
            base = _radio_state.get("base_src")
            if rs is None:
                return False
            try:
                rs.stop()
            except Exception:  # noqa: BLE001
                pass
            _radio_state["src"] = None
            _radio_state["base_src"] = None
            if base is not None:
                sess.source = base
                controller.set_sink(base)
            controller.exit_announcing("radio_stop")
            print("[radio] stopped", flush=True)
            return True

        controller.attach_radio(_radio_start, _radio_stop)

    # --wakeword-wake detector is constructed AFTER the T_HELLO handshake (further
    # down) so model loading can never delay the hello and make echo_probe reconnect.

    if gptlive:
        src_label = "gptlive"
    elif oneshot:
        src_label = "oneshot"
    elif wakeword:
        src_label = "wakeword"
    else:
        src_label = source_name
    if wakeword_wake:
        src_label += "+wakeword-wake"
    if spotify:
        src_label += "+spotify"
    print(f"[{ts}] client connected: {peer} -> {wav_path} source={src_label}", flush=True)

    writer.write(HDR.pack(MAGIC, T_HELLO, 0, 0, 8) + struct.pack("<Q", sess.hello_us))
    await writer.drain()
    sess.writer = writer
    if gpt is not None:
        try:
            await gpt.start()
        except Exception as exc:  # noqa: BLE001
            print(f"[gpt] start failed: {type(exc).__name__}: {exc}", flush=True)
            await gpt.close()
            gpt = None
            sess.source = build_source(source_name, speech_path)
    sess.downlink_task = asyncio.create_task(sess.downlink(writer))

    # ------------------------------------------------------------------
    # --wakeword-wake: run the OK Hiyo detector *in parallel* with the
    # oneshot/live pipeline. Constructed only after T_HELLO has been sent and
    # the downlink started, so model loading never delays the handshake. Unlike
    # Phase-5A --wakeword it does NOT take over the downlink source; it only
    # feeds on the 16 kHz uplink and, on a trigger, performs the normal wake
    # (IDLE -> LISTEN_ONE_SHOT). Live Mode is entered only on an explicit
    # "live mode" phrase via the router.
    # ------------------------------------------------------------------
    if wakeword_wake:
        if controller is None:
            print("[wakeword-wake] requires --control; detector disabled", flush=True)
        else:
            import wakeword as _wwmod
            _loop = asyncio.get_running_loop()
            ww_log = os.path.join(outdir, f"wakeword_{ts}.csv")

            def _ww_fire(score, _ctl=controller):
                # runs on the event loop thread
                try:
                    if _ctl.is_mic_muted():
                        print(f"[wakeword-wake] TRIGGER score={score:.2f} ignored (mic muted)", flush=True)
                        return
                except Exception:  # noqa: BLE001
                    pass
                try:
                    woke = _ctl.wake("wakeword")     # IDLE -> LISTEN_ONE_SHOT (normal mode)
                except Exception:  # noqa: BLE001
                    woke = False
                if woke:
                    print(f"[wakeword-wake] TRIGGER score={score:.2f} -> LISTEN_ONE_SHOT", flush=True)
                else:
                    print(f"[wakeword-wake] TRIGGER score={score:.2f} ignored state={_ctl.get_state()}", flush=True)

            def _ww_on_trigger(score, _loop=_loop, _fire=_ww_fire):
                _loop.call_soon_threadsafe(_fire, score)

            def _build_ww():
                return _wwmod.WakeWordDetector(
                    model_paths=[os.path.join(WAKEWORD_DIR, ww_model)],
                    melspec_path=os.path.join(WAKEWORD_DIR, "melspectrogram.onnx"),
                    embedding_path=os.path.join(WAKEWORD_DIR, "embedding_model.onnx"),
                    threshold=ww_threshold,
                    cooldown=ww_cooldown,
                    confirm_frames=ww_confirm_frames,
                    confirm_avg=ww_confirm_avg,
                    on_trigger=_ww_on_trigger,
                    log_csv=ww_log,
                )

            try:
                # off the event loop: model loading must not stall the downlink
                ww_detector = await _loop.run_in_executor(None, _build_ww)
                print(
                    f"[wakeword-wake] detector ready label={ww_detector.label} "
                    f"threshold={ww_threshold:.2f} cooldown={ww_cooldown:.2f}s "
                    f"confirm={ww_confirm_frames}x frames avg>={ww_confirm_avg:.2f} log={ww_log}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - never drop the audio client
                ww_detector = None
                print(f"[wakeword-wake] detector unavailable: {type(exc).__name__}: {exc}", flush=True)

    last_report = time.time()
    try:
        while True:
            header = await asyncio.wait_for(reader.readexactly(HDR.size),
                                            timeout=UPLINK_IDLE_TIMEOUT)
            magic, msg_type, flags, seq, length = HDR.unpack(header)
            if magic != MAGIC:
                sess.stats.parse_errors += 1
                print(f"[warn] bad magic {magic:#06x}; dropping connection", flush=True)
                break
            payload = (await asyncio.wait_for(reader.readexactly(length),
                                              timeout=UPLINK_IDLE_TIMEOUT)) if length else b""
            sess.handle(msg_type, seq, payload)
            if gpt is not None and msg_type == 0x01 and len(payload) > 8:
                gpt.push_uplink(payload[8:])
            if ww_detector is not None and msg_type == T_UPLINK and len(payload) > 8:
                # physical mute has top priority: never forward mic PCM while muted
                if controller is None or not controller.is_mic_muted():
                    ww_detector.feed(payload[8:])
            if orch is not None and msg_type == T_UPLINK and len(payload) > 8:
                orch.feed(payload[8:])
            if live_sup is not None and msg_type == T_UPLINK and len(payload) > 8:
                live_sup.feed(payload[8:])
            if time.time() - last_report >= 5.0:
                last_report = time.time()
                print("[5s] " + sess.stats.summary(), flush=True)
    except asyncio.TimeoutError:
        print("[audio] uplink idle timeout; closing stale connection", flush=True)
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}", flush=True)
    finally:
        if ww_detector is not None:
            ww_detector.stop()
            print(f"[wakeword] {ww_detector.stats()}", flush=True)
        if controller is not None and ww_source is not None:
            if getattr(controller, "_sink", None) is ww_source:
                controller.set_sink(None)
        if orch is not None:
            if getattr(controller, "_sink", None) is orch.source:
                controller.set_sink(None)
            orch.close()
        if live_sup is not None:
            live_sup.detach()
            try:
                await live_sup.stop("client_disconnect")
            except Exception:  # noqa: BLE001
                pass
        if gpt is not None:
            await gpt.close()
        if sess.downlink_task:
            sess.downlink_task.cancel()
            try:
                await sess.downlink_task
            except asyncio.CancelledError:
                pass
        sess.flush()
        print("=" * 110, flush=True)
        print(f"FINAL {sess.wav_path}", flush=True)
        print(sess.stats.summary(), flush=True)
        print("=" * 110, flush=True)
        writer.close()
        if _ACTIVE_AUDIO.get("gen") == _audio_gen:
            _ACTIVE_AUDIO["writer"] = None
            _ACTIVE_AUDIO["task"] = None
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.0.212")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--outdir", default=os.path.expanduser("~/voice/echo-bridge/captures"))
    ap.add_argument("--source", default="sequence", choices=["tone", "pink", "speech", "alt", "sequence"])
    ap.add_argument("--speech", default=os.path.join(SRC_FILES, "speech48.wav"))
    ap.add_argument("--sendlog", default="")
    ap.add_argument("--gptlive", action="store_true")
    ap.add_argument("--vc", action="store_true",
                    help="Enable Seed-VC for both one-shot TTS and Live unless overridden below")
    ap.add_argument("--oneshot-vc", dest="oneshot_vc", action="store_true", default=None,
                    help="Force Seed-VC on for normal one-shot TTS")
    ap.add_argument("--no-oneshot-vc", dest="oneshot_vc", action="store_false",
                    help="Play the normal TTS voice directly, without Seed-VC")
    ap.add_argument("--live-vc", dest="live_vc", action="store_true", default=None,
                    help="Force Seed-VC on for GPT Live output")
    ap.add_argument("--no-live-vc", dest="live_vc", action="store_false",
                    help="Play the GPT Live model voice directly, without Seed-VC")
    ap.add_argument("--oneshot-tts", choices=["direct", "hermes"], default=os.getenv("ECHO_ONESHOT_TTS", "direct"),
                    help="Normal Question Mode TTS: project direct endpoint or Hermes configured tts.provider")
    ap.add_argument("--wakeword", action="store_true",
                    help="Phase 5A: run the OK Hiyo detector on the 16 kHz uplink and play an ack chime on trigger")
    ap.add_argument("--wakeword-wake", action="store_true",
                    help="Run the OK Hiyo detector in parallel with --oneshot/--live; a trigger "
                         "performs the normal wake (LISTEN_ONE_SHOT). Live Mode is only entered "
                         "on an explicit 'live mode' phrase via the router")
    ap.add_argument("--wakeword-threshold", type=float, default=0.50)
    ap.add_argument("--wakeword-cooldown", type=float, default=2.0)
    ap.add_argument("--wakeword-confirm-frames", type=int, default=4,
                    help="consecutive frames >= confirm threshold required to fire")
    ap.add_argument("--wakeword-confirm-avg", type=float, default=0.70,
                    help="minimum mean score over the confirmation window")
    ap.add_argument("--wakeword-model", default="ok_hiyo_v1_selfcontained.onnx")
    ap.add_argument("--control", action="store_true",
                    help="Phase 5B: enable the mode controller + Echo control channel")
    ap.add_argument("--control-port", type=int, default=8793, help="Echo control daemon port")
    ap.add_argument("--admin-port", type=int, default=8794, help="local text admin console (127.0.0.1)")
    ap.add_argument("--oneshot", action="store_true",
                    help="Phase 5D: enable one-shot question mode (VAD+STT+Hermes+TTS+Seed-VC)")
    ap.add_argument("--live", action="store_true",
                    help="Phase 5E: enable persistent GPT Live Mode via the deterministic router")
    ap.add_argument("--live-idle-timeout", type=float, default=120.0,
                    help="Phase 5E: seconds of no user speech before Live Mode auto-exits")
    ap.add_argument("--barge", dest="barge", action="store_true", default=True,
                    help="Phase 5F: Echo-side barge-in detection (AEC-clean uplink)")
    ap.add_argument("--no-barge", dest="barge", action="store_false",
                    help="Phase 5F: disable Echo-side barge-in (native vendor interruption only)")
    ap.add_argument("--no-led", action="store_true",
                    help="Phase 5D.5: disable the semantic LED projector")
    ap.add_argument("--led-mode", default="cooperative", choices=["cooperative", "exclusive"],
                    help="Phase 5D.5B: LED ownership policy")
    ap.add_argument("--rig", default="",
                    help="Phase 5F.1: no-billing detector rig (48 kHz mono Hiyori WAV); "
                         "replaces the GPT Live session with a local playback source")
    ap.add_argument("--spotify", action="store_true",
                    help="Phase 5G: mix a Spotify Connect source (spotifyd pipe -> ffmpeg -> "
                         "48k mono) into the downlink; music ducks while Hiyo is interacting. "
                         "Requires --oneshot")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    controller = None
    led = None
    extra_servers = []
    if args.control:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import echo_controller
        controller = echo_controller.EchoController()
        if not args.no_led:
            try:
                import echo_led
                led = echo_led.LedProjector(controller, mode=args.led_mode)
                controller.attach_led(led)
                led.start()
            except Exception as exc:  # noqa: BLE001
                led = None
                print(f"[led] projector disabled: {type(exc).__name__}: {exc}", flush=True)
        ctl_server = await asyncio.start_server(
            lambda r, w: echo_controller.handle_control(r, w, controller),
            args.host, args.control_port,
        )
        adm_server = await asyncio.start_server(
            lambda r, w: echo_controller.handle_admin(r, w, controller),
            "127.0.0.1", args.admin_port,
        )
        extra_servers = [ctl_server, adm_server]
        if led is not None:
            print("[led] projector attached (semantic states -> device owner)", flush=True)
        print(
            f"[control] controller on {args.control_port} (Echo daemon), "
            f"admin console 127.0.0.1:{args.admin_port}",
            flush=True,
        )

    server = await asyncio.start_server(
        lambda r, w: handle_client(
            r, w, args.outdir, args.source, args.speech, args.sendlog,
            args.gptlive, args.vc, args.oneshot_vc, args.live_vc, args.oneshot_tts,
            args.wakeword, args.wakeword_threshold,
            args.wakeword_cooldown, args.wakeword_model,
            args.wakeword_confirm_frames, args.wakeword_confirm_avg,
            args.oneshot,
            args.live, args.live_idle_timeout, args.barge, controller,
            rig_wav=args.rig,
            wakeword_wake=args.wakeword_wake,
            spotify=args.spotify,
        ),
        args.host, args.port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    if args.gptlive:
        mode = "gptlive" + ("+vc" if args.vc else "")
    elif args.wakeword:
        mode = f"wakeword(thr={args.wakeword_threshold:.2f},cd={args.wakeword_cooldown:.1f}s)"
    else:
        mode = f"source={args.source}"
    if controller is not None:
        mode += "+control"
    if args.live:
        mode += "+live"
    if args.wakeword_wake:
        mode += "+wakeword-wake"
    if args.spotify:
        mode += "+spotify"
    print(f"echo-bridge listening on {addrs} (outdir={args.outdir} mode={mode})", flush=True)
    servers = [server] + extra_servers
    await asyncio.gather(*[s.serve_forever() for s in servers])


if __name__ == "__main__":
    asyncio.run(main())

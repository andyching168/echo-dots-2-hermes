#!/usr/bin/env python3
"""Echo wake-word detector (Phase 5A) — low-coupling helper for echo_server.py.

Feed it the AEC-clean PCM16 16 kHz mono uplink that echo_server.py already
receives from the Echo. It runs the *standard* openWakeWord streaming pipeline
(melspectrogram.onnx -> embedding_model.onnx -> custom classifier ONNX) in a
background thread so the asyncio transport is never blocked.

Exports:
    WakeWordDetector  feed(pcm16_bytes); background worker; on_trigger(score)
    WakeWordSource    downlink Source that emits silence + one-shot ack chimes
    make_ack_chime()  880 Hz/100 ms + 1320 Hz/120 ms, PCM16 48 kHz mono

Design notes:
  * threshold is a fixed constant (default 0.50); no adaptive threshold.
  * a fixed cooldown (default 2.0 s) suppresses repeat triggers from the same
    utterance and from the acknowledgement chime's own residual.
  * all classifier scores are streamed to a CSV detection log; stdout prints
    the requested `[wakeword] ok_hiyo score=... threshold=... TRIGGER` lines.
"""

from __future__ import annotations

import array
import math
import os
import queue
import threading
import time
from typing import Callable, List, Optional, Sequence

import numpy as np

try:  # keep import failure local to the wakeword runtime
    from openwakeword.model import Model as _OwwModel
except Exception as _exc:  # pragma: no cover - surfaced at construction
    _OwwModel = None
    _OWW_IMPORT_ERROR = _exc


def make_ack_chime(sr: int = 48000, amp: float = 0.22, fade_ms: float = 6.0) -> array.array:
    """Short two-note acknowledgement chime, PCM16 mono at `sr` (default 48 kHz).

    880 Hz ~100 ms, ~35 ms silence, 1320 Hz ~120 ms, ~10 ms tail. A short
    raised-cosine-ish fade on every segment avoids click transients. Amplitude
    is deliberately conservative (~0.22 FS, ~ -13 dBFS) to avoid clipping.
    """

    def seg(freq: float, ms: float) -> array.array:
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

    out = seg(880.0, 100.0)
    out.extend(array.array("h", bytes(int(sr * 0.035) * 2)))
    out.extend(seg(1320.0, 120.0))
    out.extend(array.array("h", bytes(int(sr * 0.010) * 2)))
    return out


class WakeWordSource:
    """Downlink Source: emits silence, then any queued acknowledgement chimes.

    The echo_server downlink loop calls next_frame() every 10 ms (480 samples
    @ 48 kHz). This keeps the verified downlink cadence untouched; chimes are
    just interleaved with silence.
    """

    def __init__(self, sr: int = 48000, frame: int = 480) -> None:
        self.sr = sr
        self.frame = frame
        self.label = "idle"
        self.chimes_enqueued = 0
        self._pending = array.array("h")
        self._silence = array.array("h", bytes(frame * 2))

    def enqueue(self, pcm: array.array) -> None:
        self._pending.extend(pcm)
        self.chimes_enqueued += 1

    def pending_samples(self) -> int:
        return len(self._pending)

    def next_frame(self) -> array.array:
        if len(self._pending) >= self.frame:
            frame = self._pending[: self.frame]
            del self._pending[: self.frame]
            self.label = "ack"
            return frame
        if len(self._pending) > 0:
            frame = array.array("h", self._pending)
            del self._pending[:]
            frame.extend(array.array("h", bytes((self.frame - len(frame)) * 2)))
            self.label = "ack"
            return frame
        self.label = "idle"
        return self._silence

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()


class WakeWordDetector:
    """Streaming "OK Hiyo" detector over openWakeWord 0.4.x.

    feed() accepts arbitrary-length PCM16 16 kHz mono bytes (we use the Echo's
    160-sample / 10 ms uplink frames). Samples are re-chunked into openWakeWord's
    native 1280-sample (80 ms) cadence. Keeping a rolling context and updating
    at the normal streaming cadence is required for the trained score to be
    meaningful (a single centred fixed window is misleading and is avoided).
    """

    def __init__(
        self,
        model_paths: Sequence[str],
        threshold: float = 0.50,
        cooldown: float = 2.0,
        on_trigger: Optional[Callable[[float], None]] = None,
        on_score: Optional[Callable[[float, float, str], None]] = None,
        log_csv: Optional[str] = None,
        confirm_frames: int = 4,
        confirm_threshold: Optional[float] = None,
        confirm_avg: float = 0.70,
        melspec_path: Optional[str] = None,
        embedding_path: Optional[str] = None,
        chunk_samples: int = 1280,
        ncpu: int = 1,
        verbose: bool = True,
        async_processing: bool = True,
    ) -> None:
        if _OwwModel is None:  # pragma: no cover
            raise RuntimeError(f"openwakeword runtime unavailable: {_OWW_IMPORT_ERROR}")
        model_list = list(model_paths)
        # openWakeWord changed its Model() signature between 0.4.x and 0.6.x:
        #   0.4.x: Model(wakeword_model_paths=[...], melspec_onnx_model_path=..., embedding_onnx_model_path=...)
        #   0.6.x: Model(wakeword_models=[...], inference_framework="onnx",
        #               melspec_model_path=..., embedding_model_path=...)
        # Try the newer form first, then fall back to the legacy form.
        try:
            new_kwargs = {"ncpu": ncpu, "inference_framework": "onnx"}
            if melspec_path:
                new_kwargs["melspec_model_path"] = melspec_path
            if embedding_path:
                new_kwargs["embedding_model_path"] = embedding_path
            self._model = _OwwModel(wakeword_models=model_list, **new_kwargs)
        except TypeError:
            legacy_kwargs = {"ncpu": ncpu}
            if melspec_path:
                legacy_kwargs["melspec_onnx_model_path"] = melspec_path
            if embedding_path:
                legacy_kwargs["embedding_onnx_model_path"] = embedding_path
            self._model = _OwwModel(wakeword_model_paths=model_list, **legacy_kwargs)
        self._ncpu = ncpu
        self._mf = melspec_path
        self._ef = embedding_path
        self._label = list(self._model.models.keys())[0]

        self.threshold = float(threshold)
        self.cooldown = float(cooldown)
        # --- confirmation gate (anti-false-trigger) -----------------------
        # A genuine "OK Hiyo" produces a sustained plateau of high scores
        # across several consecutive 80 ms frames. Household noise / TV
        # speech / music instead produces an isolated single-frame spike that
        # immediately collapses. Requiring confirm_frames consecutive frames
        # at or above confirm_threshold (and a mean above confirm_avg)
        # rejects those spikes while adding only ~(N-1)*80 ms to a real hit.
        # Tuned against 11 recorded events (2026-09-20): 4 real / 6 false,
        self.confirm_frames = max(1, int(confirm_frames))
        self.confirm_threshold = (
            float(confirm_threshold) if confirm_threshold is not None
            else 0.30
        )
        self.confirm_avg = float(confirm_avg)
        self._consec = 0
        self._window = []
        self.confirm_rejects = 0
        self.chunk_samples = int(chunk_samples)
        self.verbose = bool(verbose)
        self.async_processing = bool(async_processing)

        self._on_trigger = on_trigger
        self._on_score = on_score

        self.total_samples = 0
        self.score_count = 0
        self.trigger_count = 0
        self.cooldown_skips = 0
        self.last_score = 0.0
        self.max_score = 0.0
        self._last_trigger_s = -1e9

        self._buf = bytearray()
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._log = None
        if log_csv:
            os.makedirs(os.path.dirname(os.path.abspath(log_csv)), exist_ok=True)
            self._log = open(log_csv, "w", buffering=1)
            self._log.write("wall_iso,mono_s,score,threshold,state\n")

        if self.async_processing:
            self._thread = threading.Thread(target=self._run, name="wakeword", daemon=True)
            self._thread.start()

    @property
    def label(self) -> str:
        return self._label

    def feed(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        step = self.chunk_samples * 2
        if not self.async_processing:
            self._buf.extend(pcm16)
            while len(self._buf) >= step:
                chunk = bytes(self._buf[:step])
                del self._buf[:step]
                self._process_chunk(chunk)
            return
        self._buf.extend(pcm16)
        while len(self._buf) >= step:
            chunk = bytes(self._buf[:step])
            del self._buf[:step]
            try:
                self._q.put_nowait(chunk)
            except queue.Full:
                # never block the transport; drop the oldest queued chunk
                try:
                    self._q.get_nowait()
                    self._q.put_nowait(chunk)
                except queue.Empty:
                    pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._process_chunk(chunk)

    def _process_chunk(self, chunk: bytes) -> None:
        x = np.frombuffer(chunk, dtype=np.int16)
        try:
            pred = self._model.predict(x)
            score = float(pred[self._label])
        except Exception as exc:  # noqa: BLE001
            if self.verbose:
                print(f"[wakeword] predict error: {type(exc).__name__}: {exc}", flush=True)
            return

        self.total_samples += len(x)
        self.score_count += 1
        self.last_score = score
        if score > self.max_score:
            self.max_score = score

        mono_s = self.total_samples / 16000.0
        now = time.time()
        # --- confirmation gate (anti-false-trigger) -----------------------
        # A genuine "OK Hiyo" yields a sustained plateau of high scores over
        # consecutive 80 ms frames. TV / news / music / other speech yields
        # an isolated single-frame spike that collapses immediately. Only a
        # confirmed plateau may fire.
        if score >= self.confirm_threshold:
            self._consec += 1
            self._window.append(score)
            if len(self._window) > self.confirm_frames:
                self._window.pop(0)
        else:
            self._consec = 0
            self._window = []

        enough = self._consec >= self.confirm_frames
        if enough:
            enough = (sum(self._window) / float(len(self._window))) >= self.confirm_avg

        if not enough:
            state = ""
            if score >= self.threshold:
                self.confirm_rejects += 1
                state = "reject"
        elif (mono_s - self._last_trigger_s) >= self.cooldown:
            self._last_trigger_s = mono_s
            self.trigger_count += 1
            state = "TRIGGER"
            self._consec = 0
            self._window = []
        else:
            state = "cooldown"
            self.cooldown_skips += 1
            self._consec = 0
            self._window = []

        self._record(mono_s, score, state, now)
        if self._on_score is not None:
            self._on_score(mono_s, score, state)
        if state == "TRIGGER" and self._on_trigger is not None:
            self._on_trigger(score)

    def _record(self, mono_s: float, score: float, state: str, now: float) -> None:
        if self._log is not None:
            ms = int((now - int(now)) * 1000)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{ms:03d}"
            self._log.write(f"{stamp},{mono_s:.3f},{score:.4f},{self.threshold:.2f},{state}\n")
        if self.verbose and (state in ("TRIGGER", "cooldown", "reject") or score >= self.threshold * 0.5):
            tag = state if state else "score"
            print(
                f"[wakeword] ok_hiyo score={score:.2f} threshold={self.threshold:.2f} {tag}",
                flush=True,
            )

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the async worker has drained the queued chunks."""
        if not self.async_processing:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                time.sleep(0.05)
                if self._q.empty():
                    return True
            time.sleep(0.02)
        return False

    def stats(self) -> str:
        return (
            f"scores={self.score_count} triggers={self.trigger_count} "
            f"cooldown_skips={self.cooldown_skips} confirm_rejects={self.confirm_rejects} "
            f"last={self.last_score:.3f} "
            f"max={self.max_score:.3f} threshold={self.threshold:.2f} cooldown={self.cooldown:.2f}s"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            except Exception:
                pass

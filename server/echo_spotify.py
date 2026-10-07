#!/usr/bin/env python3
"""Phase 5G — Spotify Connect source for the Echo downlink.

Spotify Connect is provided by `librespot` (pipe backend: raw 44.1 kHz stereo
s16 on stdout), piped through `ffmpeg` to the Echo downlink format (48 kHz
mono s16).  A reader thread buffers the PCM; `MixSource` sums it with the Hiyo
voice source and ducks (silences) it while Hiyo is interacting, so the answer
is never masked by music.

Nothing here touches the device: the audio rides the existing `echo_probe`
downlink (48 kHz mono, 10 ms frames), which remains the sole owner of D23.

Env:
    ECHO_LIBRESPOT    path to the librespot binary (default ~/.cargo/bin/librespot)
    ECHO_SPOTIFY_NAME device name shown in Spotify (default "Echo Dot (Hiyo)")
    ECHO_SPOTIFY_CMD  override the whole producer with one command that emits
                      48 kHz mono s16 on stdout (used for tests)
    ECHO_SPOTIFY_PAUSE "1" to also Pause/Play the stream via MPRIS on interaction
"""

from __future__ import annotations

import array
import os
import shlex
import subprocess
import threading
import time
from typing import Callable, Optional

SR48 = 48000
FRAME = 480                      # 10 ms
BYTES_PER_FRAME = FRAME * 2
BUFFER_MAX = SR48 * 2 * 3        # 3 s of mono s16 bytes

_FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
           "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1"]


def _default_librespot_cmd() -> list:
    lib = os.path.expanduser(os.getenv("ECHO_LIBRESPOT", "~/.cargo/bin/librespot"))
    name = os.getenv("ECHO_SPOTIFY_NAME", "Echo Dot (Hiyo)")
    cmd = [lib, "--name", name, "--device-type", "speaker",
           "-b", "320", "-f", "S16", "--backend", "pipe",
           "--cache", os.path.expanduser("~/.cache/librespot"), "--disable-audio-cache"]
    # librespot's embedded mDNS otherwise advertises the docker bridge IP
    # (e.g. 172.17.0.1), which phones cannot reach.  Pin it to the LAN address.
    iface = os.getenv("ECHO_SPOTIFY_IFACE", "192.168.0.212")
    if iface:
        cmd += ["--zeroconf-interface", iface]
    return cmd


class SpotifySource:
    """Background spotifyd -> ffmpeg -> 48 kHz mono PCM buffer."""

    label = "spotify"

    def __init__(self, cmd: str = "", log: Optional[Callable[[str], None]] = None,
                 buffer_max: int = BUFFER_MAX) -> None:
        self._log = log or (lambda m: print(m, flush=True))
        self._cmd = cmd or os.getenv("ECHO_SPOTIFY_CMD", "")
        self._max = buffer_max
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._thread: Optional[threading.Thread] = None
        self.started_at = 0.0
        self.underruns = 0

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="spotify-src", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill(self._proc)
        if self._thread is not None:
            self._thread.join(timeout=3)

    @staticmethod
    def _kill(proc) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass

    def _spawn(self):
        if self._cmd:
            self._log(f"[spotify] source cmd: {self._cmd}")
            return subprocess.Popen(shlex.split(self._cmd), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        spot = subprocess.Popen(_default_librespot_cmd(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        # Drain librespot's log so the pipe never blocks; surface important lines.
        try:
            t = threading.Thread(target=self._drain_log, args=(spot,), daemon=True)
            t.start()
        except Exception:  # noqa: BLE001
            pass
        try:
            ff = subprocess.Popen(_FFMPEG, stdin=spot.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, bufsize=0)
        except Exception:
            self._kill(spot)
            raise
        try:
            spot.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        return ff

    def _drain_log(self, proc) -> None:
        """Forward notable librespot log lines (keeps stderr from blocking)."""
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if any(k in line for k in ("ERROR", "WARN", "premium", "audio key",
                                           "Authenticated", "Playing", "playing",
                                           "login", "credential")):
                    self._log(f"[librespot] {line}")
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            proc = None
            try:
                proc = self._spawn()
                self._proc = proc
                self._log("[spotify] pipeline up (spotifyd -> ffmpeg 48k mono)")
                while not self._stop.is_set():
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    with self._lock:
                        self._buf.extend(chunk)
                        if len(self._buf) > self._max:
                            del self._buf[: len(self._buf) - self._max]
            except Exception as exc:  # noqa: BLE001
                self._log(f"[spotify] pipeline error: {type(exc).__name__}: {exc}")
            finally:
                self._kill(proc)
                self._proc = None
            if not self._stop.is_set():
                self._log("[spotify] pipeline exited; restarting in 2 s")
                time.sleep(2)

    # ---- source API ---------------------------------------------------------
    def playing(self) -> bool:
        with self._lock:
            return len(self._buf) >= BYTES_PER_FRAME

    def next_frame(self) -> array.array:
        with self._lock:
            if len(self._buf) >= BYTES_PER_FRAME:
                raw = bytes(self._buf[:BYTES_PER_FRAME])
                del self._buf[:BYTES_PER_FRAME]
            else:
                raw = b"\x00" * BYTES_PER_FRAME
                if self._buf:
                    self.underruns += 1
                    self._buf.clear()
        a = array.array("h")
        a.frombytes(raw)
        return a

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()


class MixSource:
    """Downlink source = Hiyo voice + Spotify music.

    `duck()` returns True while Hiyo is interacting; then only the Hiyo side is
    emitted (music is silenced, not stopped, so it simply continues when done).
    """

    def __init__(self, hiyo, spotify: SpotifySource, duck: Callable[[], bool],
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.hiyo = hiyo
        self.spotify = spotify
        self._duck = duck
        self._log = log or (lambda m: print(m, flush=True))
        self.label = getattr(hiyo, "label", "hiyo")

    def next_frame(self) -> array.array:
        h = self.hiyo.next_frame()
        if self.spotify is None:
            self.label = getattr(self.hiyo, "label", "hiyo")
            return h
        sp_playing = self.spotify.playing()
        if not sp_playing or self._duck():
            self.label = getattr(self.hiyo, "label", "hiyo")
            return h
        s = self.spotify.next_frame()
        out = array.array("h", bytes(FRAME * 2))
        for i in range(FRAME):
            v = h[i] + s[i]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            out[i] = v
        self.label = "spotify"
        return out

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()

    # ---- injection passthrough (HA `announce` / volume tone) ---------------
    def enqueue(self, pcm) -> None:
        """Forward injected 48 kHz mono PCM to the underlying Hiyo source."""
        if hasattr(self.hiyo, "enqueue"):
            self.hiyo.enqueue(pcm)
        elif hasattr(self.hiyo, "push_48k"):
            self.hiyo.push_48k(pcm.tobytes() if hasattr(pcm, "tobytes") else pcm)

    def push_48k(self, pcm: bytes) -> None:
        if hasattr(self.hiyo, "push_48k"):
            self.hiyo.push_48k(pcm)
        elif hasattr(self.hiyo, "enqueue"):
            a = array.array("h")
            a.frombytes(pcm)
            self.hiyo.enqueue(a)


class LiveStreamSource:
    """Continuous internet-radio / live stream -> 48 kHz mono, into the downlink.

    Unlike `SpotifySource` (also ffmpeg), this is **started on demand** for a URL
    and stops on request.  It fills the same bounded buffer and is drained by the
    downlink mixer, so it behaves like any other source.  Used by the HA
    `play_media` action for radio streams (`audio/aac`, `audio/mpeg` live URLs).
    """

    label = "radio"

    def __init__(self, url: str, log: Optional[Callable[[str], None]] = None,
                 buffer_max: int = BUFFER_MAX) -> None:
        self.url = url
        self._log = log or (lambda m: print(m, flush=True))
        self._max = buffer_max
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._thread = None
        self.underruns = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="radio-src", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._proc is not None:
                self._proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-reconnect", "1", "-reconnect_streamed", "1",
               "-reconnect_delay_max", "5", "-i", self.url,
               "-f", "s16le", "-ar", "48000", "-ac", "1", "pipe:1"]
        self._log(f"[radio] ffmpeg -> {self.url[:70]}")
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, bufsize=0)
            while not self._stop.is_set():
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    break
                with self._lock:
                    self._buf.extend(chunk)
                    if len(self._buf) > self._max:
                        del self._buf[: len(self._buf) - self._max]
        except Exception as exc:  # noqa: BLE001
            self._log(f"[radio] stream error: {type(exc).__name__}: {exc}")
        finally:
            try:
                if self._proc is not None:
                    self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._log("[radio] stream stopped")

    def alive(self) -> bool:
        return self._thread is not None and not self._stop.is_set()

    def playing(self) -> bool:
        with self._lock:
            return len(self._buf) >= BYTES_PER_FRAME

    def next_frame(self) -> array.array:
        with self._lock:
            if len(self._buf) >= BYTES_PER_FRAME:
                raw = bytes(self._buf[:BYTES_PER_FRAME])
                del self._buf[:BYTES_PER_FRAME]
            else:
                raw = b"\x00" * BYTES_PER_FRAME
                if self._buf:
                    self.underruns += 1
                    self._buf.clear()
        a = array.array("h")
        a.frombytes(raw)
        return a

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()


class RadioMixSource:
    """Downlink source = Hiyo voice + a live radio stream (sum, radio ducked)."""

    def __init__(self, hiyo, radio: LiveStreamSource, duck: Callable[[], bool],
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.hiyo = hiyo
        self.radio = radio
        self._duck = duck
        self._log = log or (lambda m: print(m, flush=True))
        self.label = getattr(hiyo, "label", "hiyo")

    def next_frame(self) -> array.array:
        h = self.hiyo.next_frame()
        if self.radio is None or not self.radio.playing() or self._duck():
            self.label = getattr(self.hiyo, "label", "hiyo")
            return h
        s = self.radio.next_frame()
        out = array.array("h", bytes(FRAME * 2))
        for i in range(FRAME):
            v = h[i] + s[i]
            out[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
        self.label = "radio"
        return out

    def skip(self, n: int) -> None:
        for _ in range(n):
            self.next_frame()

    def enqueue(self, pcm) -> None:
        if hasattr(self.hiyo, "enqueue"):
            self.hiyo.enqueue(pcm)
        elif hasattr(self.hiyo, "push_48k"):
            self.hiyo.push_48k(pcm.tobytes() if hasattr(pcm, "tobytes") else pcm)

    def push_48k(self, pcm: bytes) -> None:
        if hasattr(self.hiyo, "push_48k"):
            self.hiyo.push_48k(pcm)
        elif hasattr(self.hiyo, "enqueue"):
            a = array.array("h")
            a.frombytes(pcm)
            self.hiyo.enqueue(a)

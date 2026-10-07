#!/usr/bin/env python3
"""Small localhost bridge to Hermes' configured TTS provider.

Runs inside the Hermes Agent Python environment so it can call
``tools.tts_tool.text_to_speech_tool`` directly.  The Echo bridge itself stays
in its own venv and talks to this service over localhost HTTP.

POST /synthesize {"text": "..."} -> mono PCM16 WAV @ 24 kHz
GET  /health                         -> JSON status

The selected provider, voice, credentials and custom command/plugin backend all
come from Hermes' normal ``tts:`` configuration.  This service does not choose
or override a provider.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.getenv("ECHO_HERMES_TTS_HOST", "127.0.0.1")
PORT = int(os.getenv("ECHO_HERMES_TTS_PORT", "8795"))
RATE = int(os.getenv("ECHO_HERMES_TTS_RATE", "24000"))
MAX_TEXT = int(os.getenv("ECHO_HERMES_TTS_MAX_TEXT", "20000"))
TIMEOUT = float(os.getenv("ECHO_HERMES_TTS_FFMPEG_TIMEOUT", "120"))


def _tts_tool(text: str) -> list[Path]:
    from tools.tts_tool import text_to_speech_tool

    raw = text_to_speech_tool(text=text)
    result = json.loads(raw)
    if not result.get("success", False):
        raise RuntimeError(result.get("error") or raw[:500])
    paths = result.get("file_paths") or [result.get("file_path")]
    out = [Path(str(p)).expanduser() for p in paths if p]
    if not out or any(not p.exists() or p.stat().st_size == 0 for p in out):
        raise RuntimeError("Hermes TTS returned no usable audio file")
    return out


def _decode_pcm(path: Path) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(RATE), "-ac", "1", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg decode failed rc={proc.returncode}: {detail}")
    return proc.stdout


def synthesize(text: str) -> bytes:
    paths = _tts_tool(text)
    pcm = b"".join(_decode_pcm(path) for path in paths)
    if not pcm:
        raise RuntimeError("Hermes TTS decoded to empty PCM")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    # Hermes' audio cache owns the provider artifacts.  Do not unlink them here:
    # normal Hermes cache cleanup/lifecycle rules should remain authoritative.
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "EchoHermesTTS/1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[hermes-tts] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            from tools.tts_tool import _load_tts_config, _get_provider
            cfg = _load_tts_config()
            self._json(200, {"ok": True, "provider": _get_provider(cfg), "rate": RATE})
        except Exception as exc:
            self._json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/synthesize":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
            if n <= 0 or n > 256 * 1024:
                raise ValueError("invalid Content-Length")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("text is empty")
            if len(text) > MAX_TEXT:
                raise ValueError(f"text exceeds {MAX_TEXT} characters")
            wav = synthesize(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("X-Hermes-TTS", "1")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[hermes-tts] listening http://{HOST}:{PORT} provider=Hermes-configured rate={RATE}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

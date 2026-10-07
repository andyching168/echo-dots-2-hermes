#!/usr/bin/env python3
"""Echo one-shot STT server backed by Google Cloud Speech-to-Text.

Keeps the existing local HTTP contract used by echo_oneshot.py:

    POST /transcribe  {"wav_base64": "<16k mono PCM16 WAV bytes b64>",
                       "language": "zh"|"en"|null}
        -> {"ok": true, "text": "...", "language": "zh-TW",
            "language_probability": 0.98, "latency_ms": 812}

    GET /health -> {"ok": true, "provider": "google-cloud-speech", ...}

Authentication uses Google Application Default Credentials (ADC).
The upstream Echo pipeline already performs local VAD, so only completed
utterances are sent to Google Cloud.
"""

from __future__ import annotations

import base64
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from google.cloud import speech

DEFAULT_LANG = os.getenv("ECHO_STT_LANG", "zh-TW") or "zh-TW"
MODEL = os.getenv("ECHO_STT_GCP_MODEL", "").strip()
ALT_LANGS = [
    x.strip()
    for x in os.getenv("ECHO_STT_ALT_LANGS", "").split(",")
    if x.strip()
]

_client = None


def _language_code(value: str | None) -> str:
    if not value:
        return DEFAULT_LANG
    value = value.strip()
    aliases = {
        "zh": "zh-TW",
        "zh-tw": "zh-TW",
        "zh_tw": "zh-TW",
        "en": "en-US",
        "en-us": "en-US",
        "en_us": "en-US",
    }
    return aliases.get(value.lower(), value)


def _load():
    global _client
    if _client is None:
        _client = speech.SpeechClient()
        print(
            f"[stt] provider=google-cloud-speech model={MODEL} "
            f"default_lang={DEFAULT_LANG} alt_langs={ALT_LANGS}",
            flush=True,
        )
    return _client


def transcribe(wav_bytes: bytes, language=None) -> dict:
    t0 = time.time()
    client = _load()
    lang = _language_code(language)

    alternatives = [x for x in ALT_LANGS if x != lang]
    config_kwargs = dict(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=lang,
        alternative_language_codes=alternatives,
        enable_automatic_punctuation=True,
    )
    if MODEL:
        config_kwargs["model"] = MODEL
    config = speech.RecognitionConfig(**config_kwargs)
    audio = speech.RecognitionAudio(content=wav_bytes)
    response = client.recognize(config=config, audio=audio)

    texts = []
    confidence = 0.0
    detected_language = lang
    for result in response.results:
        if not result.alternatives:
            continue
        alt = result.alternatives[0]
        texts.append(alt.transcript)
        confidence = max(confidence, float(getattr(alt, "confidence", 0.0) or 0.0))
        result_lang = getattr(result, "language_code", None)
        if result_lang:
            detected_language = result_lang

    return {
        "ok": True,
        "text": " ".join(x.strip() for x in texts if x.strip()).strip(),
        "language": detected_language,
        # Preserve the old response contract. This is transcript confidence,
        # used only for diagnostics by the current caller.
        "language_probability": round(confidence, 3),
        "latency_ms": int((time.time() - t0) * 1000),
        "provider": "google-cloud-speech",
    }


class _H(BaseHTTPRequestHandler):
    server_version = "EchoSTT-GCP/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            try:
                _load()
                self._json(
                    200,
                    {
                        "ok": True,
                        "provider": "google-cloud-speech",
                        "model": MODEL,
                        "default_language": DEFAULT_LANG,
                        "alternative_languages": ALT_LANGS,
                    },
                )
            except Exception as exc:
                self._json(500, {"ok": False, "error": f"init_failed: {exc}"})
        else:
            self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/transcribe":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            wav = base64.b64decode(body["wav_base64"])
        except Exception as exc:
            self._json(400, {"ok": False, "error": f"bad_request: {exc}"})
            return
        try:
            out = transcribe(wav, body.get("language"))
        except Exception as exc:
            print(f"[stt] failed: {exc}", flush=True)
            self._json(500, {"ok": False, "error": f"stt_failed: {exc}"})
            return
        print(
            f"[stt] {out['latency_ms']}ms provider=gcp "
            f"lang={out['language']} text={out['text']!r}",
            flush=True,
        )
        self._json(200, out)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8797)
    args = ap.parse_args()

    _load()
    httpd = ThreadingHTTPServer((args.host, args.port), _H)
    print(
        f"[ready] Echo STT server on http://{args.host}:{args.port} "
        f"provider=google-cloud-speech model={MODEL}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

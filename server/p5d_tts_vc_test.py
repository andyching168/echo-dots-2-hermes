#!/usr/bin/env python3
"""Standalone test of the Breeze TTS -> Seed-VC -> 48k path (Phase 5D).
Run under ~/voice/echo-bridge/.venv-wakeword on the server host."""

import asyncio
import os
import sys
import wave

sys.path.insert(0, os.path.expanduser("~/voice/echo-bridge"))
import echo_oneshot as eo  # noqa: E402

TEXT = os.getenv("TTS_TEST_TEXT", "你好，我是 Hiyori，這是一段聲音轉換測試。")
OUT = os.path.expanduser("~/voice/echo-bridge/logs/p5d_tts_vc_48k.wav")


async def main():
    pcm24, sr = await eo.breeze_tts(TEXT)
    print(f"[tts] {len(pcm24)/2/sr:.2f}s @ {sr} Hz")
    out22 = await eo.seed_vc(pcm24)
    print(f"[seed-vc] {len(out22)/2/22050:.2f}s @ 22050 Hz")
    out48 = eo.resample_pcm16(out22, 22050, 48000)
    print(f"[resample] {len(out48)/2/48000:.2f}s @ 48000 Hz")
    with wave.open(OUT, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(out48)
    print(f"[out] {OUT}")
    return 0 if len(out48) > 48000 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

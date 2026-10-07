#!/usr/bin/env python3
"""Phase 5D server-side dry run (no Echo hardware).

Synthesizes a spoken question, feeds it to a real OneShotOrchestrator as if it
were the AEC-clean uplink, captures the 48 kHz answer audio the orchestrator
produces, and verifies the controller returns to IDLE.

Run under ~/voice/echo-bridge/.venv-wakeword.
"""

import asyncio
import io
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.expanduser("~/voice/echo-bridge"))
import echo_oneshot as eo  # noqa: E402
from echo_controller import EchoController  # noqa: E402

TEXT = os.getenv("DRYRUN_TEXT", "123 加 456 是多少？")
OUT = os.path.expanduser("~/voice/echo-bridge/logs/p5d_dryrun_answer_48k.wav")
UTT_OUT = os.path.expanduser("~/voice/echo-bridge/logs/p5d_dryrun_utt_16k.wav")


async def synth_utt_16k(text: str) -> bytes:
    pcm24, sr = await eo.breeze_tts(text)
    return eo.resample_pcm16(pcm24, sr, 16000)


async def main():
    print(f"[dryrun] question={TEXT!r}")
    wake = await synth_utt_16k("OK Hiyo")
    utt = await synth_utt_16k(TEXT)
    lead = wake + b"\x00" * 16000  # "OK Hiyo", pause ~1s
    print(f"[dryrun] wake {len(wake)/2/16000:.2f}s + question {len(utt)/2/16000:.2f}s")
    with wave.open(UTT_OUT, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(lead + utt)

    ctl = EchoController()
    orch = eo.OneShotOrchestrator(ctl)
    ctl.set_sink(orch.source)

    captured = bytearray()
    stop = asyncio.Event()

    async def downlink():
        while not stop.is_set():
            frame = orch.source.next_frame()
            captured.extend(frame.tobytes())
            await asyncio.sleep(0.01)

    dl = asyncio.ensure_future(downlink())
    t0 = time.time()
    ctl.wake("test")
    feed = lead + utt
    # feed 10 ms frames, then trailing silence so the VAD can close the utterance
    for i in range(0, len(feed), 320):
        orch.feed(feed[i:i + 320])
        await asyncio.sleep(0.004)
    silence = b"\x00" * 320
    for _ in range(120):  # 1.2 s
        orch.feed(silence)
        await asyncio.sleep(0.004)

    final = None
    for _ in range(900):
        if ctl.get_state() == "IDLE":
            final = "IDLE"
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.4)
    stop.set(); dl.cancel()
    try:
        await dl
    except asyncio.CancelledError:
        pass

    with wave.open(OUT, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(bytes(captured))
    print(f"[dryrun] final_state={final} total={time.time()-t0:.2f}s "
          f"answer_audio={len(captured)/2/48000:.2f}s -> {OUT}")
    orch.close()
    return 0 if (final == "IDLE" and len(captured) > 48000) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

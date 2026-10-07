#!/usr/bin/env python3
"""Phase 5D.5 — one-shot LED timeline test (server side, no Echo audio).

Drives the REAL one-shot pipeline (VAD -> STT -> router -> Hermes -> TTS ->
Seed-VC) with a recorded 16 kHz utterance and records the semantic LED
timeline, proving:

    listening -> thinking -> speaking -> thinking -> idle

Run on the server host under the wakeword venv:
    ~/voice/echo-bridge/.venv-wakeword/bin/python oneshot_led_timeline_test.py
"""

import asyncio
import os
import sys
import time
import wave

BRIDGE = os.path.expanduser("~/voice/echo-bridge")
sys.path.insert(0, BRIDGE)

import echo_controller  # noqa: E402
import echo_led  # noqa: E402
import echo_oneshot  # noqa: E402

FRAME16 = 160 * 2
INPUT = os.path.join(BRIDGE, "p5d5_test_utt.wav")


async def main():
    loglines = []

    def logfn(m):
        loglines.append((time.monotonic(), m))
        print(m, flush=True)

    ctl = echo_controller.EchoController(log=logfn)
    led = echo_led.LedProjector(ctl, log=logfn)
    ctl.attach_led(led)
    led.start()
    orch = echo_oneshot.OneShotOrchestrator(ctl, log=logfn)

    run = {"v": True}
    timeline = []

    async def consumer():
        while run["v"]:
            orch.source.next_frame()
            await asyncio.sleep(0.01)

    async def monitor():
        last = None
        while run["v"]:
            s = led.status()["state"]
            if s != last:
                timeline.append((time.monotonic(), s))
                last = s
            await asyncio.sleep(0.01)

    cons = asyncio.ensure_future(consumer())
    mon = asyncio.ensure_future(monitor())

    t0 = time.monotonic()
    ctl.wake("test")
    # pass the 800 ms chime guard with silence, then feed the real utterance
    await asyncio.sleep(0.9)
    guard = b"\x00" * (FRAME16 * 100)
    for i in range(0, len(guard), FRAME16):
        orch.feed(guard[i:i + FRAME16])
        await asyncio.sleep(0.003)

    with wave.open(INPUT, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = w.readframes(w.getnframes())
    print(f"[test] feeding {len(pcm)/2/16000:.2f}s utterance", flush=True)
    for i in range(0, len(pcm), FRAME16):
        orch.feed(pcm[i:i + FRAME16])
        await asyncio.sleep(0.005)
    silence = b"\x00" * (FRAME16 * 150)
    for i in range(0, len(silence), FRAME16):
        orch.feed(silence[i:i + FRAME16])
        await asyncio.sleep(0.003)

    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if ctl.get_state() == ctl.IDLE:
            break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.5)
    run["v"] = False
    cons.cancel()
    mon.cancel()
    await asyncio.sleep(0.1)
    await led.aclose()
    orch.close()

    print("\n=== LED timeline (elapsed s, state) ===")
    for t, s in timeline:
        print(f"  {t - t0:6.2f}  {s}")
    print("\n=== lifecycle lines ===")
    for t, m in loglines:
        print(f"  {t - t0:6.2f}  {m}")

    states = [s for _, s in timeline]
    order = ["listening", "thinking", "speaking"]
    idx = [states.index(x) if x in states else -1 for x in order]
    ok = (idx[0] >= 0 and idx[1] > idx[0] and idx[2] > idx[1]
          and states[-1] == "idle")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} timeline={states}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

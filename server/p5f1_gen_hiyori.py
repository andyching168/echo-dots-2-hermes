#!/usr/bin/env python3
"""Phase 5F.1 — generate a long local Hiyori 48 kHz WAV for the no-billing rig.

Uses the local Breeze TTS (:10901) + shared Seed-VC (:8790) only; no GPT call.
Run on Ubuntu from ~/voice/echo-bridge with the venv python.
"""
from __future__ import annotations

import asyncio
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.expanduser("~/voice/echo-bridge"))
import echo_oneshot as eo  # noqa: E402

PARAS = [
    "歡迎回來。現在為你朗讀一段很長的說明，讓麥克風與回音消除系統可以持續運作。",
    "羅馬帝國從西元前二十七年的奧古斯都開始，經歷了將近五百年的時間。它的疆域橫跨歐洲、北非與西亞，是古代世界規模最大的帝國之一。",
    "帝國早期的道路、法律與城市建設相當發達，拉丁語成為整個地中海世界的共通語言，貿易與文化交流非常頻繁。",
    "到了第三世紀，帝國面臨通貨膨脹、邊境壓力與內部權力鬥爭等問題，戴克里先與君士坦丁嘗試以分治與改革來維持秩序。",
    "西元四七六年西羅馬帝國滅亡，東羅馬帝國則以拜占庭的名義繼續存在了將近一千年，直到一四五三年君士坦丁堡陷落。",
    "這是一段持續播放的語音，用來測試在真實房間中，當使用者打斷播放時，裝置需要多久才會安靜下來。",
    "客廳現在的溫度大約是二十四度，濕度是百分之五十五，空氣品質良好，窗戶是關著的，電風扇沒有運轉。",
    "如果你聽到這段話，表示播放路徑、回音消除與本地偵測器都在正常運作，我們可以繼續進行下一個測試階段。",
]

OUT24 = "/tmp/opencode/p5f1_hiyori_segments"
OUT48 = os.path.expanduser("~/voice/echo-bridge/test_audio/p5f1_hiyori_long.wav")


async def main():
    os.makedirs(OUT24, exist_ok=True)
    os.makedirs(os.path.dirname(OUT48), exist_ok=True)
    segs = []
    for i, p in enumerate(PARAS):
        pcm24, sr = await eo.breeze_tts(p)
        if sr != 24000:
            pcm24 = eo.resample_pcm16(pcm24, sr, 24000)
        out22 = await eo.seed_vc(pcm24)
        pcm48 = eo.resample_pcm16(out22, 22050, 48000)
        segs.append(pcm48)
        print(f"[gen] para {i+1}/{len(PARAS)}: tts={len(pcm24)/2/24000:.2f}s "
              f"vc={len(out22)/2/22050:.2f}s", flush=True)
        segs.append(b"\x00" * (48000 * 2 // 4))  # 250 ms gap
    data = b"".join(segs)
    with wave.open(OUT48, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(data)
    print(f"[gen] wrote {OUT48} total={len(data)/2/48000:.1f}s", flush=True)
    x = np.frombuffer(data, dtype="<i2").astype(np.float32)
    print(f"[gen] rms={np.sqrt((x**2).mean()):.0f} peak={np.abs(x).max():.0f}")


if __name__ == "__main__":
    asyncio.run(main())

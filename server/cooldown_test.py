#!/usr/bin/env python3
"""Phase 5A cooldown test: verify a single utterance triggers once and a
sub-2 s repeat is suppressed, while a >2 s repeat triggers again.

Cooldown is measured in audio time, so this is deterministic.
"""

from __future__ import annotations

import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
from wakeword import WakeWordDetector  # noqa: E402

SR = 16000
FRAME = 160
_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL_CANDIDATES = [
    os.path.join(_HERE, "server", "wakeword_models"),
    os.path.join(_HERE, "wakeword_models"),
]
MODELS = next((p for p in _MODEL_CANDIDATES if os.path.isdir(p)), _MODEL_CANDIDATES[0])
_CANDIDATES = [
    os.path.join(_HERE, "wakeword", "ok_hiyo_v1", "ok_hiyo_hee_yo.wav"),
    os.path.expanduser("~/voice/echo-bridge/test_audio/ok_hiyo/ok_hiyo_hee_yo.wav"),
    os.path.join(_HERE, "test_audio", "ok_hiyo", "ok_hiyo_hee_yo.wav"),
]
WAV = next((p for p in _CANDIDATES if os.path.exists(p)), _CANDIDATES[0])


def read(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def sil(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.int16)


def detect(name: str, audio: np.ndarray, cooldown: float) -> int:
    det = WakeWordDetector(
        model_paths=[os.path.join(MODELS, "ok_hiyo_v1_selfcontained.onnx")],
        melspec_path=os.path.join(MODELS, "melspectrogram.onnx"),
        embedding_path=os.path.join(MODELS, "embedding_model.onnx"),
        threshold=0.50,
        cooldown=cooldown,
        verbose=False,
        async_processing=False,
    )
    events = []
    det._on_score = lambda mono_s, score, state: events.append((mono_s, score, state)) if state else None
    for i in range(0, len(audio), FRAME):
        det.feed(audio[i : i + FRAME].tobytes())
    det.stop()
    trig = [e for e in events if e[2] == "TRIGGER"]
    cool = [e for e in events if e[2] == "cooldown"]
    print(
        f"  {name:<34} triggers={len(trig)} cooldown_frames={len(cool)} "
        f"trigger_scores={[round(e[1], 3) for e in trig]}"
    )
    return len(trig)


def main() -> int:
    phrase = read(WAV)
    print(f"cooldown test: phrase={os.path.basename(WAV)} cooldown=2.0s threshold=0.50")
    n1 = detect("single utterance", np.concatenate([sil(1.0), phrase, sil(2.5)]), 2.0)
    n2 = detect("two utterances, 0.5 s apart", np.concatenate([sil(1.0), phrase, sil(0.5), phrase, sil(2.5)]), 2.0)
    n3 = detect("two utterances, 2.5 s apart", np.concatenate([sil(1.0), phrase, sil(2.5), phrase, sil(2.5)]), 2.0)
    ok = (n1 == 1) and (n2 == 1) and (n3 == 2)
    print(f"RESULT: {'PASS' if ok else 'FAIL'} (expect 1 / 1 / 2)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

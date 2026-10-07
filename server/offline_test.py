#!/usr/bin/env python3
"""Phase 5A offline streaming test for the OK Hiyo wake-word model.

Runs the *same* WakeWordDetector used by the live Echo path, feeding 160-sample
frames exactly like the Echo uplink does, and reports the streaming max score.

Usage:
    python offline_test.py --models server/wakeword_models \
        ok_hiyo_hee_yo.wav ok_hiyo_hee-yo.wav ok_hiyo_hiyo.wav \
        --noise-labels 'silence,white,tones'

Exit code 0 if every positive wav peaks >= --threshold and every negative stays
below it.
"""

from __future__ import annotations

import argparse
import array
import os
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
from wakeword import WakeWordDetector  # noqa: E402

FRAME = 160
SR = 16000


def pad(audio: np.ndarray, pre_s: float, post_s: float) -> np.ndarray:
    pre = np.zeros(int(SR * pre_s), dtype=np.int16)
    post = np.zeros(int(SR * post_s), dtype=np.int16)
    return np.concatenate([pre, audio, post])


def read_wav_16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise SystemExit(f"{path}: only 16-bit WAV supported")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.int32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int32)
    if rate != SR:
        ratio = SR / rate
        n = int(len(x) / ratio)
        idx = np.arange(n) * ratio
        i0 = idx.astype(np.int64)
        i1 = np.minimum(i0 + 1, len(x) - 1)
        frac = idx - i0
        x = (x[i0] * (1 - frac) + x[i1] * frac).astype(np.int32)
    return x.astype(np.int16)


def synthetic_negative(kind: str, seconds: float = 3.0) -> np.ndarray:
    n = int(SR * seconds)
    if kind == "silence":
        return np.zeros(n, dtype=np.int16)
    if kind == "white":
        rng = np.random.default_rng(1234)
        return (rng.normal(0, 0.05, n) * 32767).astype(np.int16)
    if kind == "tones":
        t = np.arange(n) / SR
        sig = 0.15 * np.sin(2 * np.pi * 440 * t) + 0.12 * np.sin(2 * np.pi * 1200 * t)
        return (sig * 32767).astype(np.int16)
    raise SystemExit(f"unknown synthetic negative: {kind}")


def run(model_dir: str, name: str, audio: np.ndarray, threshold: float, cooldown: float, model_name: str):
    detector = WakeWordDetector(
        model_paths=[os.path.join(model_dir, model_name)],
        melspec_path=os.path.join(model_dir, "melspectrogram.onnx"),
        embedding_path=os.path.join(model_dir, "embedding_model.onnx"),
        threshold=threshold,
        cooldown=cooldown,
        verbose=False,
        async_processing=False,
    )
    scores = []
    detector._on_score = lambda mono_s, score, state: scores.append((mono_s, score))
    for i in range(0, len(audio), FRAME):
        detector.feed(audio[i : i + FRAME].tobytes())
    audio_s = len(audio) / SR
    if scores:
        best_t, best = max(scores, key=lambda z: z[1])
    else:
        best_t, best = 0.0, 0.0
    triggers = detector.trigger_count
    detector.stop()
    return {
        "name": name,
        "label": detector.label,
        "duration": audio_s,
        "n_scores": len(scores),
        "max_score": best,
        "max_at": best_t,
        "triggers": triggers,
        "scores": scores,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="server/wakeword_models")
    ap.add_argument("--model-name", default="ok_hiyo_v1.onnx")
    ap.add_argument("--threshold", type=float, default=0.50)
    ap.add_argument("--cooldown", type=float, default=2.0)
    ap.add_argument("--noise-labels", default="silence,white,tones")
    ap.add_argument("--pad-start", type=float, default=1.0)
    ap.add_argument("--pad-end", type=float, default=2.0)
    ap.add_argument("--speech", action="append", default=[], help="extra 16k WAV non-wake speech")
    ap.add_argument("wavs", nargs="*")
    args = ap.parse_args()

    positives = args.wavs
    negatives = [s for s in args.noise_labels.split(",") if s]

    print(f"model_dir={args.models} threshold={args.threshold} cooldown={args.cooldown}")
    print("=" * 78)

    ok = True
    print("POSITIVES (expect max_score >= threshold)")
    for path in positives:
        r = run(args.models, os.path.basename(path), pad(read_wav_16k(path), args.pad_start, args.pad_end), args.threshold, args.cooldown, args.model_name)
        verdict = "PASS" if r["max_score"] >= args.threshold else "FAIL"
        ok = ok and r["max_score"] >= args.threshold
        print(
            f"  [{verdict}] {r['name']:<28} dur={r['duration']:.2f}s "
            f"max={r['max_score']:.4f} @ {r['max_at']:.2f}s triggers={r['triggers']}"
        )

    print("NEGATIVES (expect max_score < threshold)")
    for label in negatives:
        r = run(args.models, label, synthetic_negative(label), args.threshold, args.cooldown, args.model_name)
        verdict = "PASS" if r["max_score"] < args.threshold else "FAIL"
        ok = ok and r["max_score"] < args.threshold
        print(
            f"  [{verdict}] {r['name']:<28} dur={r['duration']:.2f}s "
            f"max={r['max_score']:.4f} @ {r['max_at']:.2f}s triggers={r['triggers']}"
        )
    for path in args.speech:
        r = run(args.models, os.path.basename(path), pad(read_wav_16k(path), args.pad_start, args.pad_end), args.threshold, args.cooldown, args.model_name)
        verdict = "PASS" if r["max_score"] < args.threshold else "FAIL"
        ok = ok and r["max_score"] < args.threshold
        print(
            f"  [{verdict}] speech:{r['name']:<21} dur={r['duration']:.2f}s "
            f"max={r['max_score']:.4f} @ {r['max_at']:.2f}s triggers={r['triggers']}"
        )

    print("=" * 78)
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

# OK Hiyo openWakeWord v1 — RTX 3060 rebuild

Date: 2026-09-18

## Final artifact

- Model: `v1_model/ok_hiyo_v1_selfcontained.onnx`
- Size: 205410 bytes
- SHA256: `77abde27357d86bc7c59f8fc78f4b9584e22202151b5f891ea59c83842e5ca04`
- Input: float32 `[1, 16, 96]`
- Output: sigmoid `[1, 1]`
- External ONNX data: **none**
- Verified by copying only the ONNX file into an empty temporary directory and loading it successfully with ONNX Runtime.

## Wake phrase

Training spelling: `okay hee yo`

## Training

- RTX 3060 12 GB
- 20,000 synthetic positive train clips
- 2,000 positive validation clips
- 20,000 synthetic adversarial-negative train clips
- 2,000 adversarial-negative validation clips
- 500 AudioSet source clips
- 2,458 two-second AudioSet windows
- 1,966 AudioSet general-negative training windows
- 492 AudioSet holdout windows
- 64 synthetic room impulse responses
- 10,000 primary training steps + auto-train refinement
- Config: `ok_hiyo_v1.yaml`

## Fresh streaming/sliding-window positive validation

| length scale | n | mean max score | recall > 0.5 | recall > 0.7 |
|---|---:|---:|---:|---:|
| 0.75 | 100 | 0.9203 | 98% | 98% |
| 1.00 | 100 | 0.9346 | 100% | 100% |
| 1.25 | 100 | 0.9358 | 100% | 100% |

## Negative checks

- Adversarial validation: 4 / 2000 > 0.5; 3 / 2000 > 0.7
- AudioSet training-negative pool: 0 / 1966 > 0.5
- AudioSet holdout: 0 / 492 > 0.3, 0 / 492 > 0.5, 0 / 492 > 0.7
- AudioSet holdout max score: 0.00288

Recommended initial deployment threshold: **0.50**. Collect real Echo-room wake, near-miss, TV, music, and self-playback scores before final threshold tuning.

## Packaging fix

This rebuild is self-contained. The final ONNX contains all weights internally and does **not** require an `.onnx.data` sidecar.

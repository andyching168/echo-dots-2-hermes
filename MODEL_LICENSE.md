# Model licensing

## Hiyo wake-word classifier

The project-specific wake-word classifier artifacts:

- `server/wakeword_models/ok_hiyo_v1.onnx`
- `server/wakeword_models/ok_hiyo_v1_selfcontained.onnx`
- `server/wakeword_models/ok_hiyo_v1.yaml`

are released under **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)**.

A copy of the license is included at `LICENSES/CC-BY-NC-SA-4.0.txt`.

This deliberately conservative model license keeps the project's custom wake-word artifacts aligned with the non-commercial/share-alike licensing used by the upstream openWakeWord pre-trained model assets involved in the training/inference ecosystem. It does not change the repository source-code license, which remains MIT.

## openWakeWord upstream models

The repository-wide MIT License does not relicense upstream openWakeWord pre-trained model assets such as `melspectrogram.onnx` and `embedding_model.onnx`. Those retain their upstream model terms; see `THIRD_PARTY_NOTICES.md`.

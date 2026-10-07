# Third-party notices

This repository is primarily original Hiyo Echo integration code, but it uses
or interoperates with several upstream projects. Their licenses remain their
own and are not changed by the license chosen for Hiyo Echo.

## Hermes Gadget SDK

The deploy script installs the Hermes Gadget platform plugin from
Adolanium/hermes-gadget-sdk, pinned to commit
`d661b8f2787bc79dd48a7c399ae76d2bbc0b7d41`.

Upstream: <https://github.com/Adolanium/hermes-gadget-sdk>

The upstream project states that its project code and documentation are MIT
licensed, with separate notices for bundled third-party components. Hiyo Echo
does not vendor the SDK source; it installs the plugin from upstream.

## SpeexDSP

`device/bin/echo_probe` contains statically linked SpeexDSP echo-cancellation
code. The corresponding source uses the SpeexDSP echo API.

Upstream: <https://github.com/xiph/speexdsp>

The BSD-style redistribution terms are included at
`LICENSES/SpeexDSP-COPYING.txt`.

## tinyalsa

The Echo-side audio client dynamically links the device's existing
`libtinyalsa.so`; this repository does not ship that shared library.

Upstream: <https://github.com/tinyalsa/tinyalsa>

The upstream notice is included at `LICENSES/tinyalsa-NOTICE.txt`.

## openWakeWord

The server installs openWakeWord as a Python dependency. Upstream code is
Apache-2.0 licensed.

Upstream: <https://github.com/dscripka/openWakeWord>

The files `server/wakeword_models/melspectrogram.onnx` and
`server/wakeword_models/embedding_model.onnx` are upstream pre-trained model
artifacts. openWakeWord documents its included pre-trained models as
CC BY-NC-SA 4.0. A copy of that license is included at
`LICENSES/CC-BY-NC-SA-4.0.txt`.

The project-specific `ok_hiyo_v1*.onnx` classifier was trained separately and is tracked in `server/wakeword_models/OK_HIYO_V1_3060_STATUS.md`. Those classifier artifacts are separately released under CC BY-NC-SA 4.0; see `MODEL_LICENSE.md`.

## External programs not redistributed here

The runtime can call ffmpeg, librespot, Hermes Agent, Seed-VC, and other local
services. They are external dependencies and are not redistributed as part of
this source bundle.

## Trademarks and affiliation

Amazon, Echo and Fire OS are trademarks of Amazon and/or its affiliates.
Hermes, Hermes Agent and Nous Research are names/marks of their respective
owners. Hiyo Echo is an independent interoperability and hardware-reuse
project; it is not affiliated with or endorsed by Amazon, Nous Research, or
the Hermes Gadget SDK project.

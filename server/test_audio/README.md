# Optional local test audio

Audio samples from the development machine are intentionally not distributed in
the public bundle because their recording / voice provenance is not part of the
project's source license.

Production Question Mode, wake-word inference, Gadget mode, GPT Live and normal
Echo playback do not require these WAV files.

For old diagnostic modes you may provide your own files locally:

- `speech48.wav` — arbitrary 48 kHz test speech for `--source speech/sequence`.
- `ok_hiyo/ok_hiyo_hee_yo.wav` — optional wake-word regression sample used by
  `cooldown_test.py`.
- `p5f1_hiyori_long.wav` — optional local no-billing rig sample; never required
  by production services.

`*.wav` under this directory is ignored by git.

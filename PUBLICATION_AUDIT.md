# Publication audit

Last reviewed: 2026-10-07

This directory is the intended public repository. The sibling `../refs/`
tree is a private/local research archive and should **not** be added to the
public repository: it contains firmware images, recovery/boot images, large
captures, third-party archives, build trees, and offline research copies.

## Ready

- Production server code refreshed from the live Ubuntu host after the Hermes
  Gadget migration.
- Gadget plugin install is pinned to a known SDK commit.
- Gadget shadow has a dedicated systemd owner; legacy fallback is isolated.
- Production Gadget identity state is not included.
- Production JEV credential was removed; the distributable unit defaults to
  `ECHO_JEV_GATE=0`.
- Secret-pattern scan found no private keys, bearer tokens, API keys, or similar
  credential material in the deploy bundle.
- Development WAV samples were moved out of the public tree.
- The copied XDA forum post was replaced with a short attribution/link instead
  of redistributing the post verbatim.
- Shell scripts parse with `bash -n`; Python source parses successfully.
- Third-party notices were added for SpeexDSP, tinyalsa, openWakeWord and the
  Hermes Gadget dependency.

## License decisions

- Hiyo Echo project code: **MIT** (`LICENSE`).
- Project-specific `ok_hiyo_v1` classifier/config: **CC BY-NC-SA 4.0** (`MODEL_LICENSE.md`).
- Third-party assets retain their upstream terms and are not relicensed by the repository MIT license.

## Remaining review before publishing

### Review wake-word model redistribution

The openWakeWord feature-extractor model files bundled here are covered by the
upstream model terms (CC BY-NC-SA 4.0, per upstream documentation), not by the
future Hiyo Echo source-code license.

The custom `ok_hiyo_v1*.onnx` classifier is deliberately released under CC BY-NC-SA 4.0. This keeps the published wake-word model side conservative and avoids implying commercial rights broader than the surrounding openWakeWord model ecosystem. A future commercially permissive model should be retrained from a clean, separately reviewed dataset and released as a new artifact.

## Known caveats to keep documented

- Some shipped Echo-side binaries were captured from the proven live device and
  do not correspond byte-for-byte to every source snapshot in `device/src/`.
  `docs/PROVENANCE.md` records this explicitly.
- `echo_probe` statically contains SpeexDSP code; retain its BSD-style notice
  with binary releases.
- The project depends on a rooted Echo Dot 2 and an external unlock/recovery
  process. The public repository links to the upstream XDA thread rather than
  redistributing firmware or unlock archives.
- GPT Live, Seed-VC, Breeze TTS, Hermes Agent, Spotify/librespot and Home
  Assistant are optional/external services with their own terms and setup.
- IP addresses in examples are RFC1918 LAN examples captured from the test
  deployment; deployment scripts rewrite host paths/IP where appropriate.

## Recommended public-repo root

Use this directory itself (`echo-dots-2-hermes/`) as the Git repository root.
Do not initialize Git at `rs03qr/v2/`, because the sibling `refs/` archive
is intentionally outside the publishable project.

# Voice / Live configuration

The public/default profile uses **Hermes' own TTS configuration** for normal
Question Mode. The project-specific direct Breeze path is still available for
the original Hiyo deployment.

## TTS ownership

Hermes already has a first-class `tts:` configuration (`tts.provider`, provider
voice/model settings, credentials, command providers and plugin providers).
`hermes-echo-tts.service` runs inside the Hermes Agent Python environment and
exposes that configured backend to the Echo bridge on `127.0.0.1:8795`.

The bridge therefore does not need to know whether Hermes uses Edge, OpenAI,
ElevenLabs, Gemini, Piper, a local engine, or a custom `type: command` provider.
It asks Hermes' TTS layer for audio, then optionally sends that audio through
Seed-VC before playback.

### Normal Question Mode

- `--oneshot-tts hermes` — use Hermes `tts.provider` (recommended/public default).
- `--oneshot-tts direct` — use `ECHO_BREEZE_URL` directly (original Hiyo path).
- `--oneshot-vc` / `--no-oneshot-vc` — independently enable/disable Seed-VC.

So these are all valid:

```bash
# Generic public profile: Hermes-selected TTS, no custom voice conversion
--oneshot --oneshot-tts hermes --no-oneshot-vc

# Hermes-selected TTS, then convert it to the Hiyo voice
--oneshot --oneshot-tts hermes --oneshot-vc

# Original deployment: call the local Breeze endpoint directly, then Seed-VC
--oneshot --oneshot-tts direct --oneshot-vc
```

Configure the first two with Hermes itself, e.g. `hermes setup tts` or the
normal `tts:` section in `~/.hermes/config.yaml`. The Echo project does not
select a provider behind Hermes' back.

## GPT Live

Live is separate from normal Question Mode TTS:

- omit `--live` — no GPT Live session exists at all;
- `--live --no-live-vc` — use the GPT Live/vendor voice directly;
- `--live --live-vc` — route GPT Live output through Seed-VC;
- legacy `--vc` enables Seed-VC for both one-shot and Live unless a per-path
  flag overrides it.

If Live is not enabled and the router hears a Live request, the request falls
back to a normal Hermes Question turn rather than causing an error state.

## Common profiles

| Goal | Server flags |
|---|---|
| Generic Hermes assistant, no Live | `--oneshot --oneshot-tts hermes --no-oneshot-vc` |
| Hermes TTS + Hiyo voice, no Live | `--oneshot --oneshot-tts hermes --oneshot-vc` |
| Hermes TTS + GPT Live original voice | `--oneshot --oneshot-tts hermes --no-oneshot-vc --live --no-live-vc` |
| Hermes TTS + Hiyo voice + Live original voice | `--oneshot --oneshot-tts hermes --oneshot-vc --live --no-live-vc` |
| Original Hiyo production style | `--oneshot --oneshot-tts direct --live --vc` |

## Why there is a localhost TTS bridge

The Echo server intentionally runs in a small project venv, while Hermes owns
its TTS provider registry, config loader, credentials, local model caches and
plugin/command-provider lifecycle in the Hermes Agent environment. Importing
Hermes internals directly into the Echo process would couple those environments.

`echo_hermes_tts.py` keeps that boundary explicit:

```text
Echo Question Mode
  -> localhost :8795
  -> Hermes text_to_speech_tool
  -> configured tts.provider
  -> WAV back to Echo bridge
  -> optional Seed-VC
  -> existing Echo downlink
```

The bridge is persistent, so local Hermes TTS engines can retain their normal
process-level model caches instead of being loaded in a new subprocess for
every answer.

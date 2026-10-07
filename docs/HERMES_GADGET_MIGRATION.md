# Hermes Gadget migration for Echo Dot (Hiyo)

## Current checkpoint

- Hermes Gadget plugin is installed and enabled in Hermes.
- Gadget listener moved to port 8766 because 8765 is already used by remote-mcp-gateway.
- Echo is enrolled as `hg-9890ea0739c6d9c0` / `Echo Dot (Hiyo)`.
- `echo_gadget_shadow.py` is the Gadget adapter. It does not touch audio and is
  owned exclusively by `hermes-echo-gadget.service`.
- `echo_turn_server.py` is now only the localhost compatibility/recovery facade on
  `:8796`; the isolated legacy worker is started on `:8799` only when needed.
- Existing EchoController remains authoritative for mute, buttons, state, LED and volume.

## Phase 0 — shadow control plane

The Gadget device declares safe actions that proxy to the existing localhost admin socket:

- echo.get_state
- echo.get_volume
- echo.enter_live_mode
- echo.end_live_mode
- echo.interrupt
- echo.set_volume
- echo.play_chime

Physical mute is intentionally not exposed as a writable action.


## Phase 1 — first-class Gadget Question session

Completed on 2026-10-07:

- `echo_gadget_shadow.py` now owns a localhost newline-JSON control API on
  `127.0.0.1:8798`.
- It can submit text turns through the enrolled Gadget session and collect
  `turn.start`, `status`, `reply.delta`, final `reply`, and `turn.end`.
- It conservatively denies Gadget approval prompts while this bridge is headless.
- The Gadget hello advertises UTF-8 text shaping so Chinese replies are preserved.
- A real Gadget tool round-trip was verified: Hermes called `gadget_action` ->
  `echo.get_volume` -> existing EchoController and correctly returned volume 73.
- `echo_turn_server.py` now has a live backend switch:
  - `GET /backend`
  - `POST /backend {"backend":"legacy"}`
  - `POST /backend {"backend":"gadget"}`
- Production port `:8796` was smoke-tested in both modes. Gadget mode returned a
  first-class Gadget answer; legacy mode was then restored and also passed.

The backend remained on `legacy` at the end of Phase 1; Phase 2 below promotes
Gadget to the default after the physical wake-word / speaker path was exercised.
Switching still does not require restarting `echo_server.py`.


## Phase 2 — Gadget becomes the default and survives restarts

Completed on 2026-10-07:

- Gadget is now the default Question Mode backend on `:8796`.
- If the Gadget bridge is unavailable or a Gadget turn fails, `echo_turn_server.py`
  automatically falls back to the legacy QuestionSessionManager and annotates the
  response with `fallback_from=gadget`.
- The fallback path was failure-tested by stopping the Gadget adapter; it returned
  a valid legacy answer, then the Gadget adapter was restored.
- `echo_gadget_shadow.py` now publishes Gadget telemetry:
  `volume_pct`, `mic_muted`, `session_active`, and an interaction-mode code.
  A real `gadget_devices` call saw the current controller state.
- The legacy Echo prompt plugin was updated for Gadget actions. Because Hermes
  enforces a 4000-character cap per plugin prompt section, the prompt is now split
  deterministically across two sections instead of being silently skipped.
- Until a dedicated Gadget systemd unit is installed, the existing
  `hermes-echo-turn.service` temporarily supervises `echo_gadget_shadow.py`.
  A restart test confirmed the adapter starts inside the service cgroup and
  reconnects/pairs automatically.


## Phase 3 — lazy legacy fallback

Completed on 2026-10-07:

- The legacy in-process Hermes Agent, plugin discovery, MCP discovery, and
  QuestionSessionManager are no longer initialized at service startup.
- On the normal Gadget path, `hermes-echo-turn.service` now runs only the small
  HTTP compatibility facade plus `echo_gadget_shadow.py`.
- Fresh-start memory dropped from roughly 266 MB to about 34 MB and the service
  no longer spawns Brave/Minimax/Trello MCP subprocesses during normal Gadget use.
- `GET /session` now reports `legacy_initialized` and any lazy-init error.
- If Gadget genuinely fails, the legacy stack initializes under a lock on first
  fallback request; this path was failure-tested successfully.
- If only `echo_gadget_shadow.py` dies, the turn server now restarts it and
  retries the Gadget turn once before initializing legacy. A kill/recovery test
  returned `gadget_recovered=true` while `legacy_initialized` stayed false.
- Switching `POST /backend {"backend":"legacy"}` explicitly initializes the
  legacy stack; switching back to Gadget ensures the shadow bridge is present.


## Phase 4 — one-shot direct Gadget path + legacy-only plugin

Completed on 2026-10-07:

- `echo_oneshot.py` now sends normal Question Mode turns directly to the local
  Gadget bridge on `127.0.0.1:8798`, bypassing the HTTP compatibility facade
  on `:8796` during healthy operation.
- Cancel and session-reset operations also prefer the direct Gadget control path.
- If the direct bridge is unavailable, one-shot automatically falls back to
  `:8796`; that facade can restart the Gadget shadow and retry before legacy.
- A direct-path unit-style test returned `oneshot-direct-ok` through Gadget.
- A kill/recovery test confirmed this chain:
  direct socket failure -> compatibility facade -> shadow restart -> Gadget reply,
  while `legacy_initialized` remained false.
- The `hermes-echo` plugin is now dormant by default. It registers its old
  `echo_*` tools, Echo prompt, and runtime-context hook only when
  `HERMES_ECHO_LEGACY_MODE=1`.
- The lazy legacy initializer sets that flag immediately before plugin discovery.
  A forced legacy test still returned `legacy-gated-ok` with the Echo runtime
  context hook active.
- Normal plugin discovery with the legacy flag unset now reports zero
  `pre_llm_call` callbacks from `hermes-echo`.
- Gadget actions remain functional after the old plugin is gated; a real
  `gadget_action -> echo.get_volume` round trip returned volume 70.


## Phase 5 — legacy worker extracted from the main process

Completed on 2026-10-07:

- The old full `echo_turn_server.py` implementation was preserved as
  `echo_legacy_worker.py`.
- `echo_turn_server.py` is now a thin compatibility/supervision facade. It uses
  only the Python standard library and does not import Hermes Agent code.
- Normal startup runs the thin facade and the independently managed
  `hermes-echo-gadget.service`; no legacy MCP subprocesses are spawned.
- Port `:8796` remains compatible for old callers:
  `/turn`, `/cancel`, `/reset`, `/backend`, `/session`, and `/health`.
- Explicit `backend=legacy` starts `echo_legacy_worker.py` on localhost
  `:8799` with `HERMES_ECHO_LEGACY_MODE=1`, forwarding the original MCP and
  QuestionSessionManager configuration.
- A real isolated-worker test returned `worker-ok` with the old runtime-context
  hook still active.
- After returning to Gadget, the legacy worker is reaped after 300 seconds of
  inactivity so its large Hermes/MCP footprint is not permanent.
- Gadget self-heal was re-tested after extraction: killing the shadow process and
  sending a turn caused the thin facade to restart it and return
  `facade-recover-ok` with `gadget_recovered=true`; no legacy worker started.


## Phase 6 — production ownership cleanup

Completed on 2026-10-07:

- `echo_gadget_shadow.py` now has one process owner only:
  `hermes-echo-gadget.service`.
- `echo_legacy_worker.py` can no longer spawn, terminate, or supervise a Gadget
  shadow. Its old compatibility check now only verifies whether `:8798` is
  reachable and otherwise reports that Gadget is systemd-managed.
- The legacy worker defaults to `backend=legacy` when run directly, matching its
  isolated fallback role.
- The obsolete Phase 1 `echo_gadget_turn_proxy.py` was moved to
  `archive/echo_gadget_turn_proxy.py`; no service or active code path references it.
- The migration documentation and current checkpoint were updated to reflect the
  dedicated Gadget service and the 300-second legacy-worker idle timeout.
- This closes the migration: local STT/EchoRouter, GPT Live, audio/AEC,
  Breeze/Seed-VC, Spotify/HA media and EchoController remain intentionally
  independent of the Gadget transport.

## Migration order

1. Keep the existing `echo_server.py` audio/controller path unchanged.
2. Run the Gadget shadow adapter in parallel and validate pairing/actions.
3. Route one-shot text turns through the Gadget platform session while preserving current Breeze + Seed-VC output.
4. Move reply/status/cancel/session-new semantics to Gadget protocol.
5. Only after parity, retire Hermes-specific routing from `echo_turn_server.py`.
6. Keep GPT Live, Spotify/radio, HA media and device hardware arbitration as side paths until separately migrated.

## Rollback

Disable/stop only the Gadget shadow adapter and set
`platforms.gadget.enabled=false` if necessary. The existing Echo stack remains
independent and should continue working without Gadget.

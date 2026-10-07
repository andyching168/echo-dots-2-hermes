"""hermes-echo — runtime context + remote Echo tools for Hermes.

Registers:
  * seven `echo_*` tools (backed by the authoritative EchoController via the
    Phase 5B admin console) — see tools.py
  * one static, cache-safe system-prompt section with the Echo interaction rules
  * one `pre_llm_call` hook that injects the live `[Echo Runtime Context]` block
    into every Echo-originating turn (per-turn dynamic context, never cached).

No hardware knowledge and no `echo_unmute` tool: physical mute is non-overridable.
"""

import os

from .prompt import ECHO_PROMPT_FRAGMENT
from . import schemas, tools

#: Platforms/sessions treated as Echo-originating. The Echo turn path (text
#: harness now, GPT Live delegation later) must run under one of these.
ECHO_PLATFORMS = {"echo", "echo_dot2", "echo_dot2_main"}


def _log_pre_llm(platform, session_id, injected):
    """Debug evidence: append one JSON line per pre_llm_call invocation."""
    try:
        import json as _json
        import os as _os
        import time as _time
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "pre_llm.log")
        with open(path, "a") as fh:
            fh.write(_json.dumps({
                "ts": _time.time(), "platform": platform,
                "session_id": session_id, "injected": bool(injected),
            }) + "\n")
    except Exception:
        pass


def _pre_llm_call(platform=None, session_id=None, user_message=None, **kwargs):
    """Hermes calls this once per turn. Inject the authoritative runtime context
    for Echo turns only; return None for everything else (no cache pollution)."""
    injected = False
    try:
        if platform not in ECHO_PLATFORMS:
            return None
        block = tools.fetch_context()
        if not block:
            return None
        injected = True
        return {"context": block}
    except Exception:
        # never break a turn because the Echo controller is unreachable
        return None
    finally:
        _log_pre_llm(platform, session_id, injected)


def register(ctx):
    # Gadget is now the primary Echo integration. Keep this plugin dormant in the
    # normal Hermes Gateway and enable it only inside the lazy legacy fallback.
    if os.getenv("HERMES_ECHO_LEGACY_MODE", "").strip().lower() not in ("1", "true", "yes", "on"):
        return

    for name, schema in schemas.SCHEMAS.items():
        handler = getattr(tools, name)
        ctx.register_tool(
            name=name,
            toolset="echo",
            schema=schema,
            handler=handler,
            check_fn=tools.admin_available,
            requires_env=[],
            description=schema.get("description", ""),
            emoji="🔊",
        )

    # Durable Echo interaction rules (static → cached prefix stays stable).
    # Hermes caps each plugin prompt section at 4000 chars, so keep the full
    # prompt but split it into deterministic chunks instead of silently skipping it.
    _chunk = 3900
    for _i, _start in enumerate(range(0, len(ECHO_PROMPT_FRAGMENT), _chunk), 1):
        ctx.register_system_prompt_section(
            f"echo-interface-{_i}",
            ECHO_PROMPT_FRAGMENT[_start:_start + _chunk],
            position="after_memory",
            max_chars=4000,
        )

    # Dynamic per-turn device state (never cached).
    ctx.register_hook("pre_llm_call", _pre_llm_call)

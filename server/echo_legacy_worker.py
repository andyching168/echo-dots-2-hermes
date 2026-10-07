#!/usr/bin/env python3
"""Phase 5D.6 — Echo turn server with a bounded Question Session Manager.

Differences vs the Phase 5D server:

* HA MCP tools are discovered at startup (``ha`` MCP server) so the real Echo
  ``AIAgent`` has first-class Home Assistant tools, not a terminal workaround.
* ONE ``AIAgent`` per **question session** is reused across independent one-shot
  wake cycles while the continuity TTL holds (``QuestionSessionManager``).
* Every turn logs real tool-call / tool-result activity via Hermes' stable
  ``tool_start_callback`` / ``tool_complete_callback`` hooks (secret-sanitized).
* Explicit reset phrases reset the Question-Mode session; a ``/cancel`` endpoint
  discards the in-flight turn from the conversational history.

Normally launched by the thin compatibility facade on 127.0.0.1:8799.
It never owns or spawns the Gadget adapter; that lifecycle belongs to
``hermes-echo-gadget.service``.

Endpoints::

    GET  /health
    POST /turn    {"message": "...", "session_key": "echo"} ->
                  {"ok", "answer", "session_id", "reused", "turn_index",
                   "hook_ran", "injected", "tool_calls", "latency_ms", "cancelled"}
    POST /cancel  {"session_key": "echo"} -> interrupt the in-flight turn
    POST /reset   {"session_key": "echo", "reason": "explicit"} -> discard session
    GET  /session -> {"ok", "ttl_s", "sessions": [...]}
    POST /shutdown
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Activate Hermes managed dependencies before serving. This bootstrap may re-exec
# the process, so it must happen only at process startup, never during a request.
import hermes_bootstrap  # noqa: E402,F401

from echo_qsession import (  # noqa: E402
    RESET_REPLY,
    QuestionSessionManager,
    is_reset_phrase,
)

PLUGIN_DIR = os.path.expanduser("~/.hermes/plugins/hermes-echo")
HOOK_LOG = os.path.join(PLUGIN_DIR, "pre_llm.log")
PLATFORM = "echo"
DEFAULT_TTL = 300.0
GADGET_BRIDGE_HOST = os.getenv("ECHO_GADGET_LOCAL_HOST", "127.0.0.1")
GADGET_BRIDGE_PORT = int(os.getenv("ECHO_GADGET_LOCAL_PORT", "8798"))

logger = logging.getLogger("echo_legacy_worker")
_state = {"manager": None, "trace_path": None, "trace_lock": threading.Lock(),
          "backend": os.getenv("ECHO_Q_BACKEND", "legacy").strip().lower(),
          "legacy_init_lock": threading.Lock(), "legacy_config": None,
          "legacy_init_error": None}
_server_ref = {"httpd": None}


def _gadget_shadow_ready() -> bool:
    try:
        with socket.create_connection((GADGET_BRIDGE_HOST, GADGET_BRIDGE_PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _ensure_gadget_shadow() -> None:
    """Legacy worker never owns the Gadget adapter process.

    Gadget lifecycle belongs exclusively to hermes-echo-gadget.service.  This
    check exists only for old compatibility branches; it must never spawn a
    second shadow process from inside the legacy worker.
    """
    if _gadget_shadow_ready():
        return
    raise RuntimeError(
        "Gadget shadow is unavailable; it is managed by "
        "hermes-echo-gadget.service, not by echo_legacy_worker.py"
    )


# ── startup ──────────────────────────────────────────────────────────────────
def _bootstrap_plugins() -> None:
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()
    manager = get_plugin_manager()
    n = len(manager.iter_hook_callbacks("pre_llm_call"))
    print(f"[bootstrap] plugins discovered; pre_llm_call callbacks={n}", flush=True)
    if n == 0:
        print("[bootstrap] WARNING: no pre_llm_call callback registered "
              "(is hermes-echo enabled?)", flush=True)


def _bootstrap_mcp(mcp_servers: list[str], attempts: int = 4) -> list[str]:
    """Connect the named MCP servers and register their tools; bounded retries.

    A cross-process discovery lock / connect cooldown can make a single pass race
    with the gateway's own discovery, so clear cooldowns and retry.
    """
    if not mcp_servers:
        return []
    from tools.mcp_tool_discovery import discover_mcp_tools, get_mcp_status

    wanted = list(mcp_servers)
    registered: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            from tools.mcp_tool_lifecycle import _clear_connect_cooldowns

            _clear_connect_cooldowns()
        except Exception:  # noqa: BLE001
            pass
        try:
            registered = discover_mcp_tools(allowed_mcp_names=wanted)
        except Exception as exc:  # noqa: BLE001
            print(f"[bootstrap] MCP discovery error (attempt {attempt}): {exc}", flush=True)
            registered = []
        if registered:
            break
        time.sleep(1.0)
    status = {s.get("name"): s for s in (get_mcp_status() or [])}
    for name in wanted:
        st = status.get(name, {})
        print(f"[bootstrap] MCP '{name}': status={st.get('status')} tools={st.get('tools')} "
              f"error={st.get('error', '')}", flush=True)
    print(f"[bootstrap] MCP tools registered: {registered}", flush=True)
    return registered


def _patch_ha_search_fast() -> bool:
    """Cap the slow part of mcp__ha__ha_search for Echo turns.

    The HA MCP `ha_search` scans automation/script/scene/helper config bodies in
    addition to the entity registry; on a large HA install that scan takes
    30-60s. The per-call `config_time_budget` caps it. We inject a small default
    when the caller did not set one (entities are still returned). A caller can
    override it explicitly for an automation/script question.
    """
    from tools.registry import registry  # noqa: PLC0415
    entry = registry.get_entry("mcp__ha__ha_search")
    if entry is None:
        return False
    orig = entry.handler
    was_async = bool(getattr(entry, "is_async", False))

    def _with_cap(args):
        if isinstance(args, dict) and "config_time_budget" not in args:
            return {**args, "config_time_budget": 1.0}
        return args

    if was_async:
        async def _patched(args, *a, **k):  # noqa: ANN001
            return await orig(_with_cap(args), *a, **k)
    else:
        def _patched(args, *a, **k):  # noqa: ANN001
            return orig(_with_cap(args), *a, **k)

    entry.handler = _patched
    entry.is_async = was_async
    return True


def _inject_tool_failures(prefixes: list[str]) -> list[str]:
    """Test-only fault injection: make matching registered tools raise.

    Used by the Phase 5D.6 HA-failure experiment to prove the model reports a
    truthful failure instead of fabricating a sensor value.
    """
    if not prefixes:
        return []
    from tools.registry import registry

    patched = []
    for name in registry.get_all_tool_names():
        if not any(name.startswith(p) or p in name for p in prefixes):
            continue
        entry = registry.get_entry(name)
        if entry is None:
            continue

        async def _fail(*a, _name=name, **k):
            raise RuntimeError(f"simulated tool failure: {_name}")

        entry.handler = _fail
        entry.is_async = True
        patched.append(name)
    return patched


def _resolve_runtime():
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs

    runtime = _resolve_runtime_agent_kwargs()
    model = runtime.pop("model", None) or _resolve_gateway_model()
    return model, runtime


def _resolve_toolsets(exclude: set[str]):
    """Gateway default surface + the plugin `echo` toolset, minus excluded ones.

    ``session_search`` is excluded for Echo Question turns so a bounded question
    session cannot silently pull unrelated past conversations into context.
    """
    try:
        from gateway.run import _load_gateway_config
        from hermes_cli.tools_config import _get_platform_tools

        cfg = _load_gateway_config()
        enabled = set()
        for plat in ("api_server", "cli"):
            try:
                enabled |= set(_get_platform_tools(cfg, plat))
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        print(f"[bootstrap] toolset resolution failed ({exc}); using ['echo']", flush=True)
        enabled = set()
    enabled.add("echo")
    return sorted(enabled - set(exclude or ()))


# ── tracing ──────────────────────────────────────────────────────────────────
def _trace(kind: str, **fields) -> None:
    parts = [f"{k}={v}" for k, v in fields.items()]
    line = f"[{kind}] " + " ".join(parts)
    print(line, flush=True)
    path = _state.get("trace_path")
    if not path:
        return
    try:
        rec = {"ts": time.time(), "kind": kind, **fields}
        with _state["trace_lock"]:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _last_hook_entry(session_id: str):
    try:
        with open(HOOK_LOG, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        return None
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if str(rec.get("session_id")) == str(session_id):
            return rec
    return None


def _find_injected_context(agent):
    for msg in getattr(agent, "_session_messages", None) or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        for key in ("api_content", "content"):
            val = msg.get(key)
            if isinstance(val, str) and "[Echo Runtime Context]" in val:
                return val[val.index("[Echo Runtime Context]"):]
    return None


def _cap_spoken(text: str, limit: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "！", "？", ". ", "! ", "? ", "\n"):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i + len(sep)].strip()
    return cut.strip()


# ── agent construction ───────────────────────────────────────────────────────
def _load_ha_skill() -> str:
    """Load the ha-mcp skill body (YAML frontmatter stripped) for injection."""
    try:
        import os as _os
        path = _os.path.expanduser("~/.hermes/skills/ha-mcp/SKILL.md")
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
        if txt.startswith("---"):
            parts = txt.split("---", 2)
            if len(parts) >= 3:
                txt = parts[2]
        return txt.strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[skill] ha-mcp not injected: {exc}", flush=True)
        return ""


# JEV (TypeSafe System One) intent gate
# Decides whether the ha-mcp skill should be auto-injected into this turn's
# ephemeral prompt, instead of injecting it unconditionally into every turn.
# Fail-open: any error/timeout/low-confidence falls back to injecting the skill.

JEV_ENDPOINT = os.environ.get("JEV_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")
JEV_TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "6.0"))
JEV_MIN_CONFIDENCE = float(os.environ.get("JEV_MIN_CONFIDENCE", "0.80"))

_JEV_INSTRUCTIONS = (
    "Does acting on this request require controlling, querying, or configuring "
    "Home Assistant devices, entities, or automations?"
)
_JEV_CRITERIA = {
    "ha": (
        "Requires Home Assistant: controlling or reading a light, switch, plug, "
        "thermostat, lock, cover, speaker/media player, sensor, scene, script, or "
        "automation managed by Home Assistant."
    ),
    "non_ha": (
        "Does not require Home Assistant: general knowledge, math, coding help, web "
        "lookups, chit-chat, timers, reminders, weather small-talk, or any request "
        "with no smart-home device or entity involved."
    ),
}


def _jev_enabled() -> bool:
    """Gate is on only when not disabled and an API key is present."""
    flag = os.environ.get("ECHO_JEV_GATE", "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("TYPESAFE_API_KEY", "").strip())


def _jev_needs_ha(message: str) -> tuple[bool, dict]:
    """Ask JEV whether this utterance needs Home Assistant.

    Returns ``(needs_ha, info)``. Any failure is reported as ``needs_ha=True`` so
    the caller keeps the previous always-inject behaviour (fail-open).
    """
    info: dict = {"decision": "inject", "reason": "fail_open"}
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        info["reason"] = "no_api_key"
        return True, info
    payload = json.dumps({
        "state": message,
        "model": JEV_MODEL,
        "questions": {
            "needs_ha": {
                "type": "choice",
                "instructions": _JEV_INSTRUCTIONS,
                "criteria": _JEV_CRITERIA,
            }
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        JEV_ENDPOINT, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=JEV_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        jms = int((time.time() - t0) * 1000)
        ans = (body.get("answers") or {}).get("needs_ha") or {}
        choice = ans.get("choice")
        conf = float(ans.get("confidence") or 0.0)
        p_ha = float((ans.get("probabilities") or {}).get("ha") or 0.0)
        info.update({"choice": choice, "confidence": round(conf, 3),
                     "p_ha": round(p_ha, 3), "latency_ms": jms,
                     "model": body.get("model")})
        if choice not in ("ha", "non_ha"):
            info["reason"] = "unexpected_choice"
            return True, info
        if conf < JEV_MIN_CONFIDENCE:
            info["decision"] = "inject"
            info["reason"] = "low_confidence"
            return True, info
        needs = choice == "ha"
        info["decision"] = "inject" if needs else "skip"
        info["reason"] = "jev_ha" if needs else "jev_non_ha"
        return needs, info
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"error:{type(exc).__name__}"
        return True, info


def _effective_ephemeral_prompt(message: str | None = None) -> str | None:
    """Config ephemeral prompt (e.g. tool-calling rules) + the ha-mcp skill body.

    The ha-mcp skill is gated by a JEV (TypeSafe System One) intent check: it is
    injected only when the utterance needs Home Assistant. The gate fails open, so
    an API error, timeout, or low-confidence answer restores the previous
    always-inject behaviour.
    """
    eph = ""
    try:
        from gateway.run import _load_gateway_config  # noqa: PLC0415
        cfg = _load_gateway_config()
        eph = ((cfg.get("agent") or {}).get("ephemeral_system_prompt") or "").strip()
    except Exception:  # noqa: BLE001
        pass

    if _jev_enabled() and message:
        needs_ha, info = _jev_needs_ha(message)
        _trace("jev", decision=info.get("decision"), reason=info.get("reason"),
               choice=info.get("choice"), confidence=info.get("confidence"),
               p_ha=info.get("p_ha"), latency_ms=info.get("latency_ms"),
               msg=json.dumps(message[:80], ensure_ascii=False))
        if not needs_ha:
            print(f"[jev] skip ha-mcp skill ({info.get('reason')} "
                  f"conf={info.get('confidence')} {info.get('latency_ms')}ms)", flush=True)
            return eph or None

    skill = _load_ha_skill()
    if skill:
        print(f"[skill] ha-mcp injected into ephemeral prompt ({len(skill)} chars)", flush=True)
        marker = (
            "# Home Assistant (ha-mcp) skill — auto-injected; authoritative for HA requests.\n"
            "Follow these operating rules for every Home Assistant request."
        )
        eph = f"{eph}\n\n{marker}\n\n{skill}".strip() if eph else f"{marker}\n\n{skill}"
    return eph or None


def _make_build_agent(cfg: dict):
    def _build(sess):
        from run_agent import AIAgent

        # The utterance that triggered this build; consumed by the JEV gate so the
        # ha-mcp skill is injected only when the turn actually needs Home Assistant.
        pending = _state.pop("pending_message", None)

        model, runtime = _resolve_runtime()
        mgr: QuestionSessionManager = _state["manager"]

        def _on_start(call_id, name, args):
            try:
                mgr.record_tool_call(sess, name, args)
            except Exception:  # noqa: BLE001
                pass

        def _on_complete(call_id, name, args, result):
            try:
                mgr.record_tool_result(sess, name, result)
            except Exception:  # noqa: BLE001
                pass

        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            command=runtime.get("command"),
            args=list(runtime.get("args") or []),
            model=model,
            enabled_toolsets=_resolve_toolsets(cfg["exclude_toolsets"]),
            max_iterations=cfg["max_iterations"],
            quiet_mode=True,
            verbose_logging=False,
            platform=PLATFORM,
            session_id=sess.session_id,
            session_db=None,
            credential_pool=runtime.get("credential_pool"),
            request_overrides=runtime.get("request_overrides"),
            capabilities=runtime.get("capabilities") or {},
            fallback_model=None,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            ephemeral_system_prompt=_effective_ephemeral_prompt(pending),
            tool_start_callback=_on_start,
            tool_complete_callback=_on_complete,
        )
        agent.suppress_status_output = True
        agent.stream_delta_callback = None
        agent.tool_gen_callback = None
        return agent

    return _build




def _ensure_legacy_backend() -> QuestionSessionManager:
    """Initialize the old in-process Agent/MCP stack only when it is actually needed."""
    mgr = _state.get("manager")
    if mgr is not None:
        return mgr

    with _state["legacy_init_lock"]:
        mgr = _state.get("manager")
        if mgr is not None:
            return mgr

        cfg = _state.get("legacy_config")
        if not isinstance(cfg, dict):
            raise RuntimeError("legacy backend configuration is unavailable")

        t0 = time.time()
        print("[legacy] lazy initialization starting", flush=True)
        try:
            # The hermes-echo plugin is dormant in the normal Gateway. Only this
            # emergency compatibility stack opts into its old tools/prompt/hooks.
            os.environ["HERMES_ECHO_LEGACY_MODE"] = "1"
            _bootstrap_plugins()
            mcp_servers = list(cfg["mcp_servers"])
            _bootstrap_mcp(mcp_servers)
            if "ha" in mcp_servers and _patch_ha_search_fast():
                print("[legacy] ha_search patched: config_time_budget cap=1.0s (fast)", flush=True)

            fail_prefixes = list(cfg["fail_prefixes"])
            if fail_prefixes:
                patched = _inject_tool_failures(fail_prefixes)
                print(f"[legacy] TEST fault injection -> failing tools: {patched}", flush=True)

            manager = QuestionSessionManager(
                build_agent=_make_build_agent({
                    "max_iterations": cfg["max_iterations"],
                    "exclude_toolsets": cfg["exclude_toolsets"],
                }),
                ttl=cfg["qsession_ttl"],
                max_sessions=cfg["qsession_max"],
                log=lambda m: print(m, flush=True),
                trace=_trace,
            )
            _state["manager"] = manager
            _state["legacy_init_error"] = None
            print(f"[legacy] lazy initialization ready in {int((time.time()-t0)*1000)}ms", flush=True)
            return manager
        except Exception as exc:
            _state["legacy_init_error"] = f"{type(exc).__name__}: {exc}"
            print(f"[legacy] lazy initialization failed: {_state['legacy_init_error']}", flush=True)
            raise


def _gadget_call(req: dict, timeout: float = 190.0) -> dict:
    """Call the local Gadget shadow bridge over its newline-JSON control socket."""
    with socket.create_connection((GADGET_BRIDGE_HOST, GADGET_BRIDGE_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode())
        line = s.makefile("r", encoding="utf-8").readline()
    if not line:
        raise RuntimeError("empty gadget bridge response")
    return json.loads(line)


def _run_gadget_turn(message: str, session_key: str) -> dict:
    if is_reset_phrase(message):
        out = _gadget_call({"op": "new"}, 10.0)
        return {"ok": bool(out.get("ok")), "answer": RESET_REPLY if out.get("ok") else "",
                "session_id": None, "reused": False, "turn_index": 0,
                "hook_ran": False, "injected": False, "native_context": True, "tool_calls": [],
                "latency_ms": 0, "cancelled": False, "reset": True,
                "platform": "gadget", "gadget": out}

    out = _gadget_call({"op": "turn", "message": message, "timeout_s": 180.0}, 190.0)
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error") or "gadget_turn_failed",
                "answer": "", "session_id": out.get("turn") or out.get("request_id"),
                "reused": True, "turn_index": None, "hook_ran": False,
                "injected": False, "native_context": True, "tool_calls": [], "latency_ms": out.get("latency_ms", 0),
                "cancelled": False, "platform": "gadget", "gadget": out}
    return {"ok": True, "answer": str(out.get("answer") or "").strip(),
            "session_id": out.get("turn") or out.get("request_id"),
            "reused": True, "turn_index": None, "hook_ran": False,
            "injected": False, "native_context": True, "tool_calls": [], "latency_ms": out.get("latency_ms", 0),
            "cancelled": out.get("outcome") == "cancelled",
            "platform": "gadget", "gadget": out}


def _gadget_cancel() -> dict:
    return _gadget_call({"op": "cancel"}, 5.0)


def _gadget_reset() -> dict:
    return _gadget_call({"op": "new"}, 10.0)


# ── turn ─────────────────────────────────────────────────────────────────────
def _history_of(agent):
    msgs = getattr(agent, "_session_messages", None)
    return msgs if isinstance(msgs, list) else []


def _last_user(msgs) -> str:
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            return " ".join(str(m.get("content") or "").split())[:80]
    return ""


def _restore_history(agent, snapshot: list) -> int:
    """Restore the exact pre-turn conversation history after a cancelled turn.

    ``run_conversation`` can rewrite ``_session_messages`` in place (and replace the
    list object) while a turn is interrupted, so truncating by length is not safe.
    """
    cur = len(_history_of(agent))
    agent._session_messages = [copy.deepcopy(m) for m in snapshot]
    return max(cur - len(snapshot), 0)


def _run_turn(message: str, session_key: str) -> dict:
    mgr: QuestionSessionManager = _ensure_legacy_backend()

    if is_reset_phrase(message):
        old = mgr.reset(session_key, "explicit_reset")
        _trace("qsession", action="reset", id=old, reason="explicit_reset")
        return {"ok": True, "answer": RESET_REPLY, "session_id": None,
                "old_session_id": old, "reused": False, "turn_index": 0,
                "hook_ran": False, "injected": False, "tool_calls": [],
                "latency_ms": 0, "cancelled": False, "reset": True,
                "platform": PLATFORM}

    _state["pending_message"] = message
    try:
        sess, reused = mgr.get_or_create(session_key, reason="wake")
    finally:
        _state.pop("pending_message", None)
    with sess.lock:
        sess.cancel_event.clear()
        with sess.trace_lock:
            sess.current_trace = []
        try:
            sess.agent.clear_interrupt()
        except Exception:  # noqa: BLE001
            pass
        snapshot_msgs = copy.deepcopy(_history_of(sess.agent))
        snapshot = len(snapshot_msgs)
        turn_index = sess.turns + 1
        _trace("hermes", session=sess.session_id, platform=PLATFORM, turn=turn_index,
               reused=reused, age=round(sess.age(), 1), session_key=session_key,
               history_before=snapshot, last_user_before=_last_user(snapshot_msgs))
        t0 = time.time()
        try:
            result = sess.agent.run_conversation(message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("turn failed")
            mgr.reset(session_key, "fatal_error")
            _trace("qsession", action="reset", id=sess.session_id, reason="fatal_error",
                   error=type(exc).__name__)
            return {"ok": False, "error": str(exc), "session_id": sess.session_id,
                    "reused": reused, "turn_index": turn_index, "latency_ms": int((time.time() - t0) * 1000)}

        latency_ms = int((time.time() - t0) * 1000)
        cancelled = sess.cancel_event.is_set()
        if cancelled:
            removed = _restore_history(sess.agent, snapshot_msgs)
            _trace("qsession", action="cancelled", id=sess.session_id, turn=turn_index,
                   discarded_messages=removed, history_len=len(_history_of(sess.agent)))
            return {"ok": True, "answer": "", "session_id": sess.session_id,
                    "reused": reused, "turn_index": turn_index, "hook_ran": False,
                    "injected": False, "tool_calls": [], "latency_ms": latency_ms,
                    "cancelled": True, "platform": PLATFORM}

        answer = ""
        if isinstance(result, dict):
            answer = result.get("final_response") or result.get("response") or ""
        raw_len = len(answer)
        answer = _cap_spoken(answer)

        entry = _last_hook_entry(sess.session_id)
        block = _find_injected_context(sess.agent)
        with sess.trace_lock:
            tool_names = [t["name"] for t in sess.current_trace if t["kind"] == "call"]
            tool_events = [dict(t) for t in sess.current_trace]
        mgr.touch(sess)
        history_after = len(getattr(sess.agent, "_session_messages", None) or [])
        _trace("hermes", session=sess.session_id, platform=PLATFORM, turn=turn_index,
               final_response=json.dumps(answer, ensure_ascii=False),
               raw_chars=raw_len, spoken_chars=len(answer), tool_calls=tool_names,
               latency_ms=latency_ms, hook_ran=bool(entry), injected=bool(entry and entry.get("injected")),
               history_after=history_after)
        return {
            "ok": True,
            "answer": answer,
            "session_id": sess.session_id,
            "reused": reused,
            "turn_index": turn_index,
            "hook_ran": entry is not None,
            "injected": bool(entry and entry.get("injected")),
            "hook_entry": entry,
            "context_in_prompt": block is not None,
            "prompt_context_excerpt": (block[:400] if block else None),
            "tool_calls": tool_names,
            "tool_events": tool_events,
            "latency_ms": latency_ms,
            "cancelled": False,
            "platform": PLATFORM,
        }


def _do_cancel(session_key: str) -> dict:
    mgr: QuestionSessionManager = _ensure_legacy_backend()
    sess = mgr.get(session_key)
    if sess is None:
        return {"ok": True, "cancelled": False, "reason": "no_session"}
    sess.cancel_event.set()
    try:
        sess.agent.interrupt("echo_cancelled", hard_cancel=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("interrupt failed: %s", exc)
    _trace("qsession", action="cancel_requested", id=sess.session_id)
    return {"ok": True, "cancelled": True, "session_id": sess.session_id}


# ── HTTP ─────────────────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "EchoTurnServer/1.1"

    def log_message(self, fmt, *args):
        logger.debug(fmt, *args)

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/health":
            self._json(200, {"ok": True})
        elif path == "/session":
            mgr = _state.get("manager")
            cfg = _state.get("legacy_config") or {}
            self._json(200, {"ok": True, "backend": _state["backend"],
                             "legacy_initialized": mgr is not None,
                             "legacy_init_error": _state.get("legacy_init_error"),
                             "ttl_s": (mgr.ttl if mgr is not None else cfg.get("qsession_ttl", DEFAULT_TTL)),
                             "sessions": (mgr.status() if mgr is not None else [])})
        elif path == "/backend":
            self._json(200, {"ok": True, "backend": _state["backend"],
                             "choices": ["legacy", "gadget"]})
        else:
            self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/shutdown":
            self._json(200, {"ok": True, "shutting_down": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        try:
            body = self._body()
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"bad_request: {exc}"})
            return

        if path == "/backend":
            mode = str(body.get("backend") or "").strip().lower()
            if mode not in ("legacy", "gadget"):
                self._json(400, {"ok": False, "error": "backend must be legacy or gadget"})
                return
            if mode == "legacy":
                _ensure_legacy_backend()
            elif not _gadget_shadow_ready():
                _ensure_gadget_shadow()
            _state["backend"] = mode
            self._json(200, {"ok": True, "backend": mode,
                             "legacy_initialized": _state.get("manager") is not None})
            return
        if path == "/cancel":
            if _state["backend"] == "gadget":
                self._json(200, _gadget_cancel())
            else:
                self._json(200, _do_cancel(str(body.get("session_key") or "echo")))
            return
        if path == "/reset":
            if _state["backend"] == "gadget":
                self._json(200, _gadget_reset())
                return
            mgr: QuestionSessionManager = _ensure_legacy_backend()
            key = str(body.get("session_key") or "echo")
            if body.get("cancel_first", True):
                _do_cancel(key)
            old = mgr.reset(key, str(body.get("reason") or "explicit"))
            _trace("qsession", action="reset", id=old, reason=str(body.get("reason") or "explicit"))
            self._json(200, {"ok": True, "old_session_id": old})
            return
        if path != "/turn":
            self._json(404, {"ok": False, "error": "not_found"})
            return

        message = str(body.get("message") or "").strip()
        session_key = str(body.get("session_key") or "echo")
        if not message:
            self._json(400, {"ok": False, "error": "empty_message"})
            return
        try:
            if _state["backend"] == "gadget":
                try:
                    payload = _run_gadget_turn(message, session_key)
                    if not payload.get("ok"):
                        raise RuntimeError(payload.get("error") or "gadget turn failed")
                except Exception as gadget_exc:  # noqa: BLE001
                    # If only the local shadow adapter died, restart it and retry once
                    # before paying the cost of lazy legacy initialization.
                    if not _gadget_shadow_ready():
                        try:
                            logger.warning("gadget shadow unavailable; restarting before fallback: %s", gadget_exc)
                            _ensure_gadget_shadow()
                            payload = _run_gadget_turn(message, session_key)
                            if not payload.get("ok"):
                                raise RuntimeError(payload.get("error") or "gadget retry failed")
                            payload["gadget_recovered"] = True
                        except Exception as retry_exc:  # noqa: BLE001
                            logger.warning("gadget retry failed, falling back to legacy: %s", retry_exc)
                            payload = _run_turn(message, session_key)
                            payload["fallback_from"] = "gadget"
                            payload["fallback_error"] = str(retry_exc)
                    else:
                        logger.warning("gadget backend failed, falling back to legacy: %s", gadget_exc)
                        payload = _run_turn(message, session_key)
                        payload["fallback_from"] = "gadget"
                        payload["fallback_error"] = str(gadget_exc)
            else:
                payload = _run_turn(message, session_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("turn failed")
            self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(200, payload)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--qsession-ttl", type=float, default=DEFAULT_TTL,
                    help="continuity TTL seconds (from end of last successful turn)")
    ap.add_argument("--qsession-max", type=int, default=2)
    ap.add_argument("--max-iterations", type=int, default=16)
    ap.add_argument("--mcp-servers", default="ha",
                    help="comma-separated MCP servers to connect for Echo turns ('' disables)")
    ap.add_argument("--exclude-toolsets",
                    default=("session_search,context_engine,skills,terminal,file,code_execution,"
                             "browser,computer_use,vision,image_gen,cronjob,delegation,connections,"
                             "todo,memory,tts,clarify"),
                    help="toolsets removed from the Echo question-turn surface (default: the "
                         "voice-focused surface keeps echo + ha + web)")
    ap.add_argument("--trace-log", default=os.path.expanduser("~/.hermes/echo_qsession_trace.log"))
    ap.add_argument("--simulate-tool-failure", default="",
                    help="TEST ONLY: comma-separated substrings of tools to make fail")
    ap.add_argument("--once", default="", help="run a single turn in-process and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    _state["trace_path"] = args.trace_log
    _state["legacy_config"] = {
        "mcp_servers": [s.strip() for s in args.mcp_servers.split(",") if s.strip()],
        "fail_prefixes": [s.strip() for s in args.simulate_tool_failure.split(",") if s.strip()],
        "max_iterations": args.max_iterations,
        "exclude_toolsets": {s.strip() for s in args.exclude_toolsets.split(",") if s.strip()},
        "qsession_ttl": args.qsession_ttl,
        "qsession_max": args.qsession_max,
    }

    if _state["backend"] == "gadget":
        _ensure_gadget_shadow()
        print("[ready] Gadget is primary; legacy Agent/MCP stack is deferred until fallback", flush=True)
    else:
        _ensure_legacy_backend()

    if args.once:
        if _state["backend"] == "gadget":
            payload = _run_gadget_turn(args.once, "echo")
        else:
            payload = _run_turn(args.once, "echo")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        mgr = _state.get("manager")
        if mgr is not None:
            mgr.shutdown()
        return

    httpd = ThreadingHTTPServer((args.host, args.port), _Handler)
    _server_ref["httpd"] = httpd
    print(f"[ready] Echo turn server on http://{args.host}:{args.port} "
          f"(qsession ttl={args.qsession_ttl}s)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mgr = _state.get("manager")
        if mgr is not None:
            mgr.shutdown()
        httpd.server_close()
        print("[stop] Echo legacy worker stopped", flush=True)


if __name__ == "__main__":
    main()

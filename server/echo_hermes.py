#!/usr/bin/env python3
"""Phase 5C — Hermes <-> Echo integration skeleton.

This module is the *runtime context* + *mode router* + *tool facade* layer that
sits between Hermes and the authoritative Phase 5B `EchoController`. It touches
no hardware: every action goes through the controller API.

Layers:
    EchoController                      (Phase 5B, authoritative state)
          ^
    EchoHermes tools                    (this file, tool semantics)
          ^
    render_runtime_context()            (this file, per-turn dynamic context)
          ^
    EchoRouter                          (this file, deterministic text router)

The Hermes plugin (`hermes-echo`) registers `echo_*` tools that call the admin
console (127.0.0.1:8794) which dispatches back into this module, and registers a
`pre_llm_call` hook that injects `render_runtime_context()` on every Echo turn.
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

DEVICE_ID = "echo_dot2_main"

# Mode (controller state) -> interaction_mode used in the context block.
_MODE_LOWER = {
    "IDLE": "idle",
    "LISTEN_ONE_SHOT": "listen_one_shot",
    "QUESTION_ACTIVE": "question_active",
    "LIVE_MODE": "live_mode",
    "LIVE_MUTED": "live_muted",
    "ERROR_RECOVERY": "error_recovery",
}
_SESSION_ACTIVE = {"listen_one_shot", "question_active", "live_mode", "live_muted"}
_WAKE_REQUIRED = {"idle", "listen_one_shot", "question_active", "error_recovery"}

# ── Echo-specific system prompt fragment (static, cache-safe) ─────────────────
ECHO_PROMPT_FRAGMENT = """\
You are interacting with the user through an Amazon Echo Dot (2nd gen) running the Hiyo
interface. Treat the "Echo Runtime Context" block attached to each turn as the authoritative
device state; never invent or cache Echo state. That rule is about device state (mute, volume,
mode) only: the values and answers from earlier turns of THIS conversation are valid context
and must be used, not denied.

One-shot / question mode (listen_one_shot or question_active):
- the user normally wakes the device with "OK Hiyo" or the Action button
- answer the current request concisely and naturally for spoken playback
- keep the spoken reply to at most 2-3 short sentences (about 60 Chinese
  characters or 40 English words); no lists, no markdown
- the interaction normally ends after the response

Smart-home / Home Assistant questions:
- answer them with the Home Assistant MCP tools directly (names beginning
  mcp__ha__, e.g. mcp__ha__ha_search to find an entity and
  mcp__ha__ha_call_read_tool to read its state).
- SPEED: mcp__ha__ha_search also scans automation/script/scene/helper configs,
  which can take ~1 min on a large install. Hermes already caps that scan for
  Echo turns (config_time_budget=1s), which is plenty for a device/entity
  lookup. Only if the user explicitly asks about automations/scenes and the
  results look truncated, retry with a larger config_time_budget (e.g. 15).
- SLOW actuators (covers/curtains, garage doors, long scene transitions): call
  mcp__ha__ha_call_write_tool with name "ha_call_service" and include "wait": false
  in arguments, so you can report "正在開/關" immediately instead of blocking ~100s.
  Use "wait": true only if the user explicitly wants confirmation that it finished.
- to CONTROL a device (open/close a cover, turn a light/switch on or off, set a
  temperature, run a scene) you MUST use the write path. This IS supported:
    1) if the entity_id is unknown, find it first with mcp__ha__ha_search
       (e.g. domain_filter "cover", query "窗簾");
    2) then call mcp__ha__ha_call_write_tool with
       {"name": "ha_call_service",
        "arguments": {"domain": "<domain>", "service": "<service>",
                      "entity_id": "<entity_id>"}}
       e.g. cover open: domain "cover", service "open_cover";
            cover close: service "close_cover";
            light/switch on/off: domain "light"/"switch", service "turn_on"/"turn_off".
    You MUST actually call the tool. Never reply that Home Assistant has no
    control tool before calling mcp__ha__ha_call_write_tool. If needed, discover
    write tools with mcp__ha__ha_search_tools (query e.g. "service call"); only
    report failure if the write call itself returns an error.
- recipe for a sensor reading: call mcp__ha__ha_search with domain_filter "sensor"
  and the QUANTITY only as the query (e.g. query "溫度"). Do NOT put the room name
  in the query -- the search AND-matches words, so "客廳 溫度" finds nothing. The
  room appears in each result's area / friendly_name; pick the sensor for the room
  (書房 or 客廳). Prefer the room's dedicated temperature/humidity sensor; do NOT use
  a device diagnostic (entity_id ending "_device_temperature") or a fan/AC/switch
  internal reading. Then call mcp__ha__ha_call_read_tool with name "ha_get_state" and
  arguments {"entity_id": "<id from the search>"}.
- do NOT read skill files, search past conversations, or shell out to scripts to
  answer a device question -- call the mcp__ha__ tools directly and answer.
- A conversation may span several separate one-shot questions, each with its own
  "OK Hiyo" wake. Resolve ellipsis/pronoun follow-ups such as "那客廳呢？" or
  "哪一間比較熱？" against the previous exchange: keep the SAME quantity the previous
  turn asked about and change only the room. Reuse values already returned by
  earlier tool calls instead of re-querying when the answer only needs a comparison.
- Do not ask a clarifying question when the ellipsis is obvious; just answer.
- When the follow-up only compares or judges values already returned earlier in this
  conversation (e.g. "哪一間比較熱？差多少？" or "差距算明顯嗎？"), answer directly
  from those values and call NO tool.
- Always reply in the user's language (Mandarin or English); never switch to another
  Chinese topolect.

Live Mode (live_mode):
- the user does not repeat the wake word between turns; the conversation is continuous
- the user may interrupt or ask to stop at any time
- physical controls and the EchoController always outrank conversational behaviour

If the user clearly asks to end Live Mode, call the echo_end_live_mode tool. Do not merely
claim that Live Mode ended.

Never override a physical microphone mute (live_muted). Never attempt to software-unmute a
physically muted Echo; there is no such tool by design.
"""

# ── Deterministic phrase sets (no LLM used to route) ──────────────────────────
_LIVE_PHRASES = (
    "live mode",
    "啟用對話模式",
    "開始對話模式",
    "進入對話模式",
    "進入 live mode",
)
_EXIT_PHRASES = (
    "退出對話模式",
    "結束對話模式",
    "結束 live mode",
    "回到待機",
    "停止對話",
)
_SOFT_EXIT_PHRASES = ("掰掰", "先這樣", "晚點再聊", "bye bye", "goodbye")
_QUESTION_MARKERS = (
    "?", "？", "什麼", "甚麼", "意思", "怎麼", "為何", "為什麼", "嗎", "呢",
    "what", "how", "why", "which", "who",
)
_REASON_MAP = {
    "user_requested": "user_requested",
    "idle_timeout": "idle_timeout",
    "physical_action": "physical_action",
    "remote_control": "remote_control",
    "error": "error",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class EchoRouter:
    """Deterministic text router for the post-wake utterance.

    This is intentionally keyword/rule based: no second wake-word model, no LLM.
    It will later receive STT transcript text after an OK-Hiyo wake.
    """

    ROUTE_LIVE = "live"
    ROUTE_EXIT = "exit"
    ROUTE_EXIT_CANDIDATE = "exit_candidate"
    ROUTE_QUESTION = "question"

    def __init__(self, allow_soft_exit: bool = False):
        self.allow_soft_exit = allow_soft_exit

    def _is_question(self, text: str) -> bool:
        return any(m in text for m in _QUESTION_MARKERS)

    def route(self, text: str, mode: str = "IDLE") -> dict:
        t = _norm(text)
        mode_l = mode if mode == mode.lower() else _MODE_LOWER.get(mode, mode.lower())
        if not t:
            return {"route": self.ROUTE_QUESTION, "reason": "empty", "matched": None}

        # Explicit exit commands (acted on only in live modes by the caller).
        for p in _EXIT_PHRASES:
            if p in t:
                return {"route": self.ROUTE_EXIT, "reason": "explicit_exit_phrase", "matched": p}

        # Soft / future semantic exits: recognised but not acted on by default.
        for p in _SOFT_EXIT_PHRASES:
            if p in t:
                return {
                    "route": self.ROUTE_EXIT_CANDIDATE,
                    "reason": "soft_exit_candidate",
                    "matched": p,
                    "note": "not acted on in Phase 5C unless allow_soft_exit=True",
                }

        # A question that merely mentions a live phrase is a question, not a command.
        if self._is_question(t):
            return {"route": self.ROUTE_QUESTION, "reason": "question_marker", "matched": None}

        for p in _LIVE_PHRASES:
            if p in t:
                return {"route": self.ROUTE_LIVE, "reason": "live_mode_phrase", "matched": p}

        return {"route": self.ROUTE_QUESTION, "reason": "default_question", "matched": None}


# ── Runtime context ───────────────────────────────────────────────────────────
def render_runtime_context(controller) -> str:
    state = controller.get_state()
    mode = _MODE_LOWER.get(state, state.lower())
    muted = bool(controller.is_mic_muted())
    wake_required = mode in _WAKE_REQUIRED
    lines = [
        "[Echo Runtime Context]",
        "interface: echo_dot2",
        f"device_id: {DEVICE_ID}",
        f"interaction_mode: {mode}",
        "audio_transport: full_duplex_tcp",
        "wake_word: OK Hiyo",
        f"wake_word_required: {'true' if wake_required else 'false'}",
        f"physical_mute: {'true' if muted else 'false'}",
        "can_control_echo: true",
        "output_device: echo_d23",
    ]
    return "\n".join(lines)


def runtime_context_dict(controller) -> dict:
    state = controller.get_state()
    mode = _MODE_LOWER.get(state, state.lower())
    muted = bool(controller.is_mic_muted())
    return {
        "interface": "echo_dot2",
        "device_id": DEVICE_ID,
        "interaction_mode": mode,
        "audio_transport": "full_duplex_tcp",
        "wake_word": "OK Hiyo",
        "wake_word_required": mode in _WAKE_REQUIRED,
        "physical_mute": muted,
        "can_control_echo": True,
        "output_device": "echo_d23",
        "session_active": mode in _SESSION_ACTIVE,
    }


class EchoHermes:
    """Tool facade + turn builder, backed by the authoritative EchoController."""

    def __init__(self, controller, log=None, router: Optional[EchoRouter] = None):
        self.ctl = controller
        self.router = router or EchoRouter()
        self._log = log or (lambda m: print(m, flush=True))

    # ---- logging -----------------------------------------------------------
    def _tool_log(self, name: str, ok: bool, detail: str = "", source: str = "hermes"):
        self._log(f"[tool] {name} source={source} ok={'true' if ok else 'false'}"
                  + (f" {detail}" if detail else ""))

    # ---- context / prompt ---------------------------------------------------
    def context(self) -> str:
        return render_runtime_context(self.ctl)

    def prompt(self) -> str:
        return ECHO_PROMPT_FRAGMENT

    def state(self) -> dict:
        ctx = runtime_context_dict(self.ctl)
        return {
            "device": DEVICE_ID,
            "mode": self.ctl.get_state(),
            "interaction_mode": ctx["interaction_mode"],
            "physical_mute": ctx["physical_mute"],
            "volume": self.ctl.volume,
            "wake_word_required": ctx["wake_word_required"],
            "session_active": ctx["session_active"],
        }

    # ---- tools --------------------------------------------------------------
    def echo_get_state(self) -> dict:
        st = self.state()
        self._tool_log("echo_get_state", True)
        return {"ok": True, **st}

    def echo_get_volume(self) -> dict:
        self._tool_log("echo_get_volume", True, f"volume={self.ctl.volume}")
        return {"ok": True, "device": DEVICE_ID, "volume": self.ctl.volume, "source": "controller"}

    def echo_enter_live_mode(self) -> dict:
        if self.ctl.is_mic_muted():
            self._tool_log("echo_enter_live_mode", False, "physical_mute")
            return {
                "ok": False,
                "error": "physical_mute",
                "message": "Echo is physically muted; Live Mode cannot be entered and there is "
                           "no software unmute.",
                "state": self.ctl.get_state(),
            }
        if self.ctl.get_state() == "LIVE_MODE":
            self._tool_log("echo_enter_live_mode", True, "idempotent")
            return {"ok": True, "idempotent": True, "state": self.ctl.get_state()}
        ok = self.ctl.enter_live_mode(reason="hermes_tool")
        self._tool_log("echo_enter_live_mode", ok, self.ctl.get_state())
        if ok:
            return {"ok": True, "state": self.ctl.get_state()}
        return {
            "ok": False,
            "error": "invalid_transition",
            "message": "Live Mode can only be entered from listen_one_shot or question_active.",
            "state": self.ctl.get_state(),
        }

    def echo_end_live_mode(self, reason: str = "remote_control") -> dict:
        mapped = _REASON_MAP.get(reason, "remote_control")
        state = self.ctl.get_state()
        if state == "LIVE_MODE":
            self.ctl.exit_live_mode(reason=f"{mapped} source=hermes_tool")
            self._tool_log("echo_end_live_mode", True, f"reason={mapped}")
            return {"ok": True, "state": self.ctl.get_state(), "reason": mapped}
        if state == "LIVE_MUTED":
            self.ctl.exit_live_mode(reason=f"{mapped} source=hermes_tool")
            self._tool_log("echo_end_live_mode", True, f"armed, reason={mapped}")
            return {"ok": True, "armed": True, "state": self.ctl.get_state(), "reason": mapped}
        self._tool_log("echo_end_live_mode", False, f"not_live:{state}")
        return {"ok": False, "error": "not_in_live_mode", "state": state}

    def echo_interrupt(self, reason: str = "remote_control") -> dict:
        state = self.ctl.get_state()
        ok = self.ctl.interrupt(reason=f"{reason} source=hermes_tool")
        self._tool_log("echo_interrupt", ok, state)
        if ok:
            return {"ok": True, "state": self.ctl.get_state(), "interrupted": state == "LIVE_MODE"}
        return {"ok": False, "error": "invalid_transition", "state": state}

    def echo_set_volume(self, value) -> dict:
        try:
            v = int(round(float(value)))
        except Exception:
            self._tool_log("echo_set_volume", False, f"bad_value:{value!r}")
            return {"ok": False, "error": "invalid_value", "value": value}
        v = max(0, min(100, v))
        old = self.ctl.volume
        self.ctl.set_volume(v, "hermes_tool")
        self._tool_log("echo_set_volume", True, f"old={old} new={v}")
        return {"ok": True, "device": DEVICE_ID, "volume": v, "old": old, "source": "hermes_tool"}

    def echo_play_chime(self, name: str = "listen") -> dict:
        valid = {"listen", "question", "live", "idle", "interrupt", "muted", "error"}
        if name not in valid:
            self._tool_log("echo_play_chime", False, f"unknown:{name}")
            return {"ok": False, "error": "unknown_chime", "valid": sorted(valid)}
        self.ctl.play_chime(name)
        self._tool_log("echo_play_chime", True, name)
        return {"ok": True, "chime": name}

    # ---- turn routing -------------------------------------------------------
    def route(self, text: str) -> dict:
        return self.router.route(text, self.ctl.get_state())

    def turn(self, text: str) -> dict:
        """Route a post-wake utterance and apply the deterministic controller action.

        Returns the injected runtime context + prompt fragment + route + action,
        i.e. exactly what a Hermes Echo turn would carry.
        """
        ctx_before = self.context()
        r = self.route(text)
        route = r["route"]
        action = None

        if route == EchoRouter.ROUTE_LIVE:
            action = self.echo_enter_live_mode()
        elif route == EchoRouter.ROUTE_EXIT:
            if self.ctl.get_state() in ("LIVE_MODE", "LIVE_MUTED"):
                action = self.echo_end_live_mode("user_requested")
            else:
                self._log("[router] exit phrase ignored outside live mode "
                          f"reason={r.get('reason')} state={self.ctl.get_state()}")
                action = {"ok": True, "ignored": True, "state": self.ctl.get_state()}
        elif route == EchoRouter.ROUTE_EXIT_CANDIDATE:
            # Phase 5C: log only, never close the session automatically.
            self._log(f"[router] soft-exit candidate matched={r.get('matched')!r} (no action)")
            action = {"ok": True, "ignored": True, "candidate": r.get("matched")}
        else:  # question
            if self.ctl.get_state() == "LISTEN_ONE_SHOT":
                self.ctl.enter_question_mode(reason="router_question")
            action = {"ok": True, "state": self.ctl.get_state()}

        return {
            "route": route,
            "route_reason": r.get("reason"),
            "matched": r.get("matched"),
            "action": action,
            "state_before": ctx_before,
            "context": self.context(),
            "context_dict": runtime_context_dict(self.ctl),
            "prompt": ECHO_PROMPT_FRAGMENT,
            "messages": self.build_turn_messages(text),
        }

    def build_turn_messages(self, text: str) -> list:
        """Hermes-ready message list with the per-turn context injected.

        The Echo prompt fragment belongs in the (cache-safe) system prompt via
        `register_system_prompt_section`; the dynamic context block is injected
        per turn (pre_llm_call appends it to the user message). This helper makes
        the contract explicit and is used by the standalone text harness.
        """
        return [
            {"role": "system", "content": ECHO_PROMPT_FRAGMENT},
            {
                "role": "user",
                "content": f"{render_runtime_context(self.ctl)}\n\n{text}",
            },
        ]


def dispatch_tool(controller, name: str, args: Optional[dict] = None) -> dict:
    """Dispatch an echo_* tool by name (used by the admin console + plugin)."""
    eh = EchoHermes(controller)
    a = args or {}
    if name == "echo_get_state":
        return eh.echo_get_state()
    if name == "echo_get_volume":
        return eh.echo_get_volume()
    if name == "echo_enter_live_mode":
        return eh.echo_enter_live_mode()
    if name == "echo_end_live_mode":
        return eh.echo_end_live_mode(a.get("reason", "remote_control"))
    if name == "echo_interrupt":
        return eh.echo_interrupt(a.get("reason", "remote_control"))
    if name == "echo_set_volume":
        return eh.echo_set_volume(a.get("value"))
    if name == "echo_play_chime":
        return eh.echo_play_chime(a.get("name", "listen"))
    return {"ok": False, "error": "unknown_tool", "tool": name}

#!/usr/bin/env python3
"""Phase 5C end-to-end context injection test (run on the server host).

Uses the *real* deployed Hermes plugin (loaded with a fake PluginContext) and the
*real* live EchoController admin console, to prove that a Hermes Echo turn
receives the correct `[Echo Runtime Context]` block for every interaction mode.
"""

import importlib.util
import json
import os
import socket
import sys

PLUGIN_DIR = os.path.expanduser("~/.hermes/plugins/hermes-echo")
HOST, PORT = "127.0.0.1", 8794
RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


class FakeCtx:
    def __init__(self):
        self.tools, self.sections, self.hooks = {}, [], {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_system_prompt_section(self, id, content, **kw):
        self.sections.append(id)

    def register_hook(self, name, cb):
        self.hooks[name] = cb


class Admin:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=5)
        self.f = self.s.makefile("rwb")
        self.f.readline()

    def raw(self, line):
        self.f.write((line + "\n").encode())
        self.f.flush()
        return self.f.readline().decode(errors="replace").strip()

    def state(self):
        return dict(kv.split("=", 1) for kv in self.raw("state").split())


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "hermes_echo_plugin", os.path.join(PLUGIN_DIR, "__init__.py"),
        submodule_search_locations=[PLUGIN_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_echo_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_plugin()
    ctx = FakeCtx()
    mod.register(ctx)
    hook = ctx.hooks["pre_llm_call"]
    a = Admin()

    def hook_mode():
        out = hook(platform="echo", session_id="s", user_message="hi", conversation_history=[])
        assert out and "context" in out, f"no injection: {out}"
        block = out["context"]
        for line in block.splitlines():
            if line.startswith("interaction_mode:"):
                return line.split(":", 1)[1].strip(), block
        return None, block

    # IDLE
    if a.state()["state"] != "IDLE":
        a.raw("action long")
    mode, block = hook_mode()
    check("Hermes Echo turn gets context in IDLE", mode == "idle", mode)
    check("IDLE block has wake_word_required true", "wake_word_required: true" in block)

    # LISTEN / QUESTION
    a.raw("wake wakeword")
    mode, _ = hook_mode()
    check("Hermes Echo turn gets context in LISTEN_ONE_SHOT", mode == "listen_one_shot", mode)
    a.raw("question")
    mode, block = hook_mode()
    check("Hermes Echo turn gets context in QUESTION_ACTIVE", mode == "question_active", mode)
    check("context reflects authoritative state at turn time",
          a.state()["state"] == "QUESTION_ACTIVE")

    # LIVE
    a.raw("action short")
    a.raw("wake wakeword")
    a.raw("live")
    mode, block = hook_mode()
    check("Hermes Echo turn gets context in LIVE_MODE", mode == "live_mode", mode)
    check("LIVE_MODE block has wake_word_required false", "wake_word_required: false" in block)

    # LIVE_MUTED
    a.raw("mute 1")
    mode, block = hook_mode()
    check("Hermes Echo turn gets context in LIVE_MUTED", mode == "live_muted", mode)
    check("LIVE_MUTED block has physical_mute true", "physical_mute: true" in block)
    a.raw("mute 0")

    # non-Echo turn receives nothing
    other = hook(platform="telegram", session_id="s", user_message="hi", conversation_history=[])
    check("non-Echo turn is not injected", other is None, str(other))

    npass = sum(1 for _, ok in RESULTS if ok)
    print(f"RESULT: {npass}/{len(RESULTS)} PASS")
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

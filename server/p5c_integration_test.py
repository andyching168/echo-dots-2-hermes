#!/usr/bin/env python3
"""Phase 5C integration test — drives the live EchoController admin console
(127.0.0.1:8794) and exercises runtime context, tools, router and mute precedence.
Run on the server host (Ubuntu)."""

import json
import socket
import sys

HOST, PORT = "127.0.0.1", 8794
RESULTS = []


class Admin:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=5)
        self.f = self.s.makefile("rwb")
        self.banner = self.f.readline().decode().strip()

    def raw(self, line):
        self.f.write((line + "\n").encode())
        self.f.flush()
        return self.f.readline().decode(errors="replace").strip()

    def state(self):
        r = self.raw("state")
        return dict(kv.split("=", 1) for kv in r.split())

    def j(self, line):
        r = self.raw(line)
        assert r.startswith("json "), f"not json: {r[:120]}"
        return json.loads(r[5:])

    def tool(self, name, args=None):
        return self.j(f"tool {name} {json.dumps(args or {})}")


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main():
    a = Admin()
    print("banner:", a.banner[:60], "...")

    # reset to IDLE
    st = a.state()
    if st["state"] != "IDLE":
        a.raw("action long")
    check("start IDLE", a.state()["state"] == "IDLE", a.state()["state"])

    # 1. context in IDLE
    c = a.j("context")
    check("IDLE context: mode/wake_required",
          c["context_dict"]["interaction_mode"] == "idle"
          and c["context_dict"]["wake_word_required"] is True, c["context"].replace("\n", " | ")[:90])

    # 2. wake -> LISTEN_ONE_SHOT context
    a.raw("wake wakeword")
    c = a.j("context")
    check("LISTEN_ONE_SHOT context after wake",
          a.state()["state"] == "LISTEN_ONE_SHOT"
          and c["context_dict"]["interaction_mode"] == "listen_one_shot", a.state()["state"])

    # 3. tool echo_get_state
    t = a.tool("echo_get_state")
    check("tool echo_get_state", t["ok"] and t["mode"] == "LISTEN_ONE_SHOT"
          and t["device"] == "echo_dot2_main", json.dumps(t)[:110])

    # 4. router question + turn
    r = a.j("route 今天天氣怎麼樣")
    check("router: 今天天氣怎麼樣 -> question", r["route"] == "question", str(r))
    turn = a.j("turn 今天天氣怎麼樣")
    check("turn question -> QUESTION_ACTIVE",
          turn["route"] == "question" and a.state()["state"] == "QUESTION_ACTIVE", a.state()["state"])

    # 5. back to IDLE, wake, then live command
    a.raw("action short")  # QUESTION_ACTIVE -> IDLE (cancel)
    a.raw("wake wakeword")
    r = a.j("route 啟用對話模式")
    check("router: 啟用對話模式 -> live", r["route"] == "live", str(r))
    turn = a.j("turn 啟用對話模式")
    check("turn live -> LIVE_MODE", turn["route"] == "live" and a.state()["state"] == "LIVE_MODE",
          a.state()["state"])
    c = a.j("context")
    check("LIVE_MODE context: wake_required false",
          c["context_dict"]["interaction_mode"] == "live_mode"
          and c["context_dict"]["wake_word_required"] is False, c["context"].replace("\n", " | ")[:90])

    # 6. negative router case
    r = a.j("route Live Mode 是什麼意思？")
    check("router: 'Live Mode 是什麼意思？' -> question", r["route"] == "question", str(r))

    # 7. tool end_live_mode
    t = a.tool("echo_end_live_mode", {"reason": "user_requested"})
    check("tool echo_end_live_mode -> IDLE", t["ok"] and a.state()["state"] == "IDLE",
          json.dumps(t)[:100])

    # 8. invalid tool transition from IDLE
    t = a.tool("echo_enter_live_mode")
    check("tool enter_live_mode from IDLE rejected",
          t["ok"] is False and t["error"] == "invalid_transition", json.dumps(t)[:100])

    # 9. tool set_volume
    t = a.tool("echo_set_volume", {"value": 40})
    check("tool echo_set_volume clamps/logs source=hermes_tool",
          t["ok"] and t["volume"] == 40 and t["source"] == "hermes_tool", json.dumps(t)[:100])

    # 10. tool get_volume + play_chime
    check("tool echo_get_volume", a.tool("echo_get_volume")["volume"] == 40)
    check("tool echo_play_chime", a.tool("echo_play_chime", {"name": "listen"})["ok"] is True)

    # 11. physical-mute precedence
    a.raw("mute 1")
    c = a.j("context")
    check("physical mute -> live_muted context",
          c["context_dict"]["interaction_mode"] == "live_muted"
          and c["context_dict"]["physical_mute"] is True, a.state()["state"])
    t = a.tool("echo_enter_live_mode")
    check("enter_live_mode rejected while physically muted",
          t["ok"] is False and t["error"] == "physical_mute", json.dumps(t)[:100])
    t = a.tool("echo_end_live_mode", {"reason": "remote_control"})
    check("end_live_mode while LIVE_MUTED arms exit (no unmute)",
          t["ok"] and t.get("armed") and a.state()["state"] == "LIVE_MUTED", json.dumps(t)[:100])
    a.raw("mute 0")
    check("after unmute -> IDLE", a.state()["state"] == "IDLE", a.state()["state"])

    # 12. exit phrase outside live mode must not corrupt
    a.raw("wake wakeword")
    a.raw("question")
    turn = a.j("turn 退出對話模式")
    check("explicit exit outside live mode ignored, no corruption",
          turn["action"].get("ignored") and a.state()["state"] == "QUESTION_ACTIVE",
          a.state()["state"])
    a.raw("action short")

    # 13. soft exit logged as candidate, no action
    a.raw("wake wakeword")
    a.raw("live")
    turn = a.j("turn 掰掰")
    check("soft exit is candidate-only", turn["route"] == "exit_candidate"
          and a.state()["state"] == "LIVE_MODE", f"route={turn['route']} state={a.state()['state']}")

    # 14. tool interrupt in live marks interrupted
    t = a.tool("echo_interrupt", {"reason": "user_requested"})
    check("tool echo_interrupt in LIVE_MODE", t["ok"], json.dumps(t)[:100])
    a.raw("action long")

    # 15. tool echo_unmute must not exist
    r = a.raw("tool echo_unmute {}")
    check("echo_unmute is not a tool", ("unknown_tool" in r) or r.startswith("err"), r[:80])

    # 16. exit-in-live via router ends live
    a.raw("wake wakeword")
    a.raw("live")
    turn = a.j("turn 退出對話模式")
    check("router exit phrase in LIVE_MODE ends live",
          turn["route"] == "exit" and a.state()["state"] == "IDLE", a.state()["state"])

    npass = sum(1 for _, ok in RESULTS if ok)
    print(f"RESULT: {npass}/{len(RESULTS)} PASS")
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

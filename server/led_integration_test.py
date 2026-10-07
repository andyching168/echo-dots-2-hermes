#!/usr/bin/env python3
"""Phase 5D.5 LED integration test — drives the live EchoController admin console
(127.0.0.1:8794) and asserts the semantic LED projection for every controller
state + sub-event.  Run on the server host (Ubuntu)."""

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

    def led(self):
        st = self.j("ledstatus")
        return st["led"] if "led" in st else st


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def main():
    a = Admin()
    print("banner:", a.banner[:60], "...")

    # normalize to IDLE
    st = a.state()
    if st["state"] != "IDLE":
        a.raw("action long")
    a.raw("led idle")

    check("IDLE -> led idle", a.led()["state"] == "idle", str(a.led()["state"]))

    a.raw("wake wakeword")
    check("LISTEN_ONE_SHOT -> led listening",
          a.state()["state"] == "LISTEN_ONE_SHOT" and a.led()["state"] == "listening",
          a.led()["state"])

    a.raw("question")
    check("QUESTION_ACTIVE -> led thinking",
          a.state()["state"] == "QUESTION_ACTIVE" and a.led()["state"] == "thinking",
          a.led()["state"])

    a.raw("ledevent generation_started")
    check("generation_started -> thinking", a.led()["state"] == "thinking")

    a.raw("ledevent playback_started")
    check("playback_started -> speaking", a.led()["state"] == "speaking", a.led()["state"])

    a.raw("ledevent playback_finished")
    check("playback_finished -> base thinking", a.led()["state"] == "thinking", a.led()["state"])

    a.raw("action short")
    check("action cancel -> idle", a.state()["state"] == "IDLE" and a.led()["state"] == "idle",
          a.led()["state"])

    a.raw("wake wakeword")
    a.raw("live")
    check("LIVE_MODE -> led live",
          a.state()["state"] == "LIVE_MODE" and a.led()["state"] == "live", a.led()["state"])

    a.raw("interrupt")
    check("interrupt in live keeps led live", a.led()["state"] == "live")
    a.raw("action long")

    a.raw("error")
    check("ERROR_RECOVERY -> led error",
          a.state()["state"] == "ERROR_RECOVERY" and a.led()["state"] == "error",
          a.led()["state"])
    a.raw("recover")
    check("recover -> led idle", a.led()["state"] == "idle")

    # physical mute precedence (software-asserted admin path)
    a.raw("wake wakeword")
    a.raw("mute 1")
    led = a.led()
    check("physical mute -> suspended/muted",
          led["suspended"] is True and led["state"] == "muted", str(led))
    a.raw("led listening")   # must not be able to force a render while muted
    led = a.led()
    check("cannot leave muted while physically muted",
          led["suspended"] is True and led["state"] == "muted", str(led))
    a.raw("mute 0")
    led = a.led()
    check("unmute -> renderer resumed", led["suspended"] is False, str(led))

    # LED_LIVE reservation is testable directly
    a.raw("led live")
    check("LED_LIVE testable via semantic API", a.led()["state"] == "live")
    a.raw("led idle")
    check("LED_IDLE via semantic API", a.led()["state"] == "idle")

    # explicit interrupt + error flashes
    r = a.raw("ledflash interrupt")
    check("ledflash interrupt ok", r.startswith("ok"))
    r = a.raw("ledflash error")
    check("ledflash error ok", r.startswith("ok"))

    # status has the required fields
    led = a.led()
    check("status fields", set(["state", "base_state", "suspended", "fps", "brightness"]) <= set(led),
          str(led))

    npass = sum(1 for _, ok in RESULTS if ok)
    print(f"RESULT: {npass}/{len(RESULTS)} PASS")
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 5B state-machine test via the local admin console (:8794).

Exercises the required deterministic sequences and invalid-transition
rejection. Run on the server host.
"""

import socket
import sys

HOST, PORT = "127.0.0.1", 8794


class Admin:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=5)
        self.f = self.s.makefile("r")

    def cmd(self, line):
        self.s.sendall((line + "\n").encode())
        return self.f.readline().strip()

    def state(self):
        r = self.cmd("state")
        # state=IDLE volume=73 muted=0 interrupted=0
        d = dict(kv.split("=", 1) for kv in r.split())
        return d["state"], d

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def main():
    a = Admin()
    # banner
    print("banner:", a.f.readline().strip())
    results = []

    def expect(label, cond, detail=""):
        results.append((label, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {label} {detail}")

    # 1. IDLE -> LISTEN_ONE_SHOT -> IDLE
    a.cmd("recover") if a.state()[0] == "ERROR_RECOVERY" else None
    expect("precondition IDLE", a.state()[0] == "IDLE", a.state()[0])
    a.cmd("wake test")
    expect("IDLE -> LISTEN_ONE_SHOT", a.state()[0] == "LISTEN_ONE_SHOT", a.state()[0])
    a.cmd("action short")
    expect("LISTEN_ONE_SHOT -> IDLE (action short)", a.state()[0] == "IDLE", a.state()[0])

    # 2. IDLE -> LISTEN_ONE_SHOT -> QUESTION_ACTIVE -> IDLE
    a.cmd("wake test")
    a.cmd("question")
    expect("-> QUESTION_ACTIVE", a.state()[0] == "QUESTION_ACTIVE", a.state()[0])
    a.cmd("action short")
    expect("QUESTION_ACTIVE -> IDLE (cancel)", a.state()[0] == "IDLE", a.state()[0])

    # 3. IDLE -> LISTEN_ONE_SHOT -> LIVE_MODE -> IDLE (action long)
    a.cmd("wake test")
    a.cmd("live")
    expect("-> LIVE_MODE", a.state()[0] == "LIVE_MODE", a.state()[0])
    a.cmd("action long")
    expect("LIVE_MODE -> IDLE (action long)", a.state()[0] == "IDLE", a.state()[0])

    # 4. LIVE_MODE -> LIVE_MUTED -> LIVE_MODE
    a.cmd("wake test")
    a.cmd("live")
    a.cmd("mute 1")
    expect("LIVE_MODE -> LIVE_MUTED", a.state()[0] == "LIVE_MUTED", a.state()[0])
    a.cmd("mute 0")
    expect("LIVE_MUTED -> LIVE_MODE", a.state()[0] == "LIVE_MODE", a.state()[0])
    a.cmd("exit test")  # live exit
    expect("LIVE_MODE -> IDLE (exit)", a.state()[0] == "IDLE", a.state()[0])

    # 5. invalid transitions rejected
    expect("invalid: question from IDLE rejected", a.cmd("question") == "ok=False", a.cmd("state"))
    a.cmd("wake test")
    a.cmd("live")
    expect("invalid: question from LIVE_MODE rejected", a.cmd("question") == "ok=False")
    expect("invalid: wake from LIVE_MODE rejected", a.cmd("wake test") == "ok=False")
    a.cmd("action long")
    expect("back to IDLE", a.state()[0] == "IDLE", a.state()[0])

    # 6. physical mute while LISTEN_ONE_SHOT -> LIVE_MUTED -> IDLE on unmute
    a.cmd("wake test")
    a.cmd("mute 1")
    expect("LISTEN_ONE_SHOT -> LIVE_MUTED", a.state()[0] == "LIVE_MUTED", a.state()[0])
    muted = a.state()[1].get("muted")
    expect("muted flag set", muted == "1", str(muted))
    a.cmd("mute 0")
    expect("LIVE_MUTED -> IDLE (was not LIVE_MODE)", a.state()[0] == "IDLE", a.state()[0])

    a.close()
    npass = sum(1 for _, ok in results if ok)
    print(f"RESULT: {npass}/{len(results)} PASS")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

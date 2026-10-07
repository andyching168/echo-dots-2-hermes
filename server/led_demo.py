#!/usr/bin/env python3
"""Phase 5D.5 — LED showcase (test mode) for visual appreciation.

Cycles the real semantic states plus the rotating-rainbow showcase so the whole
ring can be seen on the device.  Run on the server host:

    python3 led_demo.py

Requires the LED server (`echo_server.py --control`) and the `echo_control`
daemon on the Echo.
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 8794


class Admin:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=5)
        self.f = self.s.makefile("rwb")
        self.f.readline()

    def cmd(self, line):
        self.f.write((line + "\n").encode())
        self.f.flush()
        return self.f.readline().decode(errors="replace").strip()


def show(a, label, cmds, dwell):
    print(f"  {label}")
    for c in cmds:
        a.cmd(c)
    time.sleep(dwell)


def main():
    a = Admin()
    a.cmd("led idle")
    print("LED showcase (watch the ring) ...")
    show(a, "rainbow comet (showcase)", ["leddemo 10"], 10)
    show(a, "IDLE (off)", ["led idle"], 1.0)
    show(a, "LISTENING (cyan pulse)", ["led listening"], 4)
    show(a, "THINKING (blue-purple pulse + rotating)", ["led thinking"], 4)
    show(a, "SPEAKING (bright cyan breathe + chase)", ["led speaking"], 5)
    show(a, "LIVE (dim purple, low attention)", ["led live"], 5)
    show(a, "ERROR (amber pulse)", ["led error"], 3)
    print("  INTERRUPT (white flash x3)")
    for _ in range(3):
        a.cmd("ledflash interrupt")
        time.sleep(0.7)
    show(a, "rainbow comet (showcase) again", ["leddemo 8"], 8)
    show(a, "back to IDLE (off)", ["led idle"], 1)
    a.cmd("led idle")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

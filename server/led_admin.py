#!/usr/bin/env python3
"""Send one or more commands to the EchoController admin console (127.0.0.1:8794).

Usage:  python3 led_admin.py "led listening" "state" ...
"""
import socket
import sys

HOST, PORT = "127.0.0.1", 8794


def main():
    s = socket.create_connection((HOST, PORT), timeout=5)
    f = s.makefile("rwb")
    f.readline()  # banner
    for line in sys.argv[1:]:
        f.write((line + "\n").encode())
        f.flush()
        resp = f.readline().decode(errors="replace").strip()
        print(f"{line!r} -> {resp}")
    s.close()


if __name__ == "__main__":
    main()

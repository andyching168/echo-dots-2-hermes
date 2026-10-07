#!/usr/bin/env python3
"""Headless runner for the Phase 5B EchoController admin console on 127.0.0.1:8794.

Starts ONLY the text admin console (the channel hermes-echo tools/hook use).
No audio transport, no hardware. Temporary test aid for Phase 5D proof.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.expanduser("~/voice/echo-bridge"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import echo_controller  # noqa: E402


async def main() -> None:
    controller = echo_controller.EchoController()
    server = await asyncio.start_server(
        lambda r, w: echo_controller.handle_admin(r, w, controller),
        "127.0.0.1", 8794,
    )
    print("[admin] EchoController admin console on 127.0.0.1:8794", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

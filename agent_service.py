"""Persistent local companion for the Chrome application side panel.

This process owns only the loopback bridge and local SQLite-backed planner. It
lets the extension run without keeping the Streamlit dashboard open. It never
opens Chrome or exposes the service beyond 127.0.0.1.
"""

from __future__ import annotations

import argparse
import signal
import threading

from extension_bridge import start_bridge_server


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Application Autopilot companion service.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    bridge = start_bridge_server(args.port)
    if bridge.bridge is None:
        print(f"A compatible local application bridge is already running on 127.0.0.1:{args.port}.")
        return 0

    stopped = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"Application Autopilot companion is ready at http://127.0.0.1:{args.port}.")
    try:
        stopped.wait()
    finally:
        if bridge.server is not None:
            bridge.server.should_exit = True
        if bridge.thread is not None:
            bridge.thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

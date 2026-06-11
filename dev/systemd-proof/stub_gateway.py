#!/usr/bin/env python3
"""Stub OpenClaw gateway for the systemd watchdog/reattach proof harness.

Stands in for the real ``openclaw gateway run`` under
``tinyhat-openclaw-gateway.service`` so the supervisor-systemd
behaviours (#685: watchdog wedge, reattach continuity, no-early-healthy)
can be exercised without an OpenRouter key or a real model. It is NOT
the product gateway — it only does what the supervisor's local health
probe inspects:

- binds the loopback gateway port (the supervisor's port probe), and
- emits the ``[gateway] ready`` marker the supervisor greps for, AFTER
  an optional readiness delay so the no-early-healthy demo can hold the
  gateway active-but-not-ready.

Readiness is gated by a marker file (``--ready-file``): while it is
absent the gateway stays active-but-not-ready (port open, no ready
line). ``touch`` it to make the gateway report ready; the supervisor
then sees readiness and the runtime may go healthy.
"""

from __future__ import annotations

import argparse
import http.server
import os
import sys
import threading
import time


def _log(message: str) -> None:
    # Stdout is journald under the systemd unit; the supervisor greps
    # the unit journal for the ready marker.
    print(message, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--ready-file", default="")
    parser.add_argument(
        "--ready-after",
        type=float,
        default=0.0,
        help="Seconds to wait before reporting ready (0 = immediate).",
    )
    args = parser.parse_args()

    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):  # silence per-request noise
            return

    server = http.server.HTTPServer(("127.0.0.1", args.port), _Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _log(f"[gateway] listening on 127.0.0.1:{args.port}")

    announced = False

    def _ready_now() -> bool:
        if args.ready_file:
            return os.path.exists(args.ready_file)
        return time.monotonic() >= start + args.ready_after

    start = time.monotonic()
    while True:
        if not announced and _ready_now():
            _log("[gateway] ready")
            _log("[telegram] connected to gateway")
            announced = True
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())

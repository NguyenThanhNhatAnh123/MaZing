"""Loopback-only production WSGI entry point behind Cloudflare Tunnel."""
from __future__ import annotations

import argparse
import logging
import sys

from waitress import serve

import config
from dashboard import _acquire_dashboard_lock, app
from lib import mayzing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT)
    parser.add_argument("--threads", type=int, default=32)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.threads < 4:
        parser.error("Use a valid TCP port and at least four worker threads.")
    config.ensure_dirs()
    lock = _acquire_dashboard_lock(args.port)
    if lock is None:
        raise SystemExit(f"Another dashboard already owns port {args.port}.")
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_DIR / "production.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    try:
        # Forwarded headers deliberately remain untrusted. Application origin and
        # tenant checks use explicit allowlists rather than arbitrary proxy input.
        serve(
            app,
            host="127.0.0.1",
            port=args.port,
            threads=args.threads,
            channel_timeout=180,
            max_request_body_size=app.config["MAX_CONTENT_LENGTH"],
            ident="MaZing",
        )
    finally:
        mayzing.shutdown()
        lock.close()


if __name__ == "__main__":
    main()

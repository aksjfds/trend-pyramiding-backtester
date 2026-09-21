"""Container lifecycle helpers. No exchange connections or trading operations."""

from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import threading
import time
from pathlib import Path


def require_persistent_state(path: Path):
    if os.environ.get("PYRAMID_REQUIRE_PERSISTENT_STATE", "false").lower() != "true":
        return
    mount = Path(os.environ.get("PYRAMID_STATE_MOUNT", "/data")).resolve()
    if not mount.is_mount() or not path.resolve().is_relative_to(mount):
        raise ValueError(
            "trading requires a persistent volume at PYRAMID_STATE_MOUNT containing the state directory"
        )


def heartbeat_path() -> Path:
    return Path(os.environ.get("PYRAMID_HEARTBEAT_FILE", "/tmp/pyramid-heartbeat.json"))


class ProcessControl:
    def __init__(self):
        self.stop = threading.Event()
        self.handlers = {}

    def __enter__(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self.request_stop)
        self.update("starting")
        return self

    def request_stop(self, signum, frame):
        # Do not interrupt a request between journaling, filling and stop verification.
        self.stop.set()

    def update(self, phase: str):
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".heartbeat-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"pid": os.getpid(), "updated_at": time.time(), "phase": phase}, handle)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def __exit__(self, *exc):
        try:
            self.update("stopped")
        finally:
            for signum, handler in self.handlers.items():
                signal.signal(signum, handler)


def health_main():
    parser = argparse.ArgumentParser(prog="pyramid-health")
    parser.add_argument("--ready", action="store_true", help="also require successful work/checks")
    parser.add_argument("--max-age", type=float, default=600)
    args = parser.parse_args()
    try:
        status = json.loads(heartbeat_path().read_text())
        age = time.time() - status["updated_at"]
        os.kill(status["pid"], 0)
        healthy = (
            0 <= age <= args.max_age
            and status["phase"] != "stopped"
            and (not args.ready or status["phase"] == "ready")
        )
    except (OSError, ValueError, KeyError, TypeError):
        healthy = False
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    health_main()

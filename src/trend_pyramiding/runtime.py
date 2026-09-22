"""Graceful process shutdown and local status heartbeat."""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
import time
from pathlib import Path


def safe_console_print(*args, **kwargs):
    """Best-effort console output that must never stop trading."""
    try:
        print(*args, **kwargs)
    except (BrokenPipeError, OSError, ValueError):
        pass


def heartbeat_path() -> Path:
    return Path(os.environ.get("PYRAMID_HEARTBEAT_FILE", "state/okx-heartbeat.json"))


class ProcessControl:
    def __init__(self):
        self.stop = threading.Event()
        self.handlers = {}
        self.parent_watch = None

    def __enter__(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self.request_stop)
        self.update("starting")
        parent = os.environ.get("PYRAMID_PARENT_PID")
        if parent:
            expected = int(parent)

            def watch_parent():
                while not self.stop.is_set():
                    if os.getppid() != expected:
                        self.stop.set()
                        break
                    self.stop.wait(1)

            self.parent_watch = threading.Thread(target=watch_parent, daemon=True)
            self.parent_watch.start()
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
        self.stop.set()
        if self.parent_watch:
            self.parent_watch.join(timeout=2)
        try:
            self.update("stopped")
        finally:
            for signum, handler in self.handlers.items():
                signal.signal(signum, handler)

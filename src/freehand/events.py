"""Optional, append-only JSONL recording. Times are relative to each session."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path


class EventLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.path = None
        self.stream = None
        self.start_ns = 0
        self.session = ""

    def configure(self, path: Path | None):
        if path == self.path:
            return
        self.record("end")
        self.close()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            logging.getLogger("freehand").error("event log unavailable: %s", exc)
            return
        self.path = path
        self.start_ns = time.monotonic_ns()
        self.session = uuid.uuid4().hex
        self.record("session")
        logging.getLogger("freehand").addHandler(self)

    def record(self, kind: str, **data):
        if self.stream is None:
            return
        row = dict(version=1, session=self.session, t_ms=(time.monotonic_ns() - self.start_ns) / 1e6,
                   unix_ms=time.time_ns() / 1e6, kind=kind, **data)
        try:
            self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            # Recording must never interrupt input handling (e.g. disk full).
            self.close()
            logging.getLogger("freehand").error("event log write failed; recording disabled")

    def emit(self, record):
        self.record("message", level=record.levelname, message=record.getMessage())

    def close(self):
        logging.getLogger("freehand").removeHandler(self)
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        self.stream = None
        self.path = None
        super().close()

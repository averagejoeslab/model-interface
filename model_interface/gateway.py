"""The gateway: one model call at a time, watched and stoppable.

A Run is one call. The Gateway streams it from the provider and, on every event, checks the
stop list and updates the run's record. Stopping closes the upstream connection, which makes the
provider stop generating, and ends the caller's stream cleanly with stop_reason "interrupted".
"""
import collections
import datetime
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .providers import Model


@dataclass
class Run:
    session: str
    key: str                         # the caller's key, by name
    model: str                       # the alias
    format: str                      # the caller's wire format
    stream: bool                     # whether the caller asked for a stream
    id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    started_at: float = field(default_factory=time.time)
    first_event_at: float | None = None
    ended_at: float | None = None
    events: int = 0
    usage: dict = field(default_factory=dict)
    stop_reason: str | None = None
    interrupted: bool = False
    error: str | None = None

    @property
    def scopes(self) -> set:
        return {self.id, self.session, self.key, "*"}

    def record(self) -> dict:
        ms = lambda t: round((t - self.started_at) * 1000) if t else None
        return {"run": self.id, "session": self.session, "key": self.key, "model": self.model, "format": self.format,
                "stream": self.stream, "started": datetime.datetime.fromtimestamp(self.started_at).isoformat(timespec="milliseconds"),
                "first_event_ms": ms(self.first_event_at), "duration_ms": ms(self.ended_at), "events": self.events,
                "usage": self.usage, "stop_reason": self.stop_reason, "interrupted": self.interrupted, "error": self.error}


class Stops:
    """Scopes that are stopped: a run id, a session, a key name, or "*" for everything."""

    def __init__(self):
        self.scopes = set()

    def add(self, scope: str):
        self.scopes.add(scope)

    def remove(self, scope: str):
        self.scopes.discard(scope)

    def stopped(self, run: Run) -> bool:
        return bool(self.scopes & run.scopes)


class Record:
    """One line per run, appended to a file, with the recent ones kept in memory."""

    def __init__(self, path: str | None, keep: int = 1000):
        self.path = os.path.expanduser(path) if path else None
        self.recent = collections.deque(maxlen=keep)
        self.lock = threading.Lock()

    def write(self, run: Run):
        line = run.record()
        with self.lock:
            self.recent.append(line)
            if self.path:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(line) + "\n")

    def runs(self, session: str | None = None, limit: int = 50) -> list:
        lines = [r for r in self.recent if session is None or r["session"] == session]
        return lines[-limit:][::-1]


class Gateway:
    def __init__(self, models: dict[str, Model], stops: Stops | None = None, record: Record | None = None):
        self.models = models
        self.stops = stops or Stops()
        self.record = record or Record(None)

    def stream(self, run: Run, request: dict):
        """The run's canonical events, watched. Ends early, cleanly, if the run is stopped."""
        upstream = self.models[run.model].stream(request)
        started, open_block = False, None
        try:
            for event in upstream:
                if self.stops.stopped(run):
                    run.interrupted = True
                    yield from self._cut(run, started, open_block)
                    return
                t = event["type"]
                run.events += 1
                if run.first_event_at is None:
                    run.first_event_at = time.time()
                if t == "message_start":
                    started = True
                    run.usage.update(event["message"].get("usage", {}))
                elif t == "content_block_start":
                    open_block = event["index"]
                elif t == "content_block_stop":
                    open_block = None
                elif t == "message_delta":
                    run.stop_reason = event["delta"].get("stop_reason")
                    run.usage.update(event.get("usage", {}))
                yield event
        except Exception as e:
            detail = e.response.text if isinstance(e, httpx.HTTPStatusError) else str(e)
            run.error = f"{type(e).__name__}: {detail}"
            yield {"type": "error", "error": {"type": "upstream_error", "message": detail}}
        finally:
            upstream.close()  # the provider's connection goes with it, and so does its generation
            run.ended_at = time.time()
            self.record.write(run)

    def _cut(self, run: Run, started: bool, open_block):
        """The events that end a stream early, so every client sees a whole message."""
        run.stop_reason = "interrupted"
        if not started:
            yield {"type": "message_start", "message": {"id": run.id, "type": "message", "role": "assistant", "model": run.model,
                                                        "content": [], "stop_reason": None, "stop_sequence": None,
                                                        "usage": {"input_tokens": 0, "output_tokens": 0}}}
        if open_block is not None:
            yield {"type": "content_block_stop", "index": open_block}
        yield {"type": "message_delta", "delta": {"stop_reason": "interrupted", "stop_sequence": None},
               "usage": {"output_tokens": run.usage.get("output_tokens", 0)}}
        yield {"type": "message_stop"}

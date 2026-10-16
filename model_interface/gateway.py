"""The gateway: one streaming call at a time, passed through and recorded block by block.

The record is callers, sessions, streams, blocks. A stream is one request and the response that
streams back for it. Every level has an id, a start and an end. The record is live: a watcher sees a
block the moment it opens, each delta as it lands, and the end time when it closes.
"""
import collections
import datetime
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from .formats import Format
from .providers import Model


def iso(t: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def stamped(d: dict) -> dict:
    """A block or any record, with its times as ISO strings."""
    return {k: iso(v) if k in ("started", "ended") and isinstance(v, (int, float)) else v for k, v in d.items()}


@dataclass
class Stream:
    session: str
    key: str                               # the caller, by name
    model: str                             # the alias
    format: str
    request: dict                          # the body as received
    id: str = field(default_factory=lambda: f"str_{uuid.uuid4().hex[:12]}")
    started: float = field(default_factory=time.time)
    ended: float | None = None
    message: dict = field(default_factory=dict)    # the response's id, model and role, from its first line
    blocks: list = field(default_factory=list)
    stop_reason: str | None = None
    finish_reason: str | None = None       # Chat Completions' own word for it, when that is the format
    usage: dict = field(default_factory=dict)
    error: str | None = None

    def snapshot(self, blocks: bool = True, request: bool = True) -> dict:
        out = {"id": self.id, "session": self.session, "key": self.key, "model": self.model, "format": self.format,
               "started": iso(self.started), "ended": iso(self.ended), "message": self.message}
        if request:
            out["request"] = self.request
        if blocks:
            out["blocks"] = [stamped(b) for b in self.blocks]
        out.update({"stop_reason": self.stop_reason, "usage": self.usage, "error": self.error})
        if self.finish_reason:
            out["finish_reason"] = self.finish_reason
        return out


@dataclass
class Session:
    id: str
    key: str
    started: float
    streams: list = field(default_factory=list)

    @property
    def ended(self) -> float | None:
        return self.streams[-1].ended if self.streams and all(s.ended for s in self.streams) else None

    def summary(self) -> dict:
        return {"session": self.id, "key": self.key, "started": iso(self.started), "ended": iso(self.ended), "streams": len(self.streams)}

    def snapshot(self, blocks: bool = True) -> dict:
        return {**self.summary(), "streams": [s.snapshot(blocks) for s in self.streams]}


class Record:
    """Callers, sessions, streams and blocks, live in memory, each finished stream appended to a file, with watchers."""

    def __init__(self, path: str | None, keep: int = 2000):
        self.path = os.path.expanduser(path) if path else None
        self.sessions: dict[str, Session] = collections.OrderedDict()
        self.streams: dict[str, Stream] = collections.OrderedDict()
        self.keep = keep
        self.lock = threading.Lock()
        self.watchers: dict[str, list[queue.Queue]] = collections.defaultdict(list)   # by session, and "*" for everything

    # ── writing ──────────────────────────────────────────────────────────────

    def open(self, stream: Stream):
        with self.lock:
            session = self.sessions.get(stream.session)
            if session is None:
                session = self.sessions[stream.session] = Session(stream.session, stream.key, stream.started)
            session.streams.append(stream)
            self.streams[stream.id] = stream
            while len(self.streams) > self.keep:
                _, old = self.streams.popitem(last=False)
                s = self.sessions.get(old.session)
                if s and old in s.streams:
                    s.streams.remove(old)
                    if not s.streams:
                        self.sessions.pop(old.session, None)
        self.tell(stream, "stream.started", snapshot=stream.snapshot(blocks=False))

    def close(self, stream: Stream):
        self.tell(stream, "stream.ended", snapshot=stream.snapshot(blocks=False, request=False))
        if self.path:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(stream.snapshot()) + "\n")

    def tell(self, source: Stream, kind: str, **payload):
        """One record event, to everyone watching the source's session, and to everyone watching everything.

        Every event names the session, the caller and the stream by id; the payload comes under its own keys.
        """
        event = {"type": kind, "at": iso(time.time()), "session": source.session, "key": source.key, "stream": source.id,
                 **{k: stamped(v) if isinstance(v, dict) else v for k, v in payload.items()}}
        for q in list(self.watchers.get(source.session, [])) + list(self.watchers.get("*", [])):
            q.put(event)

    # ── watching ─────────────────────────────────────────────────────────────

    def watch(self, scope: str = "*") -> queue.Queue:
        q = queue.Queue()
        self.watchers[scope].append(q)
        return q

    def unwatch(self, scope: str, q: queue.Queue):
        if q in self.watchers.get(scope, []):
            self.watchers[scope].remove(q)

    # ── reading ──────────────────────────────────────────────────────────────

    def callers(self) -> list:
        by = collections.OrderedDict()
        for s in self.sessions.values():
            c = by.setdefault(s.key, {"caller": s.key, "sessions": 0, "streams": 0, "last": None})
            c["sessions"] += 1
            c["streams"] += len(s.streams)
            c["last"] = max(c["last"] or "", iso(s.streams[-1].started) if s.streams else "")
        return sorted(by.values(), key=lambda c: c["last"] or "", reverse=True)

    def list(self) -> list:
        return [s.summary() for s in reversed(self.sessions.values())]

    def session(self, id: str) -> Session | None:
        return self.sessions.get(id)

    def stream(self, id: str) -> Stream | None:
        return self.streams.get(id)


class Gateway:
    def __init__(self, models: dict[str, Model], record: Record | None = None):
        self.models = models
        self.record = record or Record(None)

    def stream(self, stream: Stream, headers: dict):
        """The provider's stream, line by line, verbatim, read into the record as it passes."""
        model = self.models[stream.model]
        format: Format = model.provider.format
        self.record.open(stream)
        reading = format.reading(stream, lambda kind, **payload: self.record.tell(stream, kind, **payload))
        sent = 0
        try:
            with format.open(model.provider.client, {**stream.request, "model": model.name}, headers) as response:
                for line in response.iter_lines():
                    sent += 1
                    reading.feed(line)
                    yield line + "\n"
        except Exception as e:
            stream.error = f"{type(e).__name__}: {e}"
            if sent == 0:
                raise                                  # before anything was sent: the server answers with the provider's status
        finally:                                       # leaving the with-block hangs up on the provider, whoever ended the stream
            reading.close_block()
            stream.ended = time.time()
            self.record.close(stream)

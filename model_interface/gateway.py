"""The gateway: one streaming call at a time, passed through, recorded block by block, and under the killswitch.

The record is sessions of streams of blocks. A stream is one request and the response that streams
back for it. Every level has an id, a start and an end. The record is live: a watcher sees a block
the moment it opens, each delta as it lands, and the end time when it closes.
"""
import collections
import datetime
import json
import os
import queue
import socket
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
    key: str                               # the caller's key, by name
    model: str                             # the alias
    format: str
    request: dict                          # the body as received
    id: str = field(default_factory=lambda: f"str_{uuid.uuid4().hex[:12]}")
    started: float = field(default_factory=time.time)
    ended: float | None = None
    message: dict = field(default_factory=dict)    # id, model, role, from the stream's first line
    blocks: list = field(default_factory=list)
    stop_reason: str | None = None
    finish_reason: str | None = None       # Chat Completions' own word for it, when that is the format
    usage: dict = field(default_factory=dict)
    interrupted: bool = False
    cut: dict | None = None                # who cut it, when, and in which block
    error: str | None = None

    def snapshot(self, blocks: bool = True) -> dict:
        out = {"id": self.id, "session": self.session, "key": self.key, "model": self.model, "format": self.format,
               "started": iso(self.started), "ended": iso(self.ended), "request": self.request, "message": self.message}
        if blocks:
            out["blocks"] = [stamped(b) for b in self.blocks]
        out.update({"stop_reason": self.stop_reason, "usage": self.usage, "interrupted": self.interrupted, "cut": self.cut, "error": self.error})
        if self.finish_reason:
            out["finish_reason"] = self.finish_reason
        return out


@dataclass
class Session:
    id: str
    started: float
    streams: list = field(default_factory=list)
    closed: float | None = None

    @property
    def ended(self) -> float | None:
        if self.closed:
            return self.closed
        last = self.streams[-1] if self.streams else None
        return last.ended if last and all(s.ended for s in self.streams) else None

    def snapshot(self, blocks: bool = True) -> dict:
        return {"session": self.id, "started": iso(self.started), "ended": iso(self.ended), "streams": [s.snapshot(blocks) for s in self.streams]}


class Killswitch:
    """Flags: one per session, and one for everything. On means cut and refuse; off means run."""

    def __init__(self):
        self.all = False
        self.sessions = set()

    def set(self, on: bool, session: str | None = None):
        if session is None:
            self.all = on
        elif on:
            self.sessions.add(session)
        else:
            self.sessions.discard(session)

    def is_on(self, session: str) -> bool:
        return self.all or session in self.sessions

    def state(self) -> dict:
        return {"all": self.all, "sessions": sorted(self.sessions)}


class Record:
    """Sessions of streams of blocks, live in memory, each finished stream appended to a file, with watchers."""

    def __init__(self, path: str | None, keep: int = 2000):
        self.path = os.path.expanduser(path) if path else None
        self.sessions: dict[str, Session] = collections.OrderedDict()
        self.streams: dict[str, Stream] = collections.OrderedDict()
        self.keep = keep
        self.lock = threading.Lock()
        self.watchers: dict[str, list[queue.Queue]] = collections.defaultdict(list)

    def open(self, stream: Stream):
        with self.lock:
            session = self.sessions.get(stream.session)
            if session is None:
                session = self.sessions[stream.session] = Session(stream.session, stream.started)
            session.streams.append(stream)
            self.streams[stream.id] = stream
            while len(self.streams) > self.keep:
                old_id, old = self.streams.popitem(last=False)
                s = self.sessions.get(old.session)
                if s and old in s.streams:
                    s.streams.remove(old)
                    if not s.streams:
                        self.sessions.pop(old.session, None)
        self.tell(stream, "stream.started", stream=stream.snapshot(blocks=False))

    def close(self, stream: Stream):
        self.tell(stream, "stream.ended", stream={k: v for k, v in stream.snapshot(blocks=False).items() if k != "request"})
        if self.path:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(stream.snapshot()) + "\n")

    def tell(self, source: Stream, kind: str, **payload):
        """One record event, to everyone watching the source's session."""
        event = {"type": kind, "at": iso(time.time()), "session": source.session, "stream": source.id,
                 **{k: stamped(v) if isinstance(v, dict) else v for k, v in payload.items()}}
        for q in list(self.watchers.get(source.session, [])) + list(self.watchers.get("*", [])):
            q.put(event)

    def watch(self, session: str) -> queue.Queue:
        q = queue.Queue()
        self.watchers[session].append(q)
        return q

    def unwatch(self, session: str, q: queue.Queue):
        if q in self.watchers.get(session, []):
            self.watchers[session].remove(q)

    def list(self) -> list:
        return [{"session": s.id, "started": iso(s.started), "ended": iso(s.ended), "streams": len(s.streams),
                 "interrupted": sum(x.interrupted for x in s.streams)} for s in reversed(self.sessions.values())]

    def session(self, id: str) -> Session | None:
        return self.sessions.get(id)

    def stream(self, id: str) -> Stream | None:
        return self.streams.get(id)


class Gateway:
    POLL = 0.1          # how often the killswitch is checked while the provider is silent
    END = object()      # what the reader puts on the queue when the stream is over

    def __init__(self, models: dict[str, Model], killswitch: Killswitch | None = None, record: Record | None = None):
        self.models = models
        self.killswitch = killswitch or Killswitch()
        self.record = record or Record(None)

    def stream(self, stream: Stream, headers: dict):
        """The provider's stream, line by line, verbatim, read into the record as it passes. Cut within POLL seconds of the switch."""
        model = self.models[stream.model]
        format: Format = model.provider.format
        self.record.open(stream)
        reading = format.reading(stream, lambda kind, **payload: self.record.tell(stream, kind, **payload))
        lines: queue.Queue = queue.Queue()
        opened = {}                                    # the entered response, once the reader has it, so a cut can hang up

        def read():                                    # the provider's lines, on their own thread, so a silence can't hold the switch
            try:
                with format.open(model.provider.client, {**stream.request, "model": model.name}, headers) as response:
                    opened["response"] = response
                    for line in response.iter_lines():
                        lines.put(line)
            except BaseException as e:
                lines.put(e)
            lines.put(self.END)

        threading.Thread(target=read, daemon=True).start()
        sent = 0
        try:
            while True:
                try:
                    item = lines.get(timeout=self.POLL)
                except queue.Empty:
                    item = None                        # nothing arrived: check the switch anyway
                if self.killswitch.is_on(stream.session):
                    stream.interrupted = True
                    stream.cut = {"by": "killswitch", "at": iso(time.time()),
                                  "block": {"index": reading.open["index"], "type": reading.open["type"]} if reading.open else None}
                    yield from format.closing(reading)
                    reading.stop("interrupted")
                    return
                if item is None:
                    continue
                if item is self.END:
                    return
                if isinstance(item, BaseException):
                    raise item
                sent += 1
                reading.feed(item)
                yield item + "\n"
        except Exception as e:
            stream.error = f"{type(e).__name__}: {e}"
            if sent == 0:
                raise                                  # before anything was sent: the server answers with the provider's status
            yield format.error_line(str(e))
        finally:
            self._hang_up(opened.get("response"))      # the provider's connection goes, and so does its generation
            reading.close_block()
            stream.ended = time.time()
            self.record.close(stream)

    @staticmethod
    def _hang_up(response):
        """Close the provider's connection from outside the reader's thread: shut the socket down, then close."""
        if response is None:
            return
        http = getattr(response, "http_response", response)
        sock = http
        for _ in range(10):
            step = next((a for a in ("_sock", "_network_stream", "_connection", "_httpcore_stream", "_stream", "stream") if hasattr(sock, a)), None)
            if step is None or isinstance(sock, socket.socket):
                break
            sock = getattr(sock, step)
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            http.close()
        except Exception:
            pass

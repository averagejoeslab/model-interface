"""The gateway: one streaming call at a time, passed through and recorded block by block, cut by the killswitch.

The record is agents, sessions, streams, blocks. A stream is one request and the response that
streams back for it. Every level has an id, a start and an end. The record is live: a watcher
sees a block the moment it opens, each delta as it lands, and the end time when it closes.
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
from .killswitch import Killswitch
from .providers import Model


def iso(t: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def stamped(d: dict) -> dict:
    """A block or any record, with its times as ISO strings."""
    return {k: iso(v) if k in ("started", "ended") and isinstance(v, (int, float)) else v for k, v in d.items()}


@dataclass
class Stream:
    agent: str
    session: str
    model: Model
    request: dict                          # the body as received
    id: str = field(default_factory=lambda: f"str_{uuid.uuid4().hex[:12]}")
    started: float = field(default_factory=time.time)
    ended: float | None = None
    message: dict = field(default_factory=dict)    # the response's id, model and role, from its first line
    blocks: list = field(default_factory=list)
    stop: str | None = None                # why the response ended, in the format's own vocabulary
    usage: dict = field(default_factory=dict)   # as the provider names it
    interrupted: dict | None = None        # {"by": level, "at": when, "block": {index, type} or None} once cut
    error: str | None = None

    def snapshot(self, blocks: bool = True, request: bool = True) -> dict:
        out = {"id": self.id, "agent": self.agent, "session": self.session, "provider": self.model.provider.name,
               "model": self.model.name, "format": self.model.format.name,
               "started": iso(self.started), "ended": iso(self.ended), "message": self.message}
        if request:
            out["request"] = self.request
        if blocks:
            out["blocks"] = [stamped(b) for b in self.blocks]
        out.update({self.model.format.stop_field: self.stop, "usage": self.usage, "interrupted": self.interrupted, "error": self.error})
        return out


@dataclass
class Session:
    agent: str
    id: str
    started: float
    streams: list = field(default_factory=list)

    @property
    def ended(self) -> float | None:
        return self.streams[-1].ended if self.streams and all(s.ended for s in self.streams) else None

    def summary(self) -> dict:
        return {"agent": self.agent, "session": self.id, "started": iso(self.started), "ended": iso(self.ended),
                "streams": len(self.streams), "interrupted": sum(1 for s in self.streams if s.interrupted)}

    def snapshot(self, blocks: bool = True) -> dict:
        return {**self.summary(), "streams": [s.snapshot(blocks) for s in self.streams]}


class Record:
    """Agents, sessions, streams and blocks, live in memory, each finished stream appended to a file, with watchers.

    A watcher follows a scope: "*" for everything, an agent's name, or "agent/session".
    """

    def __init__(self, path: str | None, keep: int = 2000):
        self.path = os.path.expanduser(path) if path else None
        self.sessions: dict[tuple, Session] = collections.OrderedDict()
        self.streams: dict[str, Stream] = collections.OrderedDict()
        self.keep = keep
        self.lock = threading.Lock()
        self.watchers: dict[str, list[queue.Queue]] = collections.defaultdict(list)

    # ── writing ──────────────────────────────────────────────────────────────

    def open(self, stream: Stream):
        with self.lock:
            key = (stream.agent, stream.session)
            session = self.sessions.get(key)
            if session is None:
                session = self.sessions[key] = Session(stream.agent, stream.session, stream.started)
            session.streams.append(stream)
            self.streams[stream.id] = stream
            while len(self.streams) > self.keep:
                _, old = self.streams.popitem(last=False)
                s = self.sessions.get((old.agent, old.session))
                if s and old in s.streams:
                    s.streams.remove(old)
                    if not s.streams:
                        self.sessions.pop((old.agent, old.session), None)
        self.tell(stream, "stream.started", snapshot=stream.snapshot(blocks=False))

    def close(self, stream: Stream):
        self.tell(stream, "stream.ended", snapshot=stream.snapshot(blocks=False, request=False))
        if self.path:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(stream.snapshot()) + "\n")

    def tell(self, source: Stream, kind: str, **payload):
        """One record event about a stream. Every event names the agent, the session and the stream by id."""
        self.announce(kind, source.agent, source.session, stream=source.id, **payload)

    def announce(self, kind: str, agent: str | None = None, session: str | None = None, **payload):
        """One event, to everyone watching its session, its agent, or everything."""
        event = {"type": kind, "at": iso(time.time()), "agent": agent, "session": session,
                 **{k: stamped(v) if isinstance(v, dict) else v for k, v in payload.items()}}
        scopes = ["*"] + ([agent] if agent else []) + ([f"{agent}/{session}"] if agent and session else [])
        for scope in scopes:
            for q in list(self.watchers.get(scope, [])):
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

    def agents(self, names: list) -> list:
        """A summary per agent, for the names given, whether or not they have run yet."""
        by = {n: {"agent": n, "sessions": 0, "streams": 0, "running": 0, "interrupted": 0, "last": None} for n in names}
        for s in self.sessions.values():
            a = by.get(s.agent)
            if a is None:
                continue
            a["sessions"] += 1
            a["streams"] += len(s.streams)
            a["running"] += sum(1 for x in s.streams if not x.ended)
            a["interrupted"] += sum(1 for x in s.streams if x.interrupted)
            a["last"] = max(a["last"] or "", iso(s.streams[-1].started) if s.streams else "")
        return sorted(by.values(), key=lambda a: a["last"] or "", reverse=True)

    def list(self, agent: str | None = None) -> list:
        return [s.summary() for s in reversed(self.sessions.values()) if agent is None or s.agent == agent]

    def session(self, agent: str, id: str) -> Session | None:
        return self.sessions.get((agent, id))

    def stream(self, id: str) -> Stream | None:
        return self.streams.get(id)


class Gateway:
    POLL = 0.1          # how often the killswitch is checked while the provider is silent
    END = object()      # what the reader puts on the queue when the stream is over

    def __init__(self, record: Record | None = None, killswitch: Killswitch | None = None):
        self.record = record or Record(None)
        self.killswitch = killswitch or Killswitch()
        self.killswitch.listener = lambda **event: self.record.announce("killswitch", **event)

    def stream(self, stream: Stream, headers: dict):
        """The provider's stream, line by line, verbatim, read into the record as it passes. Cut within POLL seconds of a switch."""
        model = stream.model
        format: Format = model.format
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
                level = self.killswitch.scope_of(stream.agent, stream.session)
                if level:
                    stream.interrupted = {"by": level, "at": iso(time.time()),
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

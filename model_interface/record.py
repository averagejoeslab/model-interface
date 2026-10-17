"""The record: agents, sessions, streams and pieces, live in memory, each finished stream appended to a
file, with watchers that see every event as it happens.

A stream is one connection: opened when the request is forwarded, closed when the provider's last line
passes or someone hangs up. It holds the request as received and the response as it streams. A request
a rule refused is a stream too, one that never opened a connection.
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


def iso(t: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z") if t else None


def stamped(d: dict) -> dict:
    """A piece or any record, with its times as ISO strings."""
    return {k: iso(v) if k in ("started", "ended") and isinstance(v, (int, float)) else v for k, v in d.items()}


@dataclass
class Stream:
    agent: str
    session: str
    door: object                           # the Door the request came through
    provider: str                          # the provider's name
    model: str                             # the model as the provider names it
    request: dict                          # the body as received
    id: str = field(default_factory=lambda: f"str_{uuid.uuid4().hex[:12]}")
    started: float = field(default_factory=time.time)
    ended: float | None = None
    response: dict = field(default_factory=dict)   # everything the provider said about the response outside its pieces
    blocks: list = field(default_factory=list)     # the pieces, each with at, started, ended
    refused: dict | None = None            # {"by": level, "reason": rule or "switch"} when no connection was opened
    interrupted: dict | None = None        # {"by": level, "reason": ..., "at": when, "block": {at, type}} once cut
    alerts: list = field(default_factory=list)     # [{"rule", "when", "at"}] from rules that alerted
    error: str | None = None

    def snapshot(self, blocks: bool = True, request: bool = True, response: bool = True) -> dict:
        out = {"id": self.id, "agent": self.agent, "session": self.session, "door": self.door.name, "provider": self.provider,
               "model": self.model, "started": iso(self.started), "ended": iso(self.ended)}
        if request:
            out["request"] = self.request
        if response:
            out["response"] = self.door.response(self) if not self.refused else None
        if blocks:
            out["blocks"] = [stamped(b) for b in self.blocks]
        out.update({"refused": self.refused, "interrupted": self.interrupted, "alerts": self.alerts, "error": self.error})
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
                "streams": len(self.streams), "interrupted": sum(1 for s in self.streams if s.interrupted or s.refused),
                "alerts": sum(len(s.alerts) for s in self.streams)}

    def snapshot(self, blocks: bool = True) -> dict:
        return {**self.summary(), "streams": [s.snapshot(blocks) for s in self.streams]}


class Record:
    """A watcher follows a scope: "*" for everything, an agent's name, or "agent/session"."""

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
        self.tell(stream, "stream.started", snapshot=stream.snapshot(blocks=False, response=False))

    def close(self, stream: Stream):
        self.tell(stream, "stream.ended", snapshot=stream.snapshot(blocks=False, request=False))
        if self.path:
            with self.lock:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(stream.snapshot()) + "\n")

    def tell(self, source: Stream, kind: str, **payload):
        """One event about a stream: it names the agent, the session and the stream by id."""
        self.announce(kind, source.agent, source.session, stream=source.id, **payload)

    def announce(self, kind: str, agent: str | None = None, session: str | None = None, **payload):
        """One event, to everyone watching its session, its agent, or everything."""
        event = {"type": kind, "time": iso(time.time()), "agent": agent, "session": session,
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
        by = {n: {"agent": n, "sessions": 0, "streams": 0, "running": 0, "interrupted": 0, "alerts": 0, "last": None} for n in names}
        for s in self.sessions.values():
            a = by.get(s.agent)
            if a is None:
                continue
            summary = s.summary()
            a["sessions"] += 1
            a["streams"] += summary["streams"]
            a["running"] += sum(1 for x in s.streams if not x.ended)
            a["interrupted"] += summary["interrupted"]
            a["alerts"] += summary["alerts"]
            a["last"] = max(a["last"] or "", iso(s.streams[-1].started) if s.streams else "")
        return sorted(by.values(), key=lambda a: a["last"] or "", reverse=True)

    def list(self, agent: str | None = None) -> list:
        return [s.summary() for s in reversed(self.sessions.values()) if agent is None or s.agent == agent]

    def session(self, agent: str, id: str) -> Session | None:
        return self.sessions.get((agent, id))

    def stream(self, id: str) -> Stream | None:
        return self.streams.get(id)

"""The gateway: one streaming call at a time, passed through, recorded, and under the killswitch.

A Run is one call. Runs belong to sessions. The killswitch is a flag per session and one for
everything; while a flag is on, a running stream is cut at its next line and new calls are refused.
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


@dataclass
class Run:
    session: str
    key: str                               # the caller's key, by name
    model: str                             # the alias
    format: str
    request: dict                          # the body as received
    id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    started_at: float = field(default_factory=time.time)
    first_line_at: float | None = None
    ended_at: float | None = None
    lines: int = 0
    response: dict = field(default_factory=dict)   # the message, as read from the stream
    interrupted: bool = False
    error: str | None = None

    def record(self) -> dict:
        ms = lambda t: round((t - self.started_at) * 1000) if t else None
        return {"run": self.id, "session": self.session, "key": self.key, "model": self.model, "format": self.format,
                "started": datetime.datetime.fromtimestamp(self.started_at).isoformat(timespec="milliseconds"),
                "first_line_ms": ms(self.first_line_at), "duration_ms": ms(self.ended_at), "lines": self.lines,
                "stop_reason": self.response.get("stop_reason"), "usage": self.response.get("usage", {}),
                "interrupted": self.interrupted, "error": self.error, "request": self.request, "response": self.response}


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


class Sessions:
    """Every run, by session, in memory and appended to a file."""

    def __init__(self, path: str | None, keep: int = 2000):
        self.path = os.path.expanduser(path) if path else None
        self.runs = collections.OrderedDict()
        self.keep = keep
        self.lock = threading.Lock()

    def add(self, run: Run):
        line = run.record()
        with self.lock:
            self.runs[run.id] = line
            while len(self.runs) > self.keep:
                self.runs.popitem(last=False)
            if self.path:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(json.dumps(line) + "\n")

    @staticmethod
    def summary(line: dict) -> dict:
        """A run at a glance: what was asked, what came back, and the numbers."""
        asked = next((m for m in reversed(line["request"].get("messages", [])) if m.get("role") == "user"), {})
        content = asked.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") or ("[" + b.get("type", "") + "]") for b in content)
        answer = line["response"].get("content", [])
        return {**{k: v for k, v in line.items() if k not in ("request", "response")},
                "asked": content[:200],
                "answered": "".join(b.get("text", "") for b in answer if b.get("type") == "text")[:200],
                "tool_calls": [b.get("name") for b in answer if b.get("type") == "tool_use"]}

    def list(self) -> list:
        by = collections.OrderedDict()
        for line in self.runs.values():
            s = by.setdefault(line["session"], {"session": line["session"], "runs": 0, "first": line["started"], "last": None, "interrupted": 0})
            s["runs"] += 1
            s["last"] = line["started"]
            s["interrupted"] += line["interrupted"]
        return list(by.values())[::-1]

    def get(self, session: str) -> list:
        return [self.summary(l) for l in self.runs.values() if l["session"] == session]

    def run(self, id: str) -> dict | None:
        return self.runs.get(id)


class Gateway:
    def __init__(self, models: dict[str, Model], killswitch: Killswitch | None = None, sessions: Sessions | None = None):
        self.models = models
        self.killswitch = killswitch or Killswitch()
        self.sessions = sessions or Sessions(None)

    POLL = 0.1          # how often the killswitch is checked while the provider is silent
    END = object()      # what the reader puts on the queue when the stream is over

    def stream(self, run: Run, headers: dict):
        """The provider's stream, line by line, verbatim. Cut within POLL seconds of the killswitch, even mid-silence."""
        model = self.models[run.model]
        format: Format = model.provider.format
        reading = format.reading()
        lines: queue.Queue = queue.Queue()
        opened = {}                                    # the entered response, once the reader has it, so a cut can hang up

        def read():                                    # the provider's lines, read on their own thread so a silence can't hold the switch
            try:
                with format.open(model.provider.client, {**run.request, "model": model.name}, headers) as response:
                    opened["response"] = response
                    for line in response.iter_lines():
                        lines.put(line)
            except BaseException as e:
                lines.put(e)
            lines.put(self.END)

        threading.Thread(target=read, daemon=True).start()
        try:
            while True:
                try:
                    item = lines.get(timeout=self.POLL)
                except queue.Empty:
                    item = None                        # nothing arrived: check the switch anyway
                if self.killswitch.is_on(run.session):
                    run.interrupted = True
                    reading.message["stop_reason"] = "interrupted"
                    yield from format.closing(reading)
                    return
                if item is None:
                    continue
                if item is self.END:
                    return
                if isinstance(item, BaseException):
                    raise item
                run.lines += 1
                if run.first_line_at is None:
                    run.first_line_at = time.time()
                reading.feed(item)
                yield item + "\n"
        except Exception as e:
            run.error = f"{type(e).__name__}: {e}"
            if run.lines == 0:
                raise                                  # before anything was sent: the server answers with the provider's status
            yield format.error_line(str(e))
        finally:
            self._hang_up(opened.get("response"))      # the provider's connection goes, and so does its generation
            run.ended_at = time.time()
            run.response = reading.message
            self.sessions.add(run)

    @staticmethod
    def _hang_up(response):
        """Close the provider's connection from outside the reader's thread.

        A close alone doesn't wake a thread blocked on the socket, and the provider wouldn't notice until
        its next write, so the socket is shut down first when it can be reached; the close then follows.
        """
        if response is None:
            return
        http = getattr(response, "http_response", response)
        sock = http                                    # httpx -> httpcore -> the socket, by whatever names this version uses
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

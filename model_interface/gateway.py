"""The gateway: a call comes through a door with an agent's key, is routed to a provider by name, is
passed through verbatim, tapped as it streams, and can be stopped by the switches, which rules set.

The loop checks the switches on every line and every tenth of a second of silence, so a cut lands within
that, whatever the provider is doing. A rule runs in the same loop, after the tap and before the forward,
so the line it fires on is the last line the caller receives.
"""
import queue
import socket
import threading
import time

from .doors import Door
from .killswitch import Killswitch
from .providers import Provider
from .record import Record, Stream, iso
from .rules import Rules


class Refused(Exception):
    """A call that will not open a connection: a switch is on, or routing failed."""

    def __init__(self, status: int, error: dict):
        super().__init__(error.get("message"))
        self.status, self.error = status, error


class Gateway:
    POLL = 0.1          # how often the switches are checked while the provider is silent
    END = object()      # what the reader puts on the queue when the stream is over

    def __init__(self, providers: dict[str, Provider], record: Record | None = None, killswitch: Killswitch | None = None, rules: Rules | None = None):
        self.providers = providers
        self.record = record or Record(None)
        self.killswitch = killswitch or Killswitch()
        self.rules = rules or Rules()
        self.killswitch.listener = lambda **event: self.record.announce("killswitch", **event)

    # ── routing: the provider by name, from the model the caller wrote ───────

    def route(self, model: str) -> tuple[Provider, str]:
        """`provider/model` names the provider; a bare model works when there is one provider."""
        name, slash, rest = model.partition("/")
        if slash and name in self.providers:
            return self.providers[name], rest
        if len(self.providers) == 1:
            return next(iter(self.providers.values())), model
        raise Refused(404, {"type": "routing", "message": f"name the provider: {model!r} should be one of "
                            + ", ".join(f"{p}/{model}" for p in self.providers)})

    # ── the request moment ───────────────────────────────────────────────────

    def open(self, agent: str, session: str, door: Door, body: dict) -> Stream:
        """A stream for a request: routed, judged by the request rules, refused if a switch is on."""
        provider, model = self.route(body.get("model", ""))
        stream = Stream(agent=agent, session=session, door=door, provider=provider.name, model=model, request={**body, "model": model})
        self.record.open(stream)
        self.judge(stream, "request")
        tripped = self.killswitch.tripped(agent, session)
        if tripped:
            stream.refused, stream.ended = tripped, time.time()
            self.record.close(stream)
            where = {"gateway": "the whole gateway", "agent": f"agent {agent!r}", "session": f"session {session!r} of agent {agent!r}"}[tripped["by"]]
            raise Refused(423, {"type": "killswitch", "level": tripped["by"], "reason": tripped["reason"], "message": f"the killswitch is on for {where}: {tripped['reason']}"})
        return stream

    def judge(self, stream: Stream, when: str):
        """The rules at a moment: a trip sets a switch, an alert is recorded. Both are told on the feed."""
        for rule in self.rules.fired(stream, when):
            self.record.tell(stream, "rule.fired", rule=rule.name, when=when, action=rule.action, trip=rule.trip if rule.action == "trip" else None)
            if rule.action == "trip":
                self.killswitch.set(True, stream.agent, stream.session if rule.trip == "session" else None, reason=rule.name)
            else:
                stream.alerts.append({"rule": rule.name, "when": when, "at": iso(time.time())})

    # ── the connection: the stream moment, line by line, and the response moment ──

    def stream(self, stream: Stream, headers: dict):
        """The provider's stream, line by line, verbatim, tapped as it passes, cut within POLL seconds of a switch."""
        door, provider = stream.door, self.providers[stream.provider]
        reading = door.reading(stream, lambda kind, **payload: self.record.tell(stream, kind, **payload))
        lines: queue.Queue = queue.Queue()
        opened = {}

        def read():
            try:
                with door.open(provider.client(door), stream.request, headers) as response:
                    opened["response"] = response
                    for line in response.iter_lines():
                        lines.put(line)
            except BaseException as e:
                lines.put(e)
            lines.put(self.END)

        threading.Thread(target=read, daemon=True).start()
        sent, last = 0, ""
        try:
            while True:
                try:
                    item = lines.get(timeout=self.POLL)
                except queue.Empty:
                    item = None
                tripped = self.killswitch.tripped(stream.agent, stream.session)
                if tripped:
                    stream.interrupted = {**tripped, "at": iso(time.time()),
                                          "block": {"at": reading.open["at"], "type": reading.open.get("type")} if reading.open else None}
                    if last:
                        yield "\n"                        # end the event the last line belonged to, so the closing is its own
                    yield from door.closing(reading)
                    reading.stop("interrupted")
                    return
                if item is None:
                    continue
                if item is self.END:
                    return
                if isinstance(item, BaseException):
                    raise item
                sent, last = sent + 1, item
                reading.feed(item)                     # the tap
                self.judge(stream, "stream")           # the valve: a rule here is seen on the next line, or the next wake
                yield item + "\n"                      # the forward
        except Exception as e:
            stream.error = f"{type(e).__name__}: {e}"
            if sent == 0:
                raise
            yield door.error_line(str(e))
        finally:
            self._hang_up(opened.get("response"))
            reading.close_piece()
            stream.ended = time.time()
            self.judge(stream, "response")
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

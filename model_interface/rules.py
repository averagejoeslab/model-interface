"""Rules: CEL expressions over the record, run in the loop at three moments, with two actions.

A rule lives on the gateway, where it sees every agent, or on one agent. It binds to a moment:
  request   the whole request, before any connection is opened
  stream    the response as far as it has got, on every line while the connection is open
  response  the whole response, after the connection closed
and takes one action when it is true: `trip`, which sets the killswitch at a level, or `alert`, which
only records. A gateway rule trips the agent the stream belongs to, or its session; an agent rule trips
the session. No rule reaches the gateway level: that switch is for people.

A rule sees: agent, session, provider, model, door, request, response (as composed so far), piece (the
one still open, or null), and text, every string anywhere in what the moment is about, joined.
"""
from dataclasses import dataclass, field

import celpy

from .tap import strings_of

MOMENTS = ("request", "stream", "response")
ACTIONS = ("trip", "alert")


@dataclass
class Rule:
    name: str
    expr: str
    when: str = "stream"                   # a moment
    action: str = "trip"
    on: str = "gateway"                    # "gateway" or an agent's name
    trip: str | None = None                # "agent" or "session"; defaults by where the rule lives
    program: object = field(init=False, repr=False)

    def __post_init__(self):
        if self.when not in MOMENTS:
            raise ValueError(f"rule {self.name!r}: when must be one of {MOMENTS}, not {self.when!r}")
        if self.action not in ACTIONS:
            raise ValueError(f"rule {self.name!r}: action must be one of {ACTIONS}, not {self.action!r}")
        if self.trip is None:
            self.trip = "agent" if self.on == "gateway" else "session"
        allowed = ("agent", "session") if self.on == "gateway" else ("session",)
        if self.trip not in allowed:
            raise ValueError(f"rule {self.name!r} on {self.on!r} may trip {allowed}, not {self.trip!r}; the gateway level is for people")
        env = celpy.Environment()
        try:
            self.program = env.program(env.compile(self.expr))
        except Exception as e:
            raise ValueError(f"rule {self.name!r}: {e}") from e

    def fires(self, view: dict) -> bool:
        try:
            return bool(self.program.evaluate({k: celpy.json_to_cel(v) for k, v in view.items()}))
        except Exception:
            return False                   # a rule that cannot be evaluated on this view does not fire


class Rules:
    def __init__(self, rules: list[Rule] = ()):
        self.rules = list(rules)

    def at(self, when: str, agent: str) -> list[Rule]:
        """The rules that run at a moment for a stream of this agent: the gateway's, then the agent's own."""
        return [r for r in self.rules if r.when == when and r.on in ("gateway", agent)]

    @staticmethod
    def view(stream, when: str) -> dict:
        """What a rule sees at a moment."""
        response = stream.door.response(stream) if when != "request" else None
        piece = next((b for b in reversed(stream.blocks) if b["ended"] is None), None) if when == "stream" else None
        about = stream.request if when == "request" else response
        return {"agent": stream.agent, "session": stream.session, "provider": stream.provider, "model": stream.model,
                "door": stream.door.name, "request": stream.request, "response": response, "piece": piece, "text": strings_of(about)}

    def fired(self, stream, when: str) -> list[Rule]:
        """The rules that fire for this stream at this moment, with the stream as it stands."""
        rules = self.at(when, stream.agent)
        if not rules:
            return []
        view = self.view(stream, when)
        return [r for r in rules if r.fires(view)]

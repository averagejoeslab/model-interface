"""The killswitch: a state at gateway, agent or session level.

On means every running stream in scope is cut and every new call in scope is refused, until an
operator turns it off. There is no pause.
"""
import threading


class Killswitch:
    def __init__(self, listener=lambda **event: None):
        self.gateway = False
        self.agents: set[str] = set()
        self.sessions: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.listener = listener           # told of every change, so watchers see switches move

    def set(self, on: bool, agent: str | None = None, session: str | None = None):
        with self.lock:
            if agent is None:
                self.gateway = on
            elif session is None:
                (self.agents.add if on else self.agents.discard)(agent)
            else:
                (self.sessions.add if on else self.sessions.discard)((agent, session))
        self.listener(level=self.level(agent, session), on=on, agent=agent, session=session, state=self.state())

    @staticmethod
    def level(agent=None, session=None) -> str:
        return "gateway" if agent is None else "agent" if session is None else "session"

    def scope_of(self, agent: str, session: str) -> str | None:
        """The level that has this stream switched off, widest first, or None if it may run."""
        if self.gateway:
            return "gateway"
        if agent in self.agents:
            return "agent"
        if (agent, session) in self.sessions:
            return "session"
        return None

    def is_on(self, agent: str | None = None, session: str | None = None) -> bool:
        return self.gateway if agent is None else agent in self.agents if session is None else (agent, session) in self.sessions

    def state(self) -> dict:
        return {"gateway": self.gateway, "agents": sorted(self.agents),
                "sessions": [{"agent": a, "session": s} for a, s in sorted(self.sessions)]}

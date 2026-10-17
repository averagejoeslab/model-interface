"""The killswitch: a state at gateway, agent or session level, with the reason it was set.

On means every running stream in scope is cut and every new call in scope is refused, until an operator
turns it off. A rule sets it with its name as the reason; a person sets it by hand. There is no pause.
The gateway level is reserved for people.
"""
import threading

LEVELS = ("gateway", "agent", "session")


class Killswitch:
    def __init__(self, listener=lambda **event: None):
        self.gateway: str | None = None                   # the reason, when on
        self.agents: dict[str, str] = {}                  # agent -> reason
        self.sessions: dict[tuple[str, str], str] = {}    # (agent, session) -> reason
        self.lock = threading.Lock()
        self.listener = listener

    def set(self, on: bool, agent: str | None = None, session: str | None = None, reason: str = "operator"):
        with self.lock:
            if agent is None:
                self.gateway = reason if on else None
            elif session is None:
                if on:
                    self.agents[agent] = reason
                else:
                    self.agents.pop(agent, None)
            else:
                if on:
                    self.sessions[(agent, session)] = reason
                else:
                    self.sessions.pop((agent, session), None)
        self.listener(level=self.level(agent, session), on=on, agent=agent, session=session, reason=reason, state=self.state())

    @staticmethod
    def level(agent=None, session=None) -> str:
        return "gateway" if agent is None else "agent" if session is None else "session"

    def tripped(self, agent: str, session: str) -> dict | None:
        """The widest level that has this stream switched off, with its reason, or None if it may run."""
        if self.gateway:
            return {"by": "gateway", "reason": self.gateway}
        if agent in self.agents:
            return {"by": "agent", "reason": self.agents[agent]}
        if (agent, session) in self.sessions:
            return {"by": "session", "reason": self.sessions[(agent, session)]}
        return None

    def is_on(self, agent: str | None = None, session: str | None = None) -> bool:
        return bool(self.gateway) if agent is None else agent in self.agents if session is None else (agent, session) in self.sessions

    def state(self) -> dict:
        return {"gateway": self.gateway, "agents": [{"agent": a, "reason": r} for a, r in sorted(self.agents.items())],
                "sessions": [{"agent": a, "session": s, "reason": r} for (a, s), r in sorted(self.sessions.items())]}

"""Providers and agents: the only two things an operator configures besides rules.

A provider is a name, a root URL and a key. It has no format: the caller's door decides which SDK reaches
it, and a provider that does not speak that door's format says so itself, with its own error, passed through.
An agent is a name and its virtual keys. It has no grants: what it may do is said by rules.
"""
from dataclasses import dataclass, field


@dataclass
class Provider:
    name: str
    url: str                               # the provider's root; each door's SDK adds its own path
    key: str = ""
    clients: dict = field(default_factory=dict, init=False, repr=False)

    def client(self, door):
        """The SDK client for this provider behind a door, made once."""
        if door.name not in self.clients:
            self.clients[door.name] = door.client(self.url, self.key)
        return self.clients[door.name]


@dataclass
class Agent:
    name: str
    keys: list

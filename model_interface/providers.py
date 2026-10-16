"""Providers, their models, and the agents that may use them.

A provider is an endpoint and a key. The endpoint's path says which wire format it speaks, and so
which SDK reaches it. Models are the provider's own names. An agent is a set of keys and the
models it may call; a call names a model directly, and the agent's grants say which provider.
"""
from dataclasses import dataclass, field

from .formats import FORMATS, Format


def format_of(endpoint: str) -> Format:
    """The format an endpoint speaks, read off its path."""
    for format in FORMATS.values():
        if endpoint.rstrip("/").endswith(format.path):
            return format
    raise ValueError(f"{endpoint!r} is not a Messages or a Chat Completions endpoint; it must end in "
                     + " or ".join(f.path for f in FORMATS.values()))


@dataclass
class Provider:
    name: str
    endpoint: str                          # the full URL the provider serves, e.g. https://api.deepseek.com/anthropic/v1/messages
    key: str = ""
    models: list = field(default_factory=list)
    format: Format = field(init=False)
    client: object = field(init=False, repr=False)

    def __post_init__(self):
        self.format = format_of(self.endpoint)
        self.url = self.endpoint.rstrip("/").removesuffix(self.format.path)      # what the SDK calls base_url
        self.models = [Model(self, m) if isinstance(m, str) else m for m in self.models]
        self.client = self.format.client(self.url, self.key)

    def model(self, name: str):
        return next((m for m in self.models if m.name == name), None)


@dataclass(eq=False)
class Model:
    provider: Provider
    name: str                              # the provider's own name for it

    @property
    def id(self) -> str:
        return f"{self.provider.name}/{self.name}"

    @property
    def format(self) -> Format:
        return self.provider.format


@dataclass
class Agent:
    name: str
    keys: list
    models: list                           # Model grants; one provider per model name per format

    def __post_init__(self):
        seen = {}
        for m in self.models:
            other = seen.setdefault((m.format.name, m.name), m)
            if other is not m:
                raise ValueError(f"agent {self.name!r} is granted {m.name!r} on both {other.provider.name!r} and {m.provider.name!r}; "
                                 f"a model name must resolve to one provider per format")

    def resolve(self, format: Format, name: str) -> Model | None:
        """The model this agent means by a name, on the format it called."""
        return next((m for m in self.models if m.format is format and m.name == name), None)

    def granted(self, name: str) -> list:
        return [m for m in self.models if m.name == name]

"""Configuration: a TOML file naming providers with their models, agents with their keys and
grants, the operator's key, and where streams are recorded.

A value written as "env:NAME" is read from that environment variable, so no key sits in the file.
"""
import os
import tomllib
from dataclasses import dataclass, field

from .providers import Agent, Provider


@dataclass
class Config:
    providers: dict = field(default_factory=dict)      # name -> Provider
    agents: dict = field(default_factory=dict)         # name -> Agent
    admin: str = ""                                    # the operator's key: the record, the page, the switches
    record: str | None = "~/.model-interface/streams.jsonl"

    def __post_init__(self):
        self.keys = {k: a for a in self.agents.values() for k in a.keys if k}      # an agent's key -> the agent

    def agent_of(self, key: str) -> Agent | None:
        return self.keys.get(key) if key else None


def secret(value: str) -> str:
    return os.environ.get(value[4:], "") if value.startswith("env:") else value


def load(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    providers = {name: Provider(name, p["endpoint"], secret(p.get("key", "")), list(p.get("models", [])))
                 for name, p in data.get("providers", {}).items()}

    def grant(agent: str, ref: str):
        provider, _, name = ref.partition("/")
        if provider not in providers:
            raise ValueError(f"agent {agent!r}: no provider {provider!r} for {ref!r}")
        model = providers[provider].model(name)
        if model is None:
            raise ValueError(f"agent {agent!r}: provider {provider!r} has no model {name!r}; add it under [providers.{provider}] models")
        return model

    agents = {name: Agent(name, [secret(k) for k in a.get("keys", [])], [grant(name, ref) for ref in a.get("models", [])])
              for name, a in data.get("agents", {}).items()}
    return Config(providers=providers, agents=agents, admin=secret(data.get("admin", "")), record=data.get("record", Config.record))

"""Configuration: providers, agents, rules, the operator's key, and where streams are recorded.

A value written as "env:NAME" is read from that environment variable, so no key sits in the file.
"""
import os
import tomllib
from dataclasses import dataclass, field

from .providers import Agent, Provider
from .rules import Rule


@dataclass
class Config:
    providers: dict = field(default_factory=dict)      # name -> Provider
    agents: dict = field(default_factory=dict)         # name -> Agent
    rules: list = field(default_factory=list)          # Rule, in order
    admin: str = ""                                    # the operator's key: the record, the page, the switches
    record: str | None = "~/.model-interface/streams.jsonl"

    def __post_init__(self):
        self.keys = {k: a for a in self.agents.values() for k in a.keys if k}
        for r in self.rules:
            if r.on != "gateway" and r.on not in self.agents:
                raise ValueError(f"rule {r.name!r} is on {r.on!r}, which is not an agent")

    def agent_of(self, key: str) -> Agent | None:
        return self.keys.get(key) if key else None


def secret(value: str) -> str:
    return os.environ.get(value[4:], "") if value.startswith("env:") else value


def load(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    providers = {name: Provider(name, p["url"], secret(p.get("key", ""))) for name, p in data.get("providers", {}).items()}
    agents = {name: Agent(name, [secret(k) for k in a.get("keys", [])]) for name, a in data.get("agents", {}).items()}
    rules = [Rule(r["name"], r["expr"], r.get("when", "stream"), r.get("action", "trip"), r.get("on", "gateway"), r.get("trip"))
             for r in data.get("rules", [])]
    return Config(providers=providers, agents=agents, rules=rules, admin=secret(data.get("admin", "")), record=data.get("record", Config.record))

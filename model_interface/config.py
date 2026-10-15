"""Configuration: a TOML file naming providers, model aliases, the keys callers use, and where runs are recorded.

A value written as "env:NAME" is read from that environment variable, so no key sits in the file.
"""
import os
import tomllib
from dataclasses import dataclass

from .formats import FORMATS
from .providers import Model, Provider


@dataclass
class Config:
    models: dict[str, Model]
    keys: dict[str, str]             # a caller's key -> its name; none of them is a provider's
    record: str | None = "~/.model-interface/runs.jsonl"


def secret(value: str) -> str:
    return os.environ.get(value[4:], "") if value.startswith("env:") else value


def load(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    providers = {name: Provider(name, FORMATS[p["format"]], p["url"], secret(p.get("key", "")))
                 for name, p in data["providers"].items()}
    models = {alias: Model(alias, providers[m["provider"]], m["model"]) for alias, m in data["models"].items()}
    keys = {secret(value): name for name, value in data.get("keys", {}).items() if secret(value)}
    return Config(models=models, keys=keys, record=data.get("record", Config.record))

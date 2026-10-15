"""Configuration: a TOML file naming providers, models, tools, the sandbox image and the keys callers use.

A value written as "env:NAME" is read from that environment variable, so no key sits in the file.
"""
import os
import tomllib
from dataclasses import dataclass

from .formats import FORMATS
from .providers import Model, Provider
from .sandbox import DEFAULT_IMAGE, Sandboxes
from .tools import Tool, make_tools


@dataclass
class Config:
    models: dict[str, Model]
    tools: dict[str, Tool]
    sandboxes: Sandboxes
    keys: set[str]  # the keys callers present; none of them is a provider's


def secret(value: str) -> str:
    return os.environ.get(value[4:], "") if value.startswith("env:") else value


def load(path: str) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    providers = {name: Provider(name, FORMATS[p["format"]], p["url"], secret(p.get("key", "")))
                 for name, p in data["providers"].items()}
    models = {alias: Model(alias, providers[m["provider"]], m["model"]) for alias, m in data["models"].items()}
    sandbox = data.get("sandbox", {})
    return Config(
        models=models,
        tools=make_tools(data.get("tools", {"shell": {"kind": "shell"}})),
        sandboxes=Sandboxes(sandbox.get("image", DEFAULT_IMAGE), sandbox.get("workspaces", "~/.model-interface/workspaces")),
        keys={secret(k) for k in data.get("keys", [])} - {""},
    )

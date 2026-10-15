"""Tools the platform runs.

A tool has a definition the model sees and a run method that acts in the session's sandbox.
The caller names a registered tool in its request and never runs it.
"""
from abc import ABC, abstractmethod

from .sandbox import Sandbox


class Tool(ABC):
    name: str
    description: str
    input_schema: dict

    @property
    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    @abstractmethod
    def run(self, sandbox: Sandbox, input: dict) -> str: ...


class ShellTool(Tool):
    input_schema = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}

    def __init__(self, name: str = "shell", description: str | None = None):
        self.name = name
        self.description = description or (
            "Run a command in sh, in a Linux container that holds this session's workspace at /work and nothing "
            "else. No network. Each call can do several things: independent steps together, dependent ones in "
            "order. Keep calls focused, so their results stay small. What you read back, not what you expected, "
            "decides whether it worked.")

    def run(self, sandbox, input):
        return sandbox.run(input["cmd"])


KINDS = {"shell": ShellTool}


def make_tools(config: dict) -> dict[str, Tool]:
    """Tools from config: {name: {kind: ..., description?: ...}}."""
    return {name: KINDS[spec.get("kind", name)](name, spec.get("description")) for name, spec in config.items()}

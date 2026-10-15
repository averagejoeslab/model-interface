"""Fakes: a provider that plays a script, and a sandbox that records commands."""
import copy

import pytest

from model_interface.config import Config
from model_interface.formats import FORMATS
from model_interface.providers import Model, Provider
from model_interface.sandbox import Sandbox, Sandboxes
from model_interface.tools import ShellTool


class FakeSandbox(Sandbox):
    def __init__(self, session, image, workspaces):
        self.session, self.ran = session, []

    def run(self, cmd, timeout=60):
        self.ran.append(cmd)
        return "OK" if "fix" in cmd else "FAIL: 15 != 22.5"


class FakeProvider(Provider):
    """Answers from a script, and keeps every request it was sent."""

    def __init__(self, script):
        super().__init__("fake", FORMATS["anthropic"], "http://fake", "")
        self.script, self.seen = list(script), []

    def complete(self, request):
        self.seen.append(copy.deepcopy(request))  # the loop keeps appending to its messages list
        return self.script.pop(0)


def reply(*blocks, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "fake", "content": list(blocks),
            "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 5}}


def text(s):
    return {"type": "text", "text": s}


def call(id, name="shell", **input):
    return {"type": "tool_use", "id": id, "name": name, "input": input}


@pytest.fixture
def script():
    """Run the tests, see them fail, fix them, report."""
    return [reply(text("Running the tests."), call("t1", cmd="python3 -m unittest -q"), stop="tool_use"),
            reply(call("t2", cmd="sed -i fix prices.py && python3 -m unittest -q"), stop="tool_use"),
            reply(text("Fixed."))]


@pytest.fixture
def config(script):
    provider = FakeProvider(script)
    return Config(models={"coder": Model("coder", provider, "fake-model")},
                  tools={"shell": ShellTool()},
                  sandboxes=Sandboxes(make=FakeSandbox),
                  keys={"mi-test-key"})

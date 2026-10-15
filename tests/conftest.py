"""Fakes: a provider that plays a script of events, slowly if asked, and a server on a real port."""
import copy
import socket
import threading
import time

import pytest
import uvicorn

from model_interface.config import Config
from model_interface.formats import FORMATS
from model_interface.gateway import Gateway, Record, Stops
from model_interface.providers import Model, Provider
from model_interface.server import create_app


def events_for(text="Hello there, friend.", tool=None, stop="end_turn", model="fake"):
    """The canonical events of one answer: text word by word, then an optional tool call."""
    out = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                                 "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
           {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    out += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}} for w in text.replace(" ", " \x00").split("\x00")]
    out.append({"type": "content_block_stop", "index": 0})
    if tool:
        out += [{"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": tool[0], "input": {}}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": tool[1][:5]}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": tool[1][5:]}},
                {"type": "content_block_stop", "index": 1}]
        stop = "tool_use"
    out += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}},
            {"type": "message_stop"}]
    return out


class FakeProvider(Provider):
    """Plays a script of events, keeps every request, and knows when it was closed."""

    def __init__(self, events=None, delay=0.0):
        super().__init__("fake", FORMATS["anthropic"], "http://fake", "")
        self.script, self.delay, self.seen, self.closed, self.sent = list(events or events_for()), delay, [], False, 0

    def stream(self, request):
        self.seen.append(copy.deepcopy(request))
        self.closed, self.sent = False, 0
        try:
            for e in self.script:
                if self.delay:
                    time.sleep(self.delay)
                self.sent += 1
                yield e
        finally:
            self.closed = True


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def config(provider):
    return Config(models={"coder": Model("coder", provider, "fake-model")}, keys={"mi-test-key": "team-test"}, record=None)


@pytest.fixture
def gateway(config):
    return Gateway(config.models, Stops(), Record(None))


@pytest.fixture
def server(config, gateway):
    """The real app on a real port, so streams are live and can be interrupted mid-way."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(config, gateway), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True

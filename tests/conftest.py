"""A fake provider that speaks both formats over real HTTP, and the gateway on a real port in front of it."""
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from model_interface.config import Config
from model_interface.formats import FORMATS
from model_interface.gateway import Gateway, Record
from model_interface.providers import Model, Provider
from model_interface.server import create_app


def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.02)
    return srv, f"http://127.0.0.1:{port}"


class FakeProvider:
    """Answers in whichever format was asked, word by word, and notices when the caller hangs up."""

    def __init__(self):
        self.text, self.tool, self.delay, self.stall = "Hello there, friend.", None, 0.0, None   # stall: (after line n, seconds of silence)
        self.thinking = None             # a thinking (Messages) or reasoning (Chat Completions) block before the text
        self.requests, self.closed_early = [], False
        self.app = FastAPI()
        self.app.post("/v1/messages")(self.messages)
        self.app.post("/v1/chat/completions")(self.chat)

    def words(self):
        return self.text.replace(" ", " \x00").split("\x00")

    async def messages(self, request: Request):
        body = await request.json()
        self.requests.append({"path": "/v1/messages", "headers": dict(request.headers), "body": body})
        if body["model"] == "bad":
            return JSONResponse({"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}}, status_code=400)
        events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                                                        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}]
        i = 0
        if self.thinking:
            events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                       {"type": "ping"},
                       {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": self.thinking}},
                       {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig_abc"}},
                       {"type": "content_block_stop", "index": 0}]
            i = 1
        events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
        events += [{"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": w}} for w in self.words()]
        events.append({"type": "content_block_stop", "index": i})
        stop = "end_turn"
        if self.tool:
            args = json.dumps(self.tool[1])
            events += [{"type": "content_block_start", "index": i + 1, "content_block": {"type": "tool_use", "id": "t1", "name": self.tool[0], "input": {}}},
                       {"type": "content_block_delta", "index": i + 1, "delta": {"type": "input_json_delta", "partial_json": args[:6]}},
                       {"type": "content_block_delta", "index": i + 1, "delta": {"type": "input_json_delta", "partial_json": args[6:]}},
                       {"type": "content_block_stop", "index": i + 1}]
            stop = "tool_use"
        events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 7}}, {"type": "message_stop"}]
        return StreamingResponse(self.emit([f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]), media_type="text/event-stream")

    async def chat(self, request: Request):
        body = await request.json()
        self.requests.append({"path": "/v1/chat/completions", "headers": dict(request.headers), "body": body})
        if body["model"] == "bad":
            return JSONResponse({"error": {"message": "prompt is too long", "type": "invalid_request_error"}}, status_code=400)
        head = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
        chunks = [{**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}]
        if self.thinking:
            chunks += [{**head, "choices": [{"index": 0, "delta": {"reasoning_content": w}, "finish_reason": None}]} for w in self.thinking.replace(" ", " \x00").split("\x00")]
        chunks += [{**head, "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}]} for w in self.words()]
        finish = "stop"
        if self.tool:
            args = json.dumps(self.tool[1])
            chunks += [{**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": self.tool[0], "arguments": ""}}]}, "finish_reason": None}]},
                       {**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[:6]}}]}, "finish_reason": None}]},
                       {**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[6:]}}]}, "finish_reason": None}]}]
            finish = "tool_calls"
        chunks += [{**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                   {**head, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}}]
        return StreamingResponse(self.emit([f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]), media_type="text/event-stream")

    async def emit(self, lines):
        import asyncio
        self.closed_early, sent = False, 0
        try:
            for line in lines:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.stall and sent == self.stall[0]:
                    await asyncio.sleep(self.stall[1])
                yield line
                sent += 1
        finally:
            self.closed_early = sent < len(lines)


@pytest.fixture(scope="session")
def fake():
    provider = FakeProvider()
    srv, url = serve(provider.app)
    provider.url = url
    yield provider
    srv.should_exit = True


@pytest.fixture
def provider(fake):
    fake.text, fake.tool, fake.delay, fake.stall, fake.thinking, fake.requests[:], fake.closed_early = "Hello there, friend.", None, 0.0, None, None, [], False
    return fake


@pytest.fixture
def config(provider):
    anthropic = Provider("fake-anthropic", FORMATS["anthropic"], provider.url, "")
    openai = Provider("fake-openai", FORMATS["openai"], provider.url + "/v1", "")
    return Config(models={"coder": Model("coder", anthropic, "fake-sonnet"), "gpt": Model("gpt", openai, "fake-gpt"),
                          "broken": Model("broken", anthropic, "bad"), "broken-gpt": Model("broken-gpt", openai, "bad")},
                  keys={"mi-test-key": "team-test"}, record=None)


@pytest.fixture
def gateway(config):
    return Gateway(config.models, Record(None))


@pytest.fixture
def server(config, gateway):
    srv, url = serve(create_app(config, gateway))
    yield url
    srv.should_exit = True

"""The two wire formats, each passed through by its own SDK.

A Format knows how to open a streaming call with the native SDK, how to read the stream's lines
into a message for the record, and how to end a stream early so the caller still sees a whole
message. It never changes what goes over the wire.
"""
import inspect
import json
import time
import uuid
from abc import ABC, abstractmethod

import anthropic
import openai


def data_of(line: str):
    """The JSON of a data line, or None for anything else (event names, blanks, [DONE])."""
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload and payload != "[DONE]":
            return json.loads(payload)
    return None


class Reading:
    """A message, read from a stream one line at a time. Only for the record; the wire is untouched."""

    def __init__(self):
        self.message = {"id": None, "model": None, "content": [], "stop_reason": None, "usage": {}}
        self.started = False       # has the stream said what message it is
        self.open = None           # the index of a content block that hasn't closed

    def feed(self, line: str):
        data = data_of(line)
        if data is not None:
            self.take(data)

    def take(self, data: dict): ...


class Format(ABC):
    name: str
    endpoint: str              # the path, on this gateway and on the provider

    @abstractmethod
    def client(self, url: str, key: str): ...

    @abstractmethod
    def open(self, client, body: dict, headers: dict):
        """A streaming call, as the SDK's streaming-response context manager."""

    @abstractmethod
    def reading(self) -> Reading: ...

    @abstractmethod
    def closing(self, reading: Reading) -> list[str]:
        """The stream text that ends an interrupted stream so the message is whole."""

    @abstractmethod
    def error_line(self, message: str) -> str: ...

    @staticmethod
    def status_of(error):
        """A provider's refusal: (status, body), or None if the error isn't one."""
        if isinstance(error, (anthropic.APIStatusError, openai.APIStatusError)):
            return error.status_code, error.body if isinstance(error.body, dict) else {"error": str(error.body or error)}
        return None

    @staticmethod
    def _split(create, body: dict):
        """The body as the SDK's own parameters, with anything it doesn't know passed as extra_body."""
        known = inspect.signature(create).parameters
        kwargs = {k: v for k, v in body.items() if k in known and k != "stream"}
        extra = {k: v for k, v in body.items() if k not in known}
        return kwargs, extra or None


# ── Anthropic Messages ───────────────────────────────────────────────────────

class AnthropicReading(Reading):
    def __init__(self):
        super().__init__()
        self.blocks, self.partial = {}, {}

    def take(self, e):
        t = e.get("type")
        if t == "message_start":
            self.started = True
            self.message.update({k: e["message"].get(k) for k in ("id", "model", "usage")})
        elif t == "content_block_start":
            self.open = e["index"]
            self.blocks[e["index"]], self.partial[e["index"]] = dict(e["content_block"]), ""
        elif t == "content_block_delta":
            d, b = e["delta"], self.blocks[e["index"]]
            if d["type"] == "text_delta":
                b["text"] = b.get("text", "") + d["text"]
            elif d["type"] == "input_json_delta":
                self.partial[e["index"]] += d["partial_json"]
            elif d["type"] == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d["thinking"]
        elif t == "content_block_stop":
            self.open = None
            b = self.blocks[e["index"]]
            if b["type"] == "tool_use" and self.partial[e["index"]].strip():
                b["input"] = json.loads(self.partial[e["index"]])
        elif t == "message_delta":
            self.message["stop_reason"] = e["delta"].get("stop_reason")
            self.message["usage"] = {**self.message.get("usage", {}), **e.get("usage", {})}
        elif t == "error":
            self.message["error"] = e.get("error")
        self.message["content"] = [self.blocks[i] for i in sorted(self.blocks)]


class AnthropicFormat(Format):
    name, endpoint = "anthropic", "/v1/messages"

    def client(self, url, key):
        return anthropic.Anthropic(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.messages.create, body)
        beta = {"anthropic-beta": headers["anthropic-beta"]} if headers.get("anthropic-beta") else None
        return client.messages.with_streaming_response.create(**kwargs, stream=True, extra_body=extra, extra_headers=beta)

    def reading(self):
        return AnthropicReading()

    def closing(self, reading):
        def sse(e):
            return f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
        out = []
        if not reading.started:
            out.append(sse({"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant",
                                                                 "model": reading.message.get("model") or "", "content": [], "stop_reason": None,
                                                                 "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        if reading.open is not None:
            out.append(sse({"type": "content_block_stop", "index": reading.open}))
        out.append(sse({"type": "message_delta", "delta": {"stop_reason": "interrupted", "stop_sequence": None},
                        "usage": {"output_tokens": reading.message.get("usage", {}).get("output_tokens", 0)}}))
        out.append(sse({"type": "message_stop"}))
        return out

    def error_line(self, message):
        return f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'upstream_error', 'message': message}})}\n\n"


# ── OpenAI Chat Completions ──────────────────────────────────────────────────

class OpenAIReading(Reading):
    STOP = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}

    def __init__(self):
        super().__init__()
        self.text, self.calls = "", {}

    def take(self, c):
        if not self.started:
            self.started = True
            self.message.update({"id": c.get("id"), "model": c.get("model")})
        if c.get("usage"):
            self.message["usage"] = {"input_tokens": c["usage"].get("prompt_tokens", 0), "output_tokens": c["usage"].get("completion_tokens", 0)}
        for choice in c.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.text += delta["content"]
                self.open = 0
            for call in delta.get("tool_calls") or []:
                entry = self.calls.setdefault(call.get("index", 0), {"type": "tool_use", "id": call.get("id"), "name": None, "arguments": ""})
                entry["id"] = entry["id"] or call.get("id")
                entry["name"] = entry["name"] or call.get("function", {}).get("name")
                entry["arguments"] += call.get("function", {}).get("arguments") or ""
            if choice.get("finish_reason"):
                self.message["stop_reason"] = self.STOP.get(choice["finish_reason"], choice["finish_reason"])
                self.open = None
        content = [{"type": "text", "text": self.text}] if self.text else []
        for i in sorted(self.calls):
            call = self.calls[i]
            try:
                arguments = json.loads(call["arguments"]) if call["arguments"].strip() else {}
            except ValueError:
                arguments = {"_partial": call["arguments"]}
            content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": arguments})
        self.message["content"] = content


class OpenAIFormat(Format):
    name, endpoint = "openai", "/v1/chat/completions"

    def client(self, url, key):
        return openai.OpenAI(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.chat.completions.create, body)
        return client.chat.completions.with_streaming_response.create(**kwargs, stream=True, extra_body=extra)

    def reading(self):
        return OpenAIReading()

    def closing(self, reading):
        chunk = {"id": reading.message.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": reading.message.get("model") or "", "interrupted": True,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        return [f"data: {json.dumps(chunk)}\n\n", "data: [DONE]\n\n"]

    def error_line(self, message):
        return f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': message}})}\n\n"


FORMATS = {"anthropic": AnthropicFormat(), "openai": OpenAIFormat()}

"""The two wire formats, each passed through by its own SDK.

A Format knows how to open a streaming call with the native SDK, how to read the stream's lines
into the record's blocks as they arrive, and how to end a stream early so the caller still sees a
whole message. It never changes what goes over the wire.
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
    """Reads a stream's lines into a Stream record, block by block, telling a listener as it goes.

    The record is the live object: a block appears when it opens, grows with each delta, and gets
    its end time when it closes. Only the record is touched; the wire is forwarded untouched.
    """

    def __init__(self, stream, listener=lambda kind, **payload: None):
        self.stream = stream
        self.listener = listener
        self.open = None                 # the block that hasn't closed yet, or None

    def feed(self, line: str):
        data = data_of(line)
        if data is not None:
            self.take(data)

    def take(self, data: dict): ...

    # ── what every format does to the record ─────────────────────────────────

    def start(self, **message):
        self.stream.message.update(message)
        self.listener("stream.message", message=message)

    def open_block(self, type: str, **fields):
        self.close_block()
        block = {"index": len(self.stream.blocks), "type": type, "started": time.time(), "ended": None, **fields}
        self.stream.blocks.append(block)
        self.open = block
        self.listener("block.started", block=block)
        return block

    def grow(self, field: str, piece: str):
        if self.open is not None and piece:
            self.open[field] = self.open.get(field, "") + piece
            self.listener("block.delta", index=self.open["index"], field=field, delta=piece)

    def close_block(self):
        if self.open is None:
            return
        self.open["ended"] = time.time()
        self.finish_block(self.open)
        self.listener("block.ended", block=self.open)
        self.open = None

    def finish_block(self, block: dict):
        """Anything to settle once a block is whole, such as parsing its input."""

    def stop(self, reason, usage=None):
        self.close_block()
        self.stream.stop_reason = reason
        if usage:
            self.stream.usage.update(usage)


class Format(ABC):
    name: str
    endpoint: str              # the path, on this gateway and on the provider

    @abstractmethod
    def client(self, url: str, key: str): ...

    @abstractmethod
    def open(self, client, body: dict, headers: dict):
        """A streaming call, as the SDK's streaming-response context manager."""

    @abstractmethod
    def reading(self, stream, listener) -> Reading: ...

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
    """Blocks are explicit on the wire: content_block_start, deltas, content_block_stop."""

    def __init__(self, stream, listener):
        super().__init__(stream, listener)
        self.partial = ""                # a tool_use input, as JSON text, until the block closes

    def take(self, e):
        t = e.get("type")
        if t == "message_start":
            m = e["message"]
            self.start(id=m.get("id"), model=m.get("model"), role=m.get("role", "assistant"))
            self.stream.usage.update(m.get("usage") or {})
        elif t == "content_block_start":
            b = dict(e["content_block"])
            self.partial = ""
            self.open_block(b.pop("type"), **b)
        elif t == "content_block_delta":
            d = e["delta"]
            if d["type"] == "text_delta":
                self.grow("text", d["text"])
            elif d["type"] == "thinking_delta":
                self.grow("thinking", d["thinking"])
            elif d["type"] == "signature_delta":
                self.grow("signature", d["signature"])
            elif d["type"] == "input_json_delta":
                self.partial += d["partial_json"]
                self.listener("block.delta", index=self.open["index"], field="input", delta=d["partial_json"])
        elif t == "content_block_stop":
            self.close_block()
        elif t == "message_delta":
            self.stop(e["delta"].get("stop_reason"), e.get("usage"))
        elif t == "error":
            self.stream.error = json.dumps(e.get("error"))

    def finish_block(self, block):
        if block["type"] == "tool_use" and self.partial.strip():
            block["input"] = json.loads(self.partial)


class AnthropicFormat(Format):
    name, endpoint = "anthropic", "/v1/messages"

    def client(self, url, key):
        return anthropic.Anthropic(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.messages.create, body)
        beta = {"anthropic-beta": headers["anthropic-beta"]} if headers.get("anthropic-beta") else None
        return client.messages.with_streaming_response.create(**kwargs, stream=True, extra_body=extra, extra_headers=beta)

    def reading(self, stream, listener):
        return AnthropicReading(stream, listener)

    def closing(self, reading):
        def sse(e):
            return f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
        out = []
        if not reading.stream.message:
            out.append(sse({"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant",
                                                                 "model": "", "content": [], "stop_reason": None, "stop_sequence": None,
                                                                 "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        if reading.open is not None:
            out.append(sse({"type": "content_block_stop", "index": reading.open["index"]}))
        out.append(sse({"type": "message_delta", "delta": {"stop_reason": "interrupted", "stop_sequence": None},
                        "usage": {"output_tokens": reading.stream.usage.get("output_tokens", 0)}}))
        out.append(sse({"type": "message_stop"}))
        return out

    def error_line(self, message):
        return f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'upstream_error', 'message': message}})}\n\n"


# ── OpenAI Chat Completions ──────────────────────────────────────────────────

class OpenAIReading(Reading):
    """Blocks are inferred: a run of content, of reasoning, or of one tool call's arguments."""

    STOP = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn"}

    def __init__(self, stream, listener):
        super().__init__(stream, listener)
        self.calls = {}                  # the provider's tool call index -> our block

    def take(self, c):
        if not self.stream.message and (c.get("id") or c.get("model")):
            self.start(id=c.get("id"), model=c.get("model"), role="assistant")
        if c.get("usage"):
            self.stream.usage.update({"input_tokens": c["usage"].get("prompt_tokens", 0), "output_tokens": c["usage"].get("completion_tokens", 0)})
        for choice in c.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("reasoning_content"):
                if not (self.open and self.open["type"] == "reasoning"):
                    self.open_block("reasoning", reasoning="")
                self.grow("reasoning", delta["reasoning_content"])
            if delta.get("content"):
                if not (self.open and self.open["type"] == "content"):
                    self.open_block("content", content="")
                self.grow("content", delta["content"])
            for call in delta.get("tool_calls") or []:
                i = call.get("index", 0)
                if i not in self.calls:
                    self.calls[i] = self.open_block("tool_call", call_index=i, id=call.get("id"), name=call.get("function", {}).get("name"), arguments="")
                block = self.calls[i]
                block["id"] = block["id"] or call.get("id")
                block["name"] = block["name"] or call.get("function", {}).get("name")
                self.grow("arguments", call.get("function", {}).get("arguments") or "")
            if choice.get("finish_reason"):
                self.stop(self.STOP.get(choice["finish_reason"], choice["finish_reason"]))
                self.stream.finish_reason = choice["finish_reason"]


class OpenAIFormat(Format):
    name, endpoint = "openai", "/v1/chat/completions"

    def client(self, url, key):
        return openai.OpenAI(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.chat.completions.create, body)
        return client.chat.completions.with_streaming_response.create(**kwargs, stream=True, extra_body=extra)

    def reading(self, stream, listener):
        return OpenAIReading(stream, listener)

    def closing(self, reading):
        chunk = {"id": reading.stream.message.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": reading.stream.message.get("model") or "", "interrupted": True,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        return [f"data: {json.dumps(chunk)}\n\n", "data: [DONE]\n\n"]

    def error_line(self, message):
        return f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': message}})}\n\n"


FORMATS = {"anthropic": AnthropicFormat(), "openai": OpenAIFormat()}

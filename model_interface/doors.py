"""The two doors: the Messages endpoint, behind which the Anthropic SDK reaches any provider, and the
Chat Completions endpoint, behind which the OpenAI SDK does. The caller picks the SDK by picking the door.

A door knows its SDK, how to open a streaming call with it, the envelope of its stream (which part of a
line is the head, which opens a piece, which grows one), how to compose the whole response from the
pieces, and how to end a cut stream so the caller's SDK sees a whole message. It never names what is
inside a piece.
"""
import inspect
import json
import re
import time
import uuid
from abc import ABC, abstractmethod

import anthropic
import openai

from .tap import Reading, indexed, merge, parse_json


class Door(ABC):
    name: str                  # how the record names this door
    path: str                  # the endpoint on this gateway
    stop_field: str            # the format's own word for why a response ended

    @abstractmethod
    def client(self, url: str, key: str):
        """The SDK's client for a provider's root URL."""

    @abstractmethod
    def open(self, client, body: dict, headers: dict):
        """A streaming call, as the SDK's streaming-response context manager."""

    @abstractmethod
    def reading(self, stream, listener=lambda kind, **payload: None) -> Reading: ...

    @abstractmethod
    def response(self, stream) -> dict:
        """The whole response, as a non-streaming call would have returned it, composed from the stream."""

    @abstractmethod
    def closing(self, reading: Reading) -> list[str]:
        """The stream text that ends a cut stream so the caller's SDK sees a whole message."""

    @abstractmethod
    def error_line(self, message: str) -> str:
        """One stream line carrying a failure that happened after lines were already sent."""

    @staticmethod
    def native(piece: dict) -> dict:
        """A piece as the provider would write it: without the record's `at` and times."""
        return {k: v for k, v in piece.items() if k not in ("at", "started", "ended")}

    @staticmethod
    def status_of(error):
        """A provider's refusal: (status, body), or None if the error isn't one."""
        if isinstance(error, (anthropic.APIStatusError, openai.APIStatusError)):
            body = error.body if isinstance(error.body, dict) else {"error": str(error.body or error)}
            if isinstance(error, openai.APIStatusError) and "error" not in body:
                body = {"error": body}                 # the OpenAI SDK unwraps the error object; the wire carries it wrapped
            return error.status_code, body
        return None

    @staticmethod
    def _split(create, body: dict):
        """The body as the SDK's own parameters, with anything it doesn't know passed as extra_body."""
        known = inspect.signature(create).parameters
        kwargs = {k: v for k, v in body.items() if k in known and k != "stream"}
        extra = {k: v for k, v in body.items() if k not in known}
        return kwargs, extra or None


# ── /v1/messages: the Anthropic SDK ──────────────────────────────────────────
#
# Envelope: a line with `message` is the head. A line with `content_block` and `index` opens the piece at
# content[index]. A line with `delta` and `index` grows it. A `delta` without index, or `usage`, restates
# the head. The pieces are the message's content.

class MessagesReading(Reading):
    def take(self, e):
        if "message" in e:
            self.head({k: v for k, v in e["message"].items() if k != "content"})
        elif "content_block" in e and "index" in e:
            self.open_piece(f"content[{e['index']}]", dict(e["content_block"]))
        elif "delta" in e and "index" in e:
            self.grow(f"content[{e['index']}]", {k: v for k, v in e["delta"].items() if k != "type"})
        elif "delta" in e or "usage" in e:
            self.head({**e.get("delta", {}), **({"usage": e["usage"]} if e.get("usage") else {})})
            if e.get("delta", {}).get(self.stream.door.stop_field):
                self.stop(e["delta"][self.stream.door.stop_field])
        elif e.get("type") == "content_block_stop":
            self.close_piece()
        elif e.get("type") == "error":
            self.stream.error = json.dumps(e.get("error"))

    def settle(self, piece):
        """The one convention of this door: a piece's JSON text, sent as `partial_json`, is its `input`."""
        if "partial_json" in piece:
            value = parse_json(piece["partial_json"])
            if value is not None:
                piece["input"] = value
            if piece["ended"]:
                del piece["partial_json"]


class MessagesDoor(Door):
    name, path, stop_field = "messages", "/v1/messages", "stop_reason"

    def client(self, url, key):
        return anthropic.Anthropic(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.messages.create, body)
        beta = {"anthropic-beta": headers["anthropic-beta"]} if headers.get("anthropic-beta") else None
        return client.messages.with_streaming_response.create(**kwargs, stream=True, extra_body=extra, extra_headers=beta)

    def reading(self, stream, listener=lambda kind, **payload: None):
        return MessagesReading(stream, listener)

    def response(self, stream):
        return {**stream.response, "content": [self.native(b) for b in stream.blocks]}

    def closing(self, reading):
        def sse(e):
            return f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
        out = []
        if not reading.stream.response:
            out.append(sse({"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}", "type": "message", "role": "assistant",
                                                                 "model": "", "content": [], "stop_reason": None, "stop_sequence": None,
                                                                 "usage": {"input_tokens": 0, "output_tokens": 0}}}))
        if reading.open is not None:
            out.append(sse({"type": "content_block_stop", "index": int(re.search(r"\[(\d+)\]", reading.open["at"]).group(1))}))
        out.append(sse({"type": "message_delta", "delta": {"stop_reason": "interrupted", "stop_sequence": None},
                        "usage": {"output_tokens": reading.stream.response.get("usage", {}).get("output_tokens", 0)}}))
        out.append(sse({"type": "message_stop"}))
        return out

    def error_line(self, message):
        return f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'upstream_error', 'message': message}})}\n\n"


# ── /v1/chat/completions: the OpenAI SDK ─────────────────────────────────────
#
# Envelope: every chunk's top-level fields restate the head, minus the padding some providers add to every
# chunk. Each choices[i].delta grows choices[i].message, and every key in it grows a piece at that key, one
# per index for a list of indexed objects. The other fields of a choice are set.

class ChatReading(Reading):
    PADDING = ("obfuscation",)

    def take(self, c):
        self.head({k: v for k, v in c.items() if k != "choices" and k not in self.PADDING})
        for choice in c.get("choices") or []:
            i = choice.get("index", 0)
            choices = self.stream.response.setdefault("choices", [])
            while len(choices) <= i:
                choices.append({"index": len(choices), "message": {}})
            merge(choices[i], {k: v for k, v in choice.items() if k != "delta"}, grow=False)
            delta = choice.get("delta") or {}
            merge(choices[i]["message"], delta)
            for key, value in delta.items():
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, list) and indexed(value):
                    for item in value:
                        self.grow(f"choices[{i}].message.{key}[{item['index']}]", item)
                else:
                    self.grow(f"choices[{i}].message.{key}", {key: value})
            if choice.get("finish_reason"):
                self.stop(choice["finish_reason"])

    def stop(self, reason):
        self.close_piece()
        choices = self.stream.response.setdefault("choices", [{"index": 0, "message": {}}])
        choices[0]["finish_reason"] = reason


class ChatDoor(Door):
    name, path, stop_field = "chat", "/v1/chat/completions", "finish_reason"

    def client(self, url, key):
        root = url.rstrip("/")
        return openai.OpenAI(base_url=root if root.endswith("/v1") else root + "/v1", api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.chat.completions.create, body)
        return client.chat.completions.with_streaming_response.create(**kwargs, stream=True, extra_body=extra)

    def reading(self, stream, listener=lambda kind, **payload: None):
        return ChatReading(stream, listener)

    def response(self, stream):
        r = {k: v for k, v in stream.response.items() if k != self.stop_field}
        r["object"] = "chat.completion"
        return {**r, "blocks": [dict(b) for b in stream.blocks]}

    def closing(self, reading):
        chunk = {"id": reading.stream.response.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": reading.stream.response.get("model") or "", "interrupted": True,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        return [f"data: {json.dumps(chunk)}\n\n", "data: [DONE]\n\n"]

    def error_line(self, message):
        return f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': message}})}\n\n"


DOORS = {d.name: d for d in (MessagesDoor(), ChatDoor())}

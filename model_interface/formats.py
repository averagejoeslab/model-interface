"""The two wire formats, each passed through by its own SDK, each read by one merge that never looks at a name.

A Format knows how to open a streaming call with the native SDK, and the envelope of its stream: which
part of a line is the response's head, which part opens a piece, which part grows one. What is inside a
piece is never named: a piece is whatever object the provider sent, grown by whatever deltas followed,
by shape alone. Strings grow, dicts merge, lists of indexed objects merge by index, anything else is set.
"""
import copy
import inspect
import json
import re
import time
import uuid
from abc import ABC, abstractmethod

import anthropic
import openai
from jiter import from_json


# ── the merge: one rule for every key, known or not ──────────────────────────

def merge(acc: dict, delta: dict, grow: bool = True) -> dict:
    """Fold `delta` into `acc` by the shape of each value, never by the name of its key.

    grow=True is for the pieces of a stream: strings are appended, lists extend, nothing is replaced by null, and
    a piece's `type` is its identity and is set once. grow=False is for a head that is restated: values are set.
    A list of objects that carry an `index` merges entry by entry, in both modes. Values are copied in, never
    shared, so a delta can be folded into two places without the two growing each other.
    """
    for k, v in delta.items():
        a = acc.get(k)
        if grow and a is not None and (v is None or k == "type"):
            continue
        if grow and isinstance(a, str) and isinstance(v, str):
            acc[k] = a + v
        elif isinstance(a, dict) and isinstance(v, dict):
            merge(a, v, grow)
        elif isinstance(a, list) and isinstance(v, list) and indexed(v):
            for item in v:
                while len(a) <= item["index"]:
                    a.append({})
                merge(a[item["index"]], item, grow)
        elif grow and isinstance(a, list) and isinstance(v, list):
            a.extend(copy.deepcopy(v))
        else:
            acc[k] = copy.deepcopy(v)
    return acc


def indexed(items: list) -> bool:
    """Whether a list is of objects that each say where they go."""
    return bool(items) and all(isinstance(x, dict) and "index" in x for x in items)


def parse_json(text: str):
    """JSON text as a value: whole when it is whole, as far as it got when it is not, or None when it is nothing yet."""
    try:
        return json.loads(text)
    except ValueError:
        try:
            return from_json(text.encode(), partial_mode="trailing-strings")
        except ValueError:
            return None


def data_of(line: str):
    """The JSON of a data line, or None for anything else (event names, blanks, [DONE])."""
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload and payload != "[DONE]":
            return json.loads(payload)
    return None


# ── the reading: the record of one stream, built as its lines pass ───────────

class Reading:
    """Reads a stream's lines into a Stream record, piece by piece, telling a listener as it goes.

    The record is the live object: a piece appears when it opens, grows with each delta, and gets its end time
    when it closes. Every piece carries `at`, where it lives in the response, and `started`/`ended`. Nothing
    else is added, and nothing the provider sent is renamed or dropped.
    """

    def __init__(self, stream, listener=lambda kind, **payload: None):
        self.stream = stream
        self.listener = listener
        self.open = None                 # the piece that hasn't closed yet, or None

    def feed(self, line: str):
        data = data_of(line)
        if data is not None:
            self.take(data)

    def take(self, data: dict): ...

    def head(self, fields: dict):
        """What the provider says about the response outside its pieces; restated fields are set, not grown.

        Only what changed is told: a format that repeats its head on every line is not news on every line.
        """
        changed = {k: v for k, v in fields.items() if k not in self.stream.response or self.stream.response[k] != v}
        if changed:
            merge(self.stream.response, changed, grow=False)
            self.listener("stream.response", response=changed)

    def open_piece(self, at: str, piece: dict):
        self.close_piece()
        block = {"at": at, "started": time.time(), "ended": None, **piece}
        self.stream.blocks.append(block)
        self.open = block
        self.listener("block.started", at=at, block=block)
        return block

    def grow(self, at: str, delta: dict):
        """A delta for the piece at `at`: opens it if it is not the open one, then grows it by shape."""
        if self.open is None or self.open["at"] != at:
            self.open_piece(at, {})
        merge(self.open, delta)
        self.settle(self.open)
        self.listener("block.delta", at=at, delta=delta)

    def settle(self, piece: dict):
        """Anything the format derives from a piece's own fields, kept current as it grows."""

    def close_piece(self):
        if self.open is None:
            return
        self.open["ended"] = time.time()
        self.settle(self.open)
        self.listener("block.ended", at=self.open["at"], block=self.open)
        self.open = None

    def stop(self, reason):
        """The stream's end: the format's own word, set on the head."""
        self.close_piece()
        self.stream.response[self.stream.model.format.stop_field] = reason


class Format(ABC):
    name: str
    endpoint: str              # the path on this gateway
    path: str                  # what the SDK appends to its base_url; the provider's endpoint ends with it
    stop_field: str            # the format's own word for why a response ended

    @abstractmethod
    def client(self, url: str, key: str): ...

    @abstractmethod
    def open(self, client, body: dict, headers: dict):
        """A streaming call, as the SDK's streaming-response context manager."""

    @abstractmethod
    def reading(self, stream, listener=lambda kind, **payload: None) -> Reading: ...

    @staticmethod
    def native(block: dict) -> dict:
        """A piece as the provider would write it: without the record's `at` and times."""
        return {k: v for k, v in block.items() if k not in ("at", "started", "ended")}

    @abstractmethod
    def response(self, stream) -> dict:
        """The whole response in this format, as a non-streaming call would have returned it, assembled from the stream."""

    @abstractmethod
    def closing(self, reading: Reading) -> list[str]:
        """The stream text that ends a cut stream so the caller's SDK sees a whole message."""

    @abstractmethod
    def error_line(self, message: str) -> str:
        """One stream line carrying a failure that happened after lines were already sent."""

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


# ── Anthropic Messages ───────────────────────────────────────────────────────
#
# The envelope: a line with `message` is the head. A line with `content_block` and `index` opens the piece at
# content[index]. A line with `delta` and `index` grows that piece. A line with `delta` and no index, or with
# `usage`, restates the head. Pieces are the message's content.

class AnthropicReading(Reading):
    def take(self, e):
        if "message" in e:
            self.head({k: v for k, v in e["message"].items() if k != "content"})   # the content is the pieces
        elif "content_block" in e and "index" in e:
            self.open_piece(f"content[{e['index']}]", dict(e["content_block"]))
        elif "delta" in e and "index" in e:
            self.grow(f"content[{e['index']}]", {k: v for k, v in e["delta"].items() if k != "type"})   # the delta's type names the delta, not the piece
        elif "delta" in e or "usage" in e:
            self.head({**e.get("delta", {}), **({"usage": e["usage"]} if e.get("usage") else {})})
            if e.get("delta", {}).get(self.stream.model.format.stop_field):
                self.stop(e["delta"][self.stream.model.format.stop_field])
        elif e.get("type") == "content_block_stop":
            self.close_piece()
        elif e.get("type") == "error":
            self.stream.error = json.dumps(e.get("error"))

    def settle(self, piece):
        """The one convention of this format: a piece's JSON text, sent as `partial_json`, is its `input`."""
        if "partial_json" in piece:
            value = parse_json(piece["partial_json"])
            if value is not None:
                piece["input"] = value
            if piece["ended"]:
                del piece["partial_json"]


class AnthropicFormat(Format):
    name, endpoint, path, stop_field = "anthropic", "/v1/messages", "/v1/messages", "stop_reason"

    def client(self, url, key):
        return anthropic.Anthropic(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.messages.create, body)
        beta = {"anthropic-beta": headers["anthropic-beta"]} if headers.get("anthropic-beta") else None
        return client.messages.with_streaming_response.create(**kwargs, stream=True, extra_body=extra, extra_headers=beta)

    def reading(self, stream, listener=lambda kind, **payload: None):
        return AnthropicReading(stream, listener)

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


# ── OpenAI Chat Completions ──────────────────────────────────────────────────
#
# The envelope: every chunk's top-level fields restate the head. Each choices[i].delta grows choices[i].message,
# and every key in it grows a piece at that key, one per index for a list of indexed objects. The other fields of
# a choice are set. Pieces are the deltas as they came; the message is the same deltas merged.

class OpenAIReading(Reading):
    def take(self, c):
        self.head({k: v for k, v in c.items() if k != "choices"})
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
        """This format's end lives on the choice, not on the head."""
        self.close_piece()
        choices = self.stream.response.setdefault("choices", [{"index": 0, "message": {}}])
        choices[0]["finish_reason"] = reason


class OpenAIFormat(Format):
    name, endpoint, path, stop_field = "openai", "/v1/chat/completions", "/chat/completions", "finish_reason"

    def client(self, url, key):
        return openai.OpenAI(base_url=url, api_key=key or "none", timeout=600)

    def open(self, client, body, headers):
        kwargs, extra = self._split(client.chat.completions.create, body)
        return client.chat.completions.with_streaming_response.create(**kwargs, stream=True, extra_body=extra)

    def reading(self, stream, listener=lambda kind, **payload: None):
        return OpenAIReading(stream, listener)

    def response(self, stream):
        r = {k: v for k, v in stream.response.items() if k not in ("obfuscation", self.stop_field)}
        r["object"] = "chat.completion"
        return {**r, "blocks": [dict(b) for b in stream.blocks]}

    def closing(self, reading):
        chunk = {"id": reading.stream.response.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": reading.stream.response.get("model") or "", "interrupted": True,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        return [f"data: {json.dumps(chunk)}\n\n", "data: [DONE]\n\n"]

    def error_line(self, message):
        return f"data: {json.dumps({'error': {'type': 'upstream_error', 'message': message}})}\n\n"


FORMATS = {"anthropic": AnthropicFormat(), "openai": OpenAIFormat()}

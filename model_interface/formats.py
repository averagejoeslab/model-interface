"""Wire formats.

The canonical shape inside the gateway is Anthropic's: the Messages request, and the streaming
events (message_start, content_block_start, content_block_delta, content_block_stop,
message_delta, message_stop). Each Format converts between that and one wire format, in both
directions, so one class serves a provider that speaks the format and a caller that speaks it.
"""
import json
import time
import uuid
from abc import ABC, abstractmethod


def text_of(content) -> str:
    """The text in a content field, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


def sse(lines):
    """The data payloads of a server-sent event stream."""
    for line in lines:
        if line.startswith("data:"):
            yield line[5:].strip()


def assemble(events) -> dict:
    """A whole message, from its events. For callers that didn't ask for a stream."""
    message, blocks, partial = {"content": [], "stop_reason": None, "usage": {}}, {}, {}
    for e in events:
        t = e["type"]
        if t == "message_start":
            message.update(e["message"])
        elif t == "content_block_start":
            blocks[e["index"]], partial[e["index"]] = dict(e["content_block"]), ""
        elif t == "content_block_delta":
            d, b = e["delta"], blocks[e["index"]]
            if d["type"] == "text_delta":
                b["text"] = b.get("text", "") + d["text"]
            elif d["type"] == "input_json_delta":
                partial[e["index"]] += d["partial_json"]
            elif d["type"] == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d["thinking"]
            elif d["type"] == "signature_delta":
                b["signature"] = d["signature"]
        elif t == "content_block_stop":
            b = blocks[e["index"]]
            if b["type"] == "tool_use" and partial[e["index"]].strip():
                b["input"] = json.loads(partial[e["index"]])
        elif t == "message_delta":
            message.update(e["delta"])
            message["usage"] = {**message.get("usage", {}), **e.get("usage", {})}
        elif t == "error":
            message["error"] = e["error"]
    message["content"] = [blocks[i] for i in sorted(blocks)]
    return message


class Format(ABC):
    name: str
    endpoint: str  # the path on a provider that speaks this format

    @abstractmethod
    def auth(self, key: str) -> dict: ...

    @abstractmethod
    def request_in(self, body: dict) -> dict:
        """From a caller: wire -> canonical."""

    @abstractmethod
    def request_out(self, request: dict) -> dict:
        """To a provider: canonical -> wire, always streaming."""

    @abstractmethod
    def events_in(self, lines):
        """From a provider: its stream's lines -> canonical events."""

    @abstractmethod
    def events_out(self, events):
        """To a caller: canonical events -> its stream's text."""

    @abstractmethod
    def response_out(self, message: dict) -> dict:
        """To a caller that didn't ask for a stream: canonical message -> wire."""


class AnthropicFormat(Format):
    """The canonical format itself: nothing to convert."""

    name, endpoint = "anthropic", "/v1/messages"

    def auth(self, key):
        return {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}

    def request_in(self, body):
        return {k: v for k, v in body.items() if k != "stream"}

    def request_out(self, request):
        return {**request, "stream": True}

    def events_in(self, lines):
        for data in sse(lines):
            yield json.loads(data)

    def events_out(self, events):
        for e in events:
            yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"

    def response_out(self, message):
        return message


class OpenAIFormat(Format):
    """Chat Completions, as OpenAI and most other providers speak it."""

    name, endpoint = "openai", "/chat/completions"
    STOP = {"tool_calls": "tool_use", "length": "max_tokens"}
    FINISH = {"tool_use": "tool_calls", "max_tokens": "length"}

    def auth(self, key):
        return {"authorization": f"Bearer {key}"} if key else {}  # a local provider may need none

    # ── requests ─────────────────────────────────────────────────────────────

    def request_in(self, body):
        request = {k: body[k] for k in ("model", "max_tokens", "temperature") if k in body}
        request.setdefault("max_tokens", 4096)
        system = [text_of(m["content"]) for m in body["messages"] if m["role"] in ("system", "developer")]
        if system:
            request["system"] = "\n\n".join(system)
        if body.get("tools"):
            request["tools"] = [self._tool_in(t["function"]) for t in body["tools"]]
        messages = []
        for m in body["messages"]:
            if m["role"] in ("system", "developer"):
                continue
            if m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": text_of(m["content"])}
                if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
            elif m["role"] == "assistant":
                messages.append({"role": "assistant", "content": self._assistant_in(m)})
            else:
                messages.append({"role": "user", "content": text_of(m["content"])})
        request["messages"] = messages
        return request

    def request_out(self, request):
        messages = [{"role": "system", "content": text_of(request["system"])}] if request.get("system") else []
        for m in request["messages"]:
            if isinstance(m["content"], str):
                messages.append({"role": m["role"], "content": m["content"]})
            elif m["role"] == "user":
                results = [b for b in m["content"] if b["type"] == "tool_result"]
                messages += [{"role": "tool", "tool_call_id": b["tool_use_id"], "content": text_of(b["content"])} for b in results]
                if text := text_of(m["content"]):
                    messages.append({"role": "user", "content": text})
            else:
                messages.append(self._assistant_out(m["content"]))
        body = {"model": request["model"], "messages": messages, "max_tokens": request["max_tokens"],
                "stream": True, "stream_options": {"include_usage": True}}
        if "temperature" in request:
            body["temperature"] = request["temperature"]
        if request.get("tools"):
            body["tools"] = [self._tool_out(t) for t in request["tools"]]
        return body

    # ── streams ──────────────────────────────────────────────────────────────

    def events_in(self, lines):
        """Chat Completions chunks -> canonical events. Blocks are opened as deltas arrive."""
        index, open_block, calls, stop, usage, started = -1, None, {}, None, None, False
        for data in sse(lines):
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if not started:
                started = True
                yield {"type": "message_start", "message": {"id": chunk.get("id") or f"msg_{uuid.uuid4().hex}", "type": "message",
                       "role": "assistant", "model": chunk.get("model", ""), "content": [], "stop_reason": None,
                       "usage": {"input_tokens": 0, "output_tokens": 0}}}
            if chunk.get("usage"):
                usage = {"input_tokens": chunk["usage"].get("prompt_tokens", 0), "output_tokens": chunk["usage"].get("completion_tokens", 0)}
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    if open_block != "text":
                        if open_block:
                            yield {"type": "content_block_stop", "index": index}
                        index, open_block = index + 1, "text"
                        yield {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}
                    yield {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": delta["content"]}}
                for call in delta.get("tool_calls") or []:
                    if call.get("index") not in calls:
                        if open_block:
                            yield {"type": "content_block_stop", "index": index}
                        index, open_block = index + 1, "tool_use"
                        calls[call.get("index")] = index
                        yield {"type": "content_block_start", "index": index, "content_block": {
                            "type": "tool_use", "id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}", "name": call["function"]["name"], "input": {}}}
                    if call.get("function", {}).get("arguments"):
                        yield {"type": "content_block_delta", "index": calls[call.get("index")],
                               "delta": {"type": "input_json_delta", "partial_json": call["function"]["arguments"]}}
                if choice.get("finish_reason"):
                    stop = self.STOP.get(choice["finish_reason"], "end_turn")
        if open_block:
            yield {"type": "content_block_stop", "index": index}
        if started:
            yield {"type": "message_delta", "delta": {"stop_reason": stop or "end_turn", "stop_sequence": None},
                   "usage": usage or {"output_tokens": 0}}
            yield {"type": "message_stop"}

    def events_out(self, events):
        """Canonical events -> Chat Completions chunks."""
        head, calls = {"id": "", "object": "chat.completion.chunk", "created": int(time.time()), "model": ""}, {}

        def chunk(delta=None, finish=None, **extra):
            choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
            return "data: " + json.dumps({**head, "choices": [choice], **extra}) + "\n\n"

        for e in events:
            t = e["type"]
            if t == "message_start":
                head.update(id=e["message"]["id"], model=e["message"].get("model", ""))
                yield chunk({"role": "assistant", "content": ""})
            elif t == "content_block_start" and e["content_block"]["type"] == "tool_use":
                calls[e["index"]] = len(calls)
                b = e["content_block"]
                yield chunk({"tool_calls": [{"index": calls[e["index"]], "id": b["id"], "type": "function",
                                             "function": {"name": b["name"], "arguments": ""}}]})
            elif t == "content_block_delta" and e["delta"]["type"] == "text_delta":
                yield chunk({"content": e["delta"]["text"]})
            elif t == "content_block_delta" and e["delta"]["type"] == "input_json_delta":
                yield chunk({"tool_calls": [{"index": calls[e["index"]], "function": {"arguments": e["delta"]["partial_json"]}}]})
            elif t == "message_delta":
                stop = e["delta"].get("stop_reason")
                usage = e.get("usage", {})
                yield chunk(finish=self.FINISH.get(stop, "stop"), **({"interrupted": True} if stop == "interrupted" else {}),
                            usage={"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                                   "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)})
            elif t == "error":
                yield "data: " + json.dumps({"error": e["error"]}) + "\n\n"
            elif t == "message_stop":
                yield "data: [DONE]\n\n"

    # ── whole responses ──────────────────────────────────────────────────────

    def response_out(self, message):
        usage = message.get("usage", {})
        stop = message.get("stop_reason")
        return {
            "id": message.get("id", ""), "object": "chat.completion", "created": int(time.time()), "model": message.get("model", ""),
            "choices": [{"index": 0, "message": self._assistant_out(message["content"]), "finish_reason": self.FINISH.get(stop, "stop")}],
            **({"interrupted": True} if stop == "interrupted" else {}),
            "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
                      "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
        }

    # ── the pieces, each used in two directions ──────────────────────────────

    @staticmethod
    def _tool_in(function):
        return {"name": function["name"], "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}}}

    @staticmethod
    def _tool_out(tool):
        return {"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""),
                                                 "parameters": tool.get("input_schema") or {"type": "object", "properties": {}}}}

    @staticmethod
    def _assistant_in(message):
        blocks = [{"type": "text", "text": text_of(message["content"])}] if message.get("content") else []
        blocks += [{"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                    "input": json.loads(c["function"]["arguments"] or "{}")} for c in message.get("tool_calls") or []]
        return blocks

    @staticmethod
    def _assistant_out(blocks):
        message = {"role": "assistant", "content": text_of(blocks) or None}
        calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                 for b in blocks if b["type"] == "tool_use"]
        if calls:
            message["tool_calls"] = calls
        return message


FORMATS = {"anthropic": AnthropicFormat(), "openai": OpenAIFormat()}

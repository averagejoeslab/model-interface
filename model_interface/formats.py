"""Wire formats.

The canonical shape inside the platform is Anthropic's Messages format. Each Format converts
between it and one wire format, in both directions, so the same class serves a provider that
speaks that format and a caller that speaks it.
"""
import json
import time
import uuid
from abc import ABC, abstractmethod

PLATFORM_USE = "platform_tool_use"        # a tool call the platform ran, folded into assistant content
PLATFORM_RESULT = "platform_tool_result"  # what it returned, folded in right after it


def text_of(content) -> str:
    """The text in a content field, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


class Format(ABC):
    endpoint: str  # the path on a provider that speaks this format

    @abstractmethod
    def auth(self, key: str) -> dict: ...

    @abstractmethod
    def request_in(self, body: dict) -> dict:
        """From a caller: wire -> canonical."""

    @abstractmethod
    def request_out(self, request: dict) -> dict:
        """To a provider: canonical -> wire."""

    @abstractmethod
    def response_in(self, body: dict) -> dict:
        """From a provider: wire -> canonical."""

    @abstractmethod
    def response_out(self, response: dict) -> dict:
        """To a caller: canonical -> wire."""


class AnthropicFormat(Format):
    """The canonical format itself: nothing to convert."""

    endpoint = "/v1/messages"

    def auth(self, key):
        return {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}

    def request_in(self, body):
        return body

    def request_out(self, request):
        return request

    def response_in(self, body):
        return body

    def response_out(self, response):
        return response


class OpenAIFormat(Format):
    """Chat Completions, as OpenAI and most other providers speak it."""

    endpoint = "/chat/completions"

    def auth(self, key):
        return {"authorization": f"Bearer {key}"} if key else {}  # a local provider may need none

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
                messages.append({"role": "assistant", "content": m.get("platform") or self._assistant_in(m)})
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
        body = {"model": request["model"], "messages": messages, "max_tokens": request["max_tokens"]}
        if "temperature" in request:
            body["temperature"] = request["temperature"]
        if request.get("tools"):
            body["tools"] = [self._tool_out(t) for t in request["tools"]]
        return body

    def response_in(self, body):
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        return {
            "id": body.get("id") or f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", ""),
            "content": self._assistant_in(choice["message"]),
            "stop_reason": {"tool_calls": "tool_use", "length": "max_tokens"}.get(choice.get("finish_reason"), "end_turn"),
            "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
        }

    def response_out(self, response):
        message = self._assistant_out(response["content"])
        if any(b["type"] in (PLATFORM_USE, PLATFORM_RESULT) for b in response["content"]):
            message["platform"] = response["content"]  # what ran here; sent back as is, it keeps the transcript whole
        usage = response["usage"]
        return {
            "id": response["id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": response["model"],
            "choices": [{"index": 0, "message": message,
                         "finish_reason": {"tool_use": "tool_calls", "max_tokens": "length"}.get(response["stop_reason"], "stop")}],
            "usage": {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"],
                      "total_tokens": usage["input_tokens"] + usage["output_tokens"]},
        }

    # ── the pieces, each used in two directions ─────────────────────────────

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
        """An assistant message's content and tool_calls, as blocks."""
        blocks = [{"type": "text", "text": text_of(message["content"])}] if message.get("content") else []
        blocks += [{"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                    "input": json.loads(c["function"]["arguments"] or "{}")} for c in message.get("tool_calls") or []]
        return blocks

    @staticmethod
    def _assistant_out(blocks):
        """Blocks, as an assistant message with content and tool_calls."""
        message = {"role": "assistant", "content": text_of(blocks) or None}
        calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                 for b in blocks if b["type"] == "tool_use"]
        if calls:
            message["tool_calls"] = calls
        return message


FORMATS = {"anthropic": AnthropicFormat(), "openai": OpenAIFormat()}

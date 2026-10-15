"""The caller's transcript, and the provider's.

The caller keeps the conversation and sends it whole every time. What the platform ran is folded
into assistant content as platform_tool_use and platform_tool_result blocks, so one request gets one
response. Providers need the real shape: an assistant turn of tool calls, then a user turn of results.
"""
from .formats import PLATFORM_RESULT, PLATFORM_USE


def unfold(messages: list) -> list:
    """Folded assistant content, as the turns a provider expects."""
    out = []
    for m in messages:
        if m["role"] != "assistant" or isinstance(m["content"], str):
            out.append(m)
            continue
        turn, results = [], []
        for block in m["content"]:
            if block["type"] == PLATFORM_RESULT:
                results.append({**block, "type": "tool_result"})
                continue
            if results:  # a new assistant turn starts: flush the one before it, and its results
                out += [{"role": "assistant", "content": turn}, {"role": "user", "content": results}]
                turn, results = [], []
            turn.append({**block, "type": "tool_use"} if block["type"] == PLATFORM_USE else block)
        if turn:
            out.append({"role": "assistant", "content": turn})
        if results:
            out.append({"role": "user", "content": results})
    return merged(out)


def blocks(content) -> list:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content


def merged(messages: list) -> list:
    """Consecutive user turns, as one: a tool result and the person's next message often land side by side."""
    out = []
    for m in messages:
        if out and m["role"] == "user" == out[-1]["role"]:
            out[-1] = {"role": "user", "content": blocks(out[-1]["content"]) + blocks(m["content"])}
        else:
            out.append(m)
    return out

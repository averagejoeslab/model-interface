"""The tap and the doors, proved against real wire captures: nothing named, nothing dropped."""
import json
import pathlib

import pytest

from model_interface.doors import DOORS
from model_interface.record import Stream
from model_interface.tap import data_of, merge, parse_json, strings_of

messages, chat = DOORS["messages"], DOORS["chat"]
FIXTURES = pathlib.Path(__file__).with_name("fixtures")
CAPTURES = [("anthropic-messages", "messages"), ("anthropic-messages-tool-only", "messages"),
            ("openai-chat", "chat"), ("anthropic-compat-chat", "chat"), ("ollama-chat", "chat")]


def fresh(door):
    return Stream(agent="k", session="s", door=DOORS[door], provider="p", model="m", request={})


def read(door, lines, told=None):
    stream = fresh(door)
    r = DOORS[door].reading(stream, lambda kind, **p: told.append((kind, p)) if told is not None else None)
    for line in lines:
        r.feed(line)
    r.close_piece()
    return stream


def sse(e):
    return f"data: {json.dumps(e)}"


# ── the merge ─────────────────────────────────────────────────────────────────

def test_the_merge_goes_by_shape_not_by_name():
    acc = merge({}, {"anything": "Hel", "n": 1, "o": {"deep": "a"}, "items": [{"index": 0, "s": "x"}], "type": "first"})
    merge(acc, {"anything": "lo", "n": 2, "o": {"deep": "b", "new": True}, "items": [{"index": 1, "s": "y"}, {"index": 0, "s": "z"}], "type": "second", "nothing": None})
    assert acc == {"anything": "Hello", "n": 2, "o": {"deep": "ab", "new": True}, "items": [{"index": 0, "s": "xz"}, {"index": 1, "s": "y"}], "type": "first", "nothing": None}
    assert merge({"content": "kept"}, {"content": None}) == {"content": "kept"}
    assert merge({"model": "gpt-5", "usage": {"a": 1}}, {"model": "gpt-5", "usage": {"b": 2}}, grow=False) == {"model": "gpt-5", "usage": {"a": 1, "b": 2}}
    delta = {"function": {"arguments": "x"}}
    a, b = merge({}, delta), merge({}, delta)
    merge(a, {"function": {"arguments": "y"}})
    assert b["function"]["arguments"] == "x", "values are copied in, never shared"


def test_json_text_and_strings():
    assert parse_json('{"cmd": "rm -rf /tm') == {"cmd": "rm -rf /tm"} and parse_json("") is None
    assert data_of('data: {"a": 1}') == {"a": 1} and data_of("event: ping") is None and data_of("data: [DONE]") is None
    assert strings_of({"a": "x", "b": [{"c": "y"}, 3], "d": None}) == "x\ny"


# ── the doors, against the real captures ──────────────────────────────────────

@pytest.mark.parametrize("name,door", CAPTURES)
def test_the_response_equals_what_the_providers_own_sdk_assembles(name, door):
    lines = (FIXTURES / f"{name}.sse").read_text().split("\n")
    stream = read(door, lines)
    ours = DOORS[door].response(stream)
    if door == "messages":
        from anthropic._models import construct_type
        from anthropic.types import RawMessageStreamEvent
        from anthropic.lib.streaming._messages import accumulate_event
        snapshot, bufs = None, {}
        for line in lines:
            d = data_of(line)
            if d and d.get("type") != "ping":
                snapshot = accumulate_event(event=construct_type(type_=RawMessageStreamEvent, value=d), current_snapshot=snapshot, json_bufs=bufs)
        theirs = snapshot.to_dict()
    else:
        from openai._models import construct_type
        from openai.types.chat import ChatCompletionChunk
        from openai.lib.streaming.chat._completions import ChatCompletionStreamState
        state = ChatCompletionStreamState()
        for line in lines:
            d = data_of(line)
            if d:
                list(state.handle_chunk(construct_type(type_=ChatCompletionChunk, value=d)))
        theirs = state.get_final_completion().to_dict()
        ours.pop("blocks")
        for choice in theirs["choices"]:
            m, our_m = choice["message"], ours["choices"][choice["index"]]["message"]
            for k in ("annotations", "audio", "function_call", "refusal", "parsed"):
                if k not in our_m:
                    m.pop(k, None)
            for call in m.get("tool_calls") or []:
                call["function"].pop("parsed_arguments", None)
        if theirs.get("system_fingerprint") is None and "system_fingerprint" not in ours:
            theirs.pop("system_fingerprint")
    extras = {k: ours.pop(k) for k in list(ours) if k not in theirs}
    assert set(extras) <= {"timings"}
    assert ours == theirs


def test_a_messages_stream_becomes_pieces_at_their_place_with_times():
    told = []
    stream = read("messages", (FIXTURES / "anthropic-messages.sse").read_text().split("\n"), told)
    assert [b["at"] for b in stream.blocks] == ["content[0]", "content[1]", "content[2]"]
    assert [b["type"] for b in stream.blocks] == ["text", "tool_use", "tool_use"]
    text, tool = stream.blocks[0], stream.blocks[1]
    assert text["started"] <= text["ended"] <= tool["started"] <= tool["ended"]
    assert tool["input"] == {"cmd": "ls -la; git status --short | head -20"} and "partial_json" not in tool and tool["caller"] == {"type": "direct"}
    assert stream.response["stop_reason"] == "tool_use" and stream.response["usage"]["output_tokens"] == 179
    deltas = [p for k, p in told if k == "block.delta"]
    assert deltas[0] == {"at": "content[0]", "delta": {"text": "I'll start by looking at"}}


def test_a_tool_input_is_readable_while_it_is_still_arriving():
    stream = fresh("messages")
    r = messages.reading(stream)
    r.feed(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "shell", "input": {}}}))
    r.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "rm -r'}}))
    assert stream.blocks[0]["input"] == {"cmd": "rm -r"}
    r.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'f /"}'}}))
    r.close_piece()
    assert stream.blocks[0]["input"] == {"cmd": "rm -rf /"} and "partial_json" not in stream.blocks[0]


def test_a_chat_stream_becomes_pieces_at_their_place_with_times():
    told = []
    stream = read("chat", (FIXTURES / "openai-chat.sse").read_text().split("\n"), told)
    assert [b["at"] for b in stream.blocks] == ["choices[0].message.role", "choices[0].message.content", "choices[0].message.tool_calls[0]"]
    role, content, call = stream.blocks
    assert call["function"] == {"name": "shell", "arguments": '{"cmd":"bash -lc \\"ls -la\\""}'} and call["index"] == 0
    assert content["ended"] <= call["started"]
    assert stream.response["choices"][0]["finish_reason"] == "tool_calls" and stream.response["service_tier"] == "default"
    heads = [p["response"] for k, p in told if k == "stream.response"]
    assert len(heads) == 2 and "usage" in heads[1] and not any("obfuscation" in h for h in heads)


# ── things no door has seen before ────────────────────────────────────────────

def test_an_unknown_block_type_and_delta_type_are_kept_whole_in_messages():
    stream = read("messages", [sse(e) for e in [
        {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "model": "future", "content": [],
                                              "usage": {"input_tokens": 5, "new_counter": 7}, "brand_new_field": {"k": "v"}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "video", "video": "", "codec": "av1"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "video": "AAAA"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "video": "BBBB", "frames": [1]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "frames": [2]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "new_delta_field": "x"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"}]])
    r = messages.response(stream)
    assert r["content"] == [{"type": "video", "video": "AAAABBBB", "codec": "av1", "frames": [1, 2]}]
    assert r["brand_new_field"] == {"k": "v"} and r["new_delta_field"] == "x" and r["usage"] == {"input_tokens": 5, "new_counter": 7, "output_tokens": 9}


def test_an_unknown_streamed_field_and_indexed_list_are_kept_whole_in_chat():
    head = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "future", "new_chunk_field": "nc"}
    stream = read("chat", [sse(c) for c in [
        {**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"brand_new_text": "part one, "}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"brand_new_text": "part two"}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"citations": [{"index": 0, "url": "https://a"}]}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**head, "choices": [], "usage": {"prompt_tokens": 5}, "timings": {"ms": 12}}]])
    r = chat.response(stream)
    m = r["choices"][0]["message"]
    assert m["brand_new_text"] == "part one, part two" and m["citations"] == [{"index": 0, "url": "https://a"}]
    assert r["new_chunk_field"] == "nc" and r["timings"] == {"ms": 12}
    assert [b["at"] for b in r["blocks"]] == ["choices[0].message.role", "choices[0].message.brand_new_text", "choices[0].message.citations[0]"]


# ── ending a cut stream ────────────────────────────────────────────────────────

def test_closing_a_messages_stream_leaves_a_whole_message():
    stream = fresh("messages")
    r = messages.reading(stream)
    r.feed('data: {"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {}}}')
    r.feed('data: {"type": "content_block_start", "index": 2, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}')
    events = [json.loads(s.split("data: ")[1]) for s in messages.closing(r)]
    assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[0]["index"] == 2 and events[1]["delta"]["stop_reason"] == "interrupted"


def test_closing_a_chat_stream_marks_the_chunk_and_ends():
    out = chat.closing(chat.reading(fresh("chat")))
    assert json.loads(out[0][6:])["interrupted"] is True and out[1] == "data: [DONE]\n\n"


def test_the_body_is_split_into_what_the_sdk_knows_and_extra_body():
    import anthropic as sdk
    client = sdk.Anthropic(api_key="x", base_url="http://127.0.0.1:1")
    kwargs, extra = messages._split(client.messages.create, {"model": "m", "max_tokens": 1, "messages": [], "stream": True, "brand_new_field": {"x": 1}})
    assert kwargs == {"model": "m", "max_tokens": 1, "messages": []} and extra == {"brand_new_field": {"x": 1}}


def test_the_chat_door_gives_the_openai_sdk_the_root_plus_v1():
    assert str(chat.client("https://api.groq.com/openai", "k").base_url) == "https://api.groq.com/openai/v1/"
    assert str(chat.client("https://api.openai.com/v1", "k").base_url) == "https://api.openai.com/v1/"
    assert str(messages.client("https://api.anthropic.com", "k").base_url) == "https://api.anthropic.com"

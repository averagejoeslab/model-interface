"""The merge, the readings, and the proof against real wire captures: nothing named, nothing dropped."""
import json
import pathlib

import pytest

from model_interface.formats import FORMATS, data_of, merge, parse_json
from model_interface.gateway import Stream
from model_interface.providers import Provider

anthropic, openai = FORMATS["anthropic"], FORMATS["openai"]
PROVIDERS = {"anthropic": Provider("a", "http://127.0.0.1:1/v1/messages", "", ["m"]),
             "openai": Provider("o", "http://127.0.0.1:1/v1/chat/completions", "", ["m"])}
FIXTURES = pathlib.Path(__file__).with_name("fixtures")
CAPTURES = [("anthropic-messages", "anthropic"), ("anthropic-messages-tool-only", "anthropic"),
            ("openai-chat", "openai"), ("anthropic-compat-chat", "openai"), ("ollama-chat", "openai")]


def fresh(format):
    return Stream(agent="k", session="s", model=PROVIDERS[format].models[0], request={})


def read(format, lines, told=None):
    stream = fresh(format)
    r = FORMATS[format].reading(stream, lambda kind, **p: told.append((kind, p)) if told is not None else None)
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
    assert merge({"content": "kept"}, {"content": None}) == {"content": "kept"}, "null never erases what has grown"
    assert merge({"model": "gpt-5", "usage": {"a": 1}}, {"model": "gpt-5", "usage": {"b": 2}}, grow=False) == {"model": "gpt-5", "usage": {"a": 1, "b": 2}}, "a restated head is set, not grown"


def test_json_text_is_read_as_far_as_it_got():
    assert parse_json('{"cmd": "ls"}') == {"cmd": "ls"}
    assert parse_json('{"cmd": "rm -rf /tm') == {"cmd": "rm -rf /tm"}
    assert parse_json("") is None
    assert data_of('data: {"a": 1}') == {"a": 1} and data_of("event: ping") is None and data_of("data: [DONE]") is None


# ── the readings, against the real captures ───────────────────────────────────

@pytest.mark.parametrize("name,format", CAPTURES)
def test_the_response_equals_what_the_providers_own_sdk_assembles(name, format):
    """The whole response, assembled by the merge, equals the SDK's final object for every real stream we captured."""
    lines = (FIXTURES / f"{name}.sse").read_text().split("\n")
    stream = read(format, lines)
    ours = FORMATS[format].response(stream)
    if format == "anthropic":
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
        for choice in theirs["choices"]:                      # what the SDK adds as null that the wire never carried
            m, our_m = choice["message"], ours["choices"][choice["index"]]["message"]
            for k in ("annotations", "audio", "function_call", "refusal", "parsed"):
                if k not in our_m:
                    m.pop(k, None)
            for call in m.get("tool_calls") or []:
                call["function"].pop("parsed_arguments", None)
        if theirs.get("system_fingerprint") is None and "system_fingerprint" not in ours:
            theirs.pop("system_fingerprint")
    extras = {k: ours.pop(k) for k in list(ours) if k not in theirs}
    assert set(extras) <= {"timings"}, "the only thing we keep that the SDK drops is Ollama's timings"
    assert ours == theirs


def test_a_messages_stream_becomes_pieces_at_their_place_in_the_content_with_times():
    told = []
    stream = read("anthropic", (FIXTURES / "anthropic-messages.sse").read_text().split("\n"), told)
    assert [b["at"] for b in stream.blocks] == ["content[0]", "content[1]", "content[2]"]
    assert [b["type"] for b in stream.blocks] == ["text", "tool_use", "tool_use"]
    text, tool = stream.blocks[0], stream.blocks[1]
    assert text["text"].startswith("I'll start by looking") and text["started"] <= text["ended"] <= tool["started"] <= tool["ended"]
    assert tool["input"] == {"cmd": "ls -la; git status --short | head -20"} and "partial_json" not in tool and tool["caller"] == {"type": "direct"}
    assert stream.response["stop_reason"] == "tool_use" and stream.response["usage"]["output_tokens"] == 179 and stream.response["stop_details"] is None
    kinds = [k for k, _ in told]
    assert kinds[0] == "stream.response" and kinds.count("block.started") == 3 and kinds.count("block.ended") == 3
    deltas = [p for k, p in told if k == "block.delta"]
    assert deltas[0] == {"at": "content[0]", "delta": {"text": "I'll start by looking at"}}
    assert {"partial_json"} == {k for p in deltas if p["at"] == "content[1]" for k in p["delta"]}


def test_a_tool_input_is_readable_while_it_is_still_arriving():
    stream = fresh("anthropic")
    r = anthropic.reading(stream)
    r.feed(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t", "name": "shell", "input": {}}}))
    r.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "rm -r'}}))
    assert stream.blocks[0]["input"] == {"cmd": "rm -r"} and stream.blocks[0]["partial_json"] == '{"cmd": "rm -r'
    r.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'f /"}'}}))
    r.close_piece()
    assert stream.blocks[0]["input"] == {"cmd": "rm -rf /"} and "partial_json" not in stream.blocks[0]


def test_a_chat_stream_becomes_pieces_at_their_place_in_the_message_with_times():
    told = []
    stream = read("openai", (FIXTURES / "openai-chat.sse").read_text().split("\n"), told)
    assert [b["at"] for b in stream.blocks] == ["choices[0].message.role", "choices[0].message.content", "choices[0].message.tool_calls[0]"]
    role, content, call = stream.blocks
    assert role == {"at": role["at"], "started": role["started"], "ended": role["ended"], "role": "assistant"}
    assert content["content"] == "I will run the test suite, inspect failures, and apply fixes to make all tests pass."
    assert call["function"] == {"name": "shell", "arguments": '{"cmd":"bash -lc \\"ls -la\\""}'} and call["id"] == "call_qcL9itOPJMmVXfQO124Z3aJi" and call["index"] == 0
    assert content["ended"] <= call["started"], "the content piece closed when the tool call began"
    m = stream.response["choices"][0]["message"]
    assert m["content"] == content["content"] and m["tool_calls"][0]["function"]["arguments"] == call["function"]["arguments"]
    assert stream.response["choices"][0]["finish_reason"] == "tool_calls" and stream.response["service_tier"] == "default"
    assert stream.response["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0
    assert [p["at"] for k, p in told if k == "block.started"] == [b["at"] for b in stream.blocks]
    heads = [p["response"] for k, p in told if k == "stream.response"]
    assert len(heads) == 2 and "usage" in heads[1] and not any("obfuscation" in h for h in heads), "the head is told once, then only when it changes: here the usage at the end; padding is never news"
    assert "obfuscation" not in stream.response


# ── things no reading has seen before ─────────────────────────────────────────

def test_an_unknown_block_type_and_delta_type_are_kept_whole_in_messages():
    stream = read("anthropic", [sse(e) for e in [
        {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant", "model": "future", "content": [],
                                              "usage": {"input_tokens": 5, "new_counter": 7}, "brand_new_field": {"k": "v"}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "video", "video": "", "codec": "av1"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "video": "AAAA"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "video": "BBBB", "frames": [1]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "video_delta", "frames": [2]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "new_delta_field": "x"}, "usage": {"output_tokens": 9}},
        {"type": "message_stop"}]])
    r = anthropic.response(stream)
    assert r["content"] == [{"type": "video", "video": "AAAABBBB", "codec": "av1", "frames": [1, 2]}]
    assert r["brand_new_field"] == {"k": "v"} and r["new_delta_field"] == "x" and r["usage"] == {"input_tokens": 5, "new_counter": 7, "output_tokens": 9}
    assert r["stop_reason"] == "end_turn"


def test_an_unknown_streamed_field_and_indexed_list_are_kept_whole_in_chat():
    head = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "future", "new_chunk_field": "nc"}
    stream = read("openai", [sse(c) for c in [
        {**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"brand_new_text": "part one, "}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"brand_new_text": "part two"}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"citations": [{"index": 0, "url": "https://a"}]}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}, "priority": "high"}]}, "finish_reason": None}]},
        {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {**head, "choices": [], "usage": {"prompt_tokens": 5, "new_counter": 7}, "timings": {"ms": 12}}]])
    r = openai.response(stream)
    m = r["choices"][0]["message"]
    assert m["brand_new_text"] == "part one, part two" and m["citations"] == [{"index": 0, "url": "https://a"}] and m["tool_calls"][0]["priority"] == "high"
    assert r["new_chunk_field"] == "nc" and r["timings"] == {"ms": 12} and r["usage"] == {"prompt_tokens": 5, "new_counter": 7}
    assert [b["at"] for b in r["blocks"]] == ["choices[0].message.role", "choices[0].message.brand_new_text", "choices[0].message.citations[0]", "choices[0].message.tool_calls[0]"]
    assert r["blocks"][1]["brand_new_text"] == "part one, part two" and r["blocks"][1]["ended"] <= r["blocks"][2]["started"]


# ── ending a cut stream ────────────────────────────────────────────────────────

def test_closing_an_anthropic_stream_leaves_a_whole_message():
    stream = fresh("anthropic")
    r = anthropic.reading(stream, lambda kind, **p: None)
    r.feed('data: {"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {}}}')
    r.feed('data: {"type": "content_block_start", "index": 2, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}')
    events = [json.loads(s.split("data: ")[1]) for s in anthropic.closing(r)]
    assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[0]["index"] == 2 and events[1]["delta"]["stop_reason"] == "interrupted"
    bare = [json.loads(s.split("data: ")[1]) for s in anthropic.closing(anthropic.reading(fresh("anthropic"), lambda kind, **p: None))]
    assert [e["type"] for e in bare] == ["message_start", "message_delta", "message_stop"]


def test_closing_an_openai_stream_marks_the_chunk_and_ends():
    out = openai.closing(openai.reading(fresh("openai"), lambda kind, **p: None))
    chunk = json.loads(out[0][6:])
    assert chunk["interrupted"] is True and chunk["choices"][0]["finish_reason"] == "stop" and out[1] == "data: [DONE]\n\n"


def test_the_body_is_split_into_what_the_sdk_knows_and_extra_body():
    import anthropic as sdk
    client = sdk.Anthropic(api_key="x", base_url="http://127.0.0.1:1")
    kwargs, extra = anthropic._split(client.messages.create, {"model": "m", "max_tokens": 1, "messages": [], "stream": True, "brand_new_field": {"x": 1}})
    assert kwargs == {"model": "m", "max_tokens": 1, "messages": []} and extra == {"brand_new_field": {"x": 1}}

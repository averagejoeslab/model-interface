import json

from model_interface.formats import FORMATS, data_of
from model_interface.gateway import Stream
from model_interface.providers import Provider

anthropic, openai = FORMATS["anthropic"], FORMATS["openai"]
PROVIDERS = {"anthropic": Provider("a", "http://127.0.0.1:1/v1/messages", "", ["m"]),
             "openai": Provider("o", "http://127.0.0.1:1/v1/chat/completions", "", ["m"])}


def fresh(format):
    return Stream(agent="k", session="s", model=PROVIDERS[format].models[0], request={})


def test_data_of_reads_only_data_lines():
    assert data_of('data: {"a": 1}') == {"a": 1}
    assert data_of("event: ping") is None and data_of("") is None and data_of("data: [DONE]") is None


def test_an_anthropic_reading_builds_blocks_with_times_as_the_lines_arrive():
    stream, told = fresh("anthropic"), []
    r = anthropic.reading(stream, lambda kind, **p: told.append(kind))
    for e in [{"type": "message_start", "message": {"id": "m1", "model": "x", "role": "assistant", "usage": {"input_tokens": 3}}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
              {"type": "ping"},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Run the tests first."}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
              {"type": "content_block_stop", "index": 0},
              {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
              {"type": "content_block_stop", "index": 1},
              {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "t1", "name": "shell", "input": {}}},
              {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"cmd": '}}]:
        r.feed("data: " + json.dumps(e))
    assert stream.response == {"id": "m1", "model": "x", "role": "assistant", "usage": {"input_tokens": 3}}
    assert [b["type"] for b in stream.blocks] == ["thinking", "text", "tool_use"]
    assert stream.blocks[0]["thinking"] == "Run the tests first." and stream.blocks[0]["signature"] == "sig" and stream.blocks[0]["ended"] is not None
    assert stream.blocks[1]["text"] == "Hi" and stream.blocks[1]["started"] >= stream.blocks[0]["ended"]
    assert r.open is stream.blocks[2] and stream.blocks[2]["ended"] is None and stream.blocks[2]["input"] == {}
    r.feed('data: {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "\\"ls\\"}"}}')
    r.feed('data: {"type": "content_block_stop", "index": 2}')
    r.feed('data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}')
    assert stream.blocks[2]["input"] == {"cmd": "ls"} and stream.blocks[2]["ended"] is not None and r.open is None
    r = anthropic.response(stream)
    assert r["stop_reason"] == "tool_use" and r["usage"] == {"input_tokens": 3, "output_tokens": 5} and r["type"] == "message"
    assert [c["type"] for c in r["content"]] == ["thinking", "text", "tool_use"] and "started" not in r["content"][0]
    assert r["content"][2] == {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "ls"}}
    assert told[:4] == ["stream.response", "block.started", "block.delta", "block.delta"] and told.count("block.ended") == 3


def test_an_openai_reading_infers_the_same_blocks_from_chunks():
    stream, told = fresh("openai"), []
    r = openai.reading(stream, lambda kind, **p: told.append(kind))
    for c in [{"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Think"}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"reasoning_content": " first."}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"cmd":'}}]}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "ls"}'}}]}, "finish_reason": "tool_calls"}]},
              {"id": "c1", "model": "g", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5}}]:
        r.feed("data: " + json.dumps(c))
    r.feed("data: [DONE]")
    assert stream.response["id"] == "c1" and stream.response["model"] == "g" and stream.response["object"] == "chat.completion"
    assert [(b["type"], b.get("reasoning") or b.get("content") or b.get("arguments")) for b in stream.blocks] == [("reasoning", "Think first."), ("content", "Hi"), ("tool_call", '{"cmd": "ls"}')]
    assert stream.blocks[2]["id"] == "call_1" and stream.blocks[2]["name"] == "shell" and stream.blocks[2]["call_index"] == 0
    assert all(b["ended"] is not None for b in stream.blocks)
    r = stream.snapshot()["response"]
    assert r["id"] == "c1" and r["object"] == "chat.completion" and r["usage"] == {"prompt_tokens": 3, "completion_tokens": 5}
    assert r["choices"] == [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Hi", "reasoning_content": "Think first.",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}]}}]
    assert "stop_reason" not in r, "the response uses the format's own words, and only those"
    assert told.count("block.started") == 3 and told.count("block.ended") == 3


def test_closing_an_anthropic_stream_leaves_a_whole_message():
    stream = fresh("anthropic")
    r = anthropic.reading(stream, lambda kind, **p: None)
    r.feed('data: {"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {}}}')
    r.feed('data: {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}}')
    events = [json.loads(s.split("data: ")[1]) for s in anthropic.closing(r)]
    assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[0]["index"] == 0 and events[1]["delta"]["stop_reason"] == "interrupted"
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

import json

from model_interface.formats import FORMATS, data_of

anthropic, openai = FORMATS["anthropic"], FORMATS["openai"]


def test_data_of_reads_only_data_lines():
    assert data_of('data: {"a": 1}') == {"a": 1}
    assert data_of("event: ping") is None and data_of("") is None and data_of("data: [DONE]") is None


def test_an_anthropic_reading_builds_the_message_from_events():
    r = anthropic.reading()
    for e in [{"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {"input_tokens": 3}}},
              {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
              {"type": "content_block_stop", "index": 0},
              {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "shell", "input": {}}},
              {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"cmd": "ls"}'}}]:
        r.feed("data: " + json.dumps(e))
    assert r.started and r.open == 1
    r.feed('data: {"type": "content_block_stop", "index": 1}')
    r.feed('data: {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}')
    assert r.open is None
    assert r.message == {"id": "m1", "model": "x", "content": [{"type": "text", "text": "Hi"}, {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "ls"}}],
                         "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 5}}


def test_an_openai_reading_builds_the_same_shape_from_chunks():
    r = openai.reading()
    for c in [{"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"cmd":'}}]}, "finish_reason": None}]},
              {"id": "c1", "model": "g", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "ls"}'}}]}, "finish_reason": "tool_calls"}]},
              {"id": "c1", "model": "g", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5}}]:
        r.feed("data: " + json.dumps(c))
    r.feed("data: [DONE]")
    assert r.message == {"id": "c1", "model": "g", "content": [{"type": "text", "text": "Hi"}, {"type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "ls"}}],
                         "stop_reason": "tool_use", "usage": {"input_tokens": 3, "output_tokens": 5}}


def test_closing_an_anthropic_stream_leaves_a_whole_message():
    r = anthropic.reading()
    r.feed('data: {"type": "message_start", "message": {"id": "m1", "model": "x", "usage": {}}}')
    r.feed('data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}')
    events = [json.loads(s.split("data: ")[1]) for s in anthropic.closing(r)]
    assert [e["type"] for e in events] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[1]["delta"]["stop_reason"] == "interrupted"
    fresh = [json.loads(s.split("data: ")[1]) for s in anthropic.closing(anthropic.reading())]
    assert [e["type"] for e in fresh] == ["message_start", "message_delta", "message_stop"]


def test_closing_an_openai_stream_marks_the_chunk_and_ends():
    out = openai.closing(openai.reading())
    chunk = json.loads(out[0][6:])
    assert chunk["interrupted"] is True and chunk["choices"][0]["finish_reason"] == "stop" and out[1] == "data: [DONE]\n\n"


def test_the_body_is_split_into_what_the_sdk_knows_and_extra_body():
    import anthropic as sdk
    client = sdk.Anthropic(api_key="x", base_url="http://127.0.0.1:1")
    kwargs, extra = anthropic._split(client.messages.create, {"model": "m", "max_tokens": 1, "messages": [], "stream": True, "brand_new_field": {"x": 1}})
    assert kwargs == {"model": "m", "max_tokens": 1, "messages": []} and extra == {"brand_new_field": {"x": 1}}

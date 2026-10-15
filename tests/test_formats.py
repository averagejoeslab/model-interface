import json

from model_interface.formats import FORMATS, assemble

from conftest import events_for

openai, anthropic = FORMATS["openai"], FORMATS["anthropic"]
SHELL = {"name": "shell", "description": "run", "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}


def chunks(*deltas, finish="stop", usage=True):
    """A Chat Completions stream, as SSE lines."""
    lines = []
    for d in deltas:
        lines += ["data: " + json.dumps({"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "delta": d, "finish_reason": None}]}), ""]
    lines += ["data: " + json.dumps({"id": "chatcmpl-1", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}), ""]
    if usage:
        lines += ["data: " + json.dumps({"id": "chatcmpl-1", "model": "m", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}}), ""]
    return lines + ["data: [DONE]", ""]


def test_chat_completions_request_becomes_canonical_and_back():
    body = {"model": "coder", "max_tokens": 100, "tools": [{"type": "function", "function": {"name": "shell", "description": "run", "parameters": SHELL["input_schema"]}}],
            "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ls"},
                         {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}]},
                         {"role": "tool", "tool_call_id": "c1", "content": "a.py"}, {"role": "assistant", "content": "one file"}]}
    request = openai.request_in(body)
    assert request["system"] == "be brief" and request["tools"] == [SHELL]
    assert request["messages"][1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "shell", "input": {"cmd": "ls"}}]}
    assert request["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]}
    out = openai.request_out(request)
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert openai.request_in({**out, "model": "coder"})["messages"] == request["messages"]


def test_anthropic_request_out_always_streams_and_request_in_drops_the_callers_stream_flag():
    assert anthropic.request_out({"model": "m", "messages": []}) == {"model": "m", "messages": [], "stream": True}
    assert anthropic.request_in({"model": "m", "messages": [], "stream": True}) == {"model": "m", "messages": []}


def test_chat_completions_chunks_become_canonical_events():
    lines = chunks({"role": "assistant", "content": ""}, {"content": "Hel"}, {"content": "lo"},
                   {"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "shell", "arguments": ""}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":'}}]},
                   {"tool_calls": [{"index": 0, "function": {"arguments": ' "ls"}'}}]}, finish="tool_calls")
    events = list(openai.events_in(lines))
    assert [e["type"] for e in events] == ["message_start", "content_block_start", "content_block_delta", "content_block_delta",
                                           "content_block_stop", "content_block_start", "content_block_delta", "content_block_delta",
                                           "content_block_stop", "message_delta", "message_stop"]
    message = assemble(events)
    assert message["content"] == [{"type": "text", "text": "Hello"}, {"type": "tool_use", "id": "c1", "name": "shell", "input": {"cmd": "ls"}}]
    assert message["stop_reason"] == "tool_use" and message["usage"] == {"input_tokens": 3, "output_tokens": 4}


def test_canonical_events_become_chat_completions_chunks_and_back():
    events = events_for("Hi there", tool=("shell", '{"cmd": "ls"}'))
    text = "".join(openai.events_out(events))
    lines = text.split("\n")
    assert lines[-2] == "" and "data: [DONE]" in lines
    back = list(openai.events_in(lines))
    assert assemble(back)["content"] == assemble(events)["content"]
    assert assemble(back)["stop_reason"] == "tool_use"


def test_an_interrupted_stream_is_marked_in_chat_completions_format():
    events = events_for("Hi")[:-2] + [{"type": "message_delta", "delta": {"stop_reason": "interrupted", "stop_sequence": None}, "usage": {"output_tokens": 2}},
                                     {"type": "message_stop"}]
    last = [json.loads(l[5:]) for l in "".join(openai.events_out(events)).split("\n") if l.startswith("data:") and "[DONE]" not in l][-1]
    assert last["choices"][0]["finish_reason"] == "stop" and last["interrupted"] is True
    whole = openai.response_out(assemble(events))
    assert whole["interrupted"] is True and whole["choices"][0]["message"]["content"] == "Hi"


def test_assemble_folds_text_and_tool_input():
    message = assemble(events_for("Hello there", tool=("shell", '{"cmd": "pwd"}')))
    assert message["content"] == [{"type": "text", "text": "Hello there"}, {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}]
    assert message["usage"] == {"input_tokens": 10, "output_tokens": 7} and message["stop_reason"] == "tool_use"


def test_anthropic_events_out_is_sse_of_the_events_themselves():
    text = "".join(anthropic.events_out(events_for("Hi")))
    assert text.startswith("event: message_start\ndata: {") and text.rstrip().endswith('{"type": "message_stop"}')
    assert [e["type"] for e in anthropic.events_in(text.split("\n"))] == [e["type"] for e in events_for("Hi")]


def test_a_provider_without_a_key_gets_no_auth_header():
    assert anthropic.auth("") == {"anthropic-version": "2023-06-01"} and anthropic.auth("k")["x-api-key"] == "k"
    assert openai.auth("") == {} and openai.auth("k") == {"authorization": "Bearer k"}

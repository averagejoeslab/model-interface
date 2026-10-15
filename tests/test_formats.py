from model_interface.formats import FORMATS, PLATFORM_RESULT, PLATFORM_USE

from conftest import call, text

openai = FORMATS["openai"]
SHELL = {"name": "shell", "description": "run", "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}


def test_chat_completions_request_becomes_canonical():
    body = {"model": "coder", "max_tokens": 100, "tools": [{"type": "function", "function": {"name": "shell", "description": "run", "parameters": SHELL["input_schema"]}}],
            "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ls"},
                         {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}]},
                         {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
                         {"role": "assistant", "content": "one file"}]}
    request = openai.request_in(body)
    assert request["system"] == "be brief" and request["tools"] == [SHELL]
    assert request["messages"] == [{"role": "user", "content": "ls"},
                                   {"role": "assistant", "content": [call("c1", cmd="ls")]},
                                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]},
                                   {"role": "assistant", "content": [text("one file")]}]


def test_canonical_request_becomes_chat_completions_and_back():
    request = {"model": "m", "max_tokens": 50, "system": [text("be brief")], "tools": [SHELL],
               "messages": [{"role": "user", "content": "ls"}, {"role": "assistant", "content": [text("ok"), call("c1", cmd="ls")]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "a.py"}]}]}
    body = openai.request_out(request)
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert body["messages"][2] == {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "ls"}'}}]}
    assert body["tools"][0]["function"]["parameters"] == SHELL["input_schema"]
    assert openai.request_in({**body, "model": "m"})["messages"] == request["messages"]


def test_chat_completions_response_becomes_canonical():
    body = {"id": "chatcmpl-1", "model": "llama", "choices": [{"message": {"role": "assistant", "content": "hi", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    response = openai.response_in(body)
    assert response["content"] == [text("hi"), call("c1", cmd="ls")]
    assert response["stop_reason"] == "tool_use" and response["usage"] == {"input_tokens": 3, "output_tokens": 4}


def test_canonical_response_with_platform_blocks_becomes_chat_completions_and_survives_the_round_trip():
    content = [text("Running."), {**call("t1", cmd="test"), "type": PLATFORM_USE}, {"type": PLATFORM_RESULT, "tool_use_id": "t1", "content": "OK"}, text("Done.")]
    response = {"id": "msg_1", "model": "coder", "content": content, "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}}
    body = openai.response_out(response)
    message = body["choices"][0]["message"]
    assert message["content"] == "Running.Done." and "tool_calls" not in message and message["platform"] == content
    assert body["choices"][0]["finish_reason"] == "stop" and body["usage"]["total_tokens"] == 3
    sent_back = openai.request_in({"model": "coder", "messages": [{"role": "user", "content": "go"}, message]})
    assert sent_back["messages"][1] == {"role": "assistant", "content": content}


def test_anthropic_format_is_identity():
    anthropic = FORMATS["anthropic"]
    body = {"model": "coder", "messages": []}
    assert anthropic.request_in(body) is body and anthropic.response_out(body) is body
    assert anthropic.auth("k") == {"x-api-key": "k", "anthropic-version": "2023-06-01"}

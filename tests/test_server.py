import json

import httpx
from anthropic import Anthropic
from openai import OpenAI

from conftest import events_for

H = {"x-api-key": "mi-test-key", "X-Session": "demo"}


def test_messages_streams_in_anthropic_format_with_a_run_id(server, provider):
    with httpx.stream("POST", f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-run"].startswith("run_") and r.headers["x-session"] == "demo"
        lines = list(r.iter_lines())
    types = [json.loads(l[5:])["type"] for l in lines if l.startswith("data:")]
    assert types[0] == "message_start" and types[-1] == "message_stop" and types.count("content_block_delta") == 3
    assert "stream" not in provider.seen[0]                 # canonical requests carry no stream flag; the provider adds it


def test_messages_without_stream_gets_one_assembled_message(server):
    r = httpx.post(f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert r.status_code == 200 and body["content"] == [{"type": "text", "text": "Hello there, friend."}]
    assert body["stop_reason"] == "end_turn" and body["usage"] == {"input_tokens": 10, "output_tokens": 7}


def test_the_sdks_stream_through_both_endpoints(server, provider):
    provider.script[:] = events_for("Hi there", tool=("shell", '{"cmd": "ls"}'))
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": "hi"}]) as s:
        assert "".join(s.text_stream) == "Hi there"
        m = s.get_final_message()
    assert m.content[1].type == "tool_use" and m.content[1].input == {"cmd": "ls"} and m.stop_reason == "tool_use"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key")
    text, calls, finish = "", {}, None
    for chunk in o.chat.completions.create(model="coder", stream=True, messages=[{"role": "user", "content": "hi"}]):
        if chunk.choices:
            d = chunk.choices[0].delta
            text += d.content or ""
            for c in d.tool_calls or []:
                calls[c.index] = calls.get(c.index, "") + (c.function.arguments or "")
            finish = chunk.choices[0].finish_reason or finish
    assert text == "Hi there" and json.loads(calls[0]) == {"cmd": "ls"} and finish == "tool_calls"


def test_an_interrupt_mid_stream_ends_it_cleanly_for_the_caller(server, provider, gateway):
    provider.script[:] = events_for("one two three four five six seven eight nine ten")
    provider.delay = 0.05
    seen = []
    with httpx.stream("POST", f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "go"}]}) as r:
        run = r.headers["x-run"]
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[5:]))
                if len(seen) == 4:
                    assert httpx.post(f"{server}/v1/interrupt/{run}", headers=H).status_code == 200
    assert seen[-2]["delta"]["stop_reason"] == "interrupted" and seen[-1]["type"] == "message_stop"
    assert len(seen) < len(provider.script) and provider.closed
    record = httpx.get(f"{server}/v1/runs", headers=H, params={"session": "demo"}).json()["data"][0]
    assert record["run"] == run and record["interrupted"] is True and record["stop_reason"] == "interrupted"
    assert httpx.delete(f"{server}/v1/interrupt/{run}", headers=H).json() == {"stopped": []}


def test_a_stopped_session_refuses_new_runs_until_resumed(server, provider):
    httpx.post(f"{server}/v1/interrupt/demo", headers=H)
    body = httpx.post(f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}).json()
    assert body["stop_reason"] == "interrupted" and body["content"] == []
    httpx.delete(f"{server}/v1/interrupt/demo", headers=H)
    assert httpx.post(f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}).json()["stop_reason"] == "end_turn"


def test_a_provider_refusal_passes_through(server, provider):
    def refused(request):
        req = httpx.Request("POST", "http://fake/v1/messages")
        raise httpx.HTTPStatusError("fake answered 400", request=req, response=httpx.Response(400, json={"error": {"message": "prompt is too long"}}, request=req))
        yield
    provider.stream = refused
    r = httpx.post(f"{server}/v1/messages", headers=H, json={"model": "coder", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 502 and "prompt is too long" in r.text


def test_unknown_key_model_and_session_are_refused(server):
    good = {"model": "coder", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    assert httpx.post(f"{server}/v1/messages", headers={"x-api-key": "wrong"}, json=good).status_code == 401
    assert httpx.post(f"{server}/v1/messages", headers=H, json={**good, "model": "nope"}).status_code == 404
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "../etc"}, json=good).status_code == 400
    assert httpx.get(f"{server}/v1/models", headers=H).json()["data"] == [{"id": "coder", "object": "model"}]

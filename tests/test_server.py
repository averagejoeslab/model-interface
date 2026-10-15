import json
import threading
import time

import httpx
from anthropic import Anthropic
from openai import OpenAI

H = {"x-api-key": "mi-test-key", "X-Session": "demo"}


def anthropic_body(model="coder", **extra):
    return {"model": model, "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


def openai_body(model="gpt", **extra):
    return {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


# ── pass-through ──────────────────────────────────────────────────────────────

def test_messages_passes_through_to_the_anthropic_provider_verbatim_with_the_alias_swapped(server, provider):
    with httpx.stream("POST", f"{server}/v1/messages", headers={**H, "anthropic-beta": "some-beta"}, json=anthropic_body(metadata={"user_id": "u1"}, brand_new_field=1)) as r:
        assert r.status_code == 200 and r.headers["x-run"].startswith("run_")
        text = "".join(r.iter_text())
    sent = provider.requests[0]
    assert sent["body"]["model"] == "fake-sonnet" and sent["body"]["stream"] is True
    assert sent["body"]["metadata"] == {"user_id": "u1"} and sent["body"]["brand_new_field"] == 1
    assert sent["headers"]["anthropic-beta"] == "some-beta" and sent["headers"]["x-api-key"] == "none"
    assert text.startswith("event: message_start\n") and text.rstrip().endswith('data: {"type": "message_stop"}')


def test_chat_completions_passes_through_to_the_openai_provider(server, provider):
    with httpx.stream("POST", f"{server}/v1/chat/completions", headers={"authorization": "Bearer mi-test-key"}, json=openai_body(temperature=0.2)) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    sent = provider.requests[0]
    assert sent["body"]["model"] == "fake-gpt" and sent["body"]["temperature"] == 0.2 and sent["headers"]["authorization"] == "Bearer none"
    assert text.rstrip().endswith("data: [DONE]")


def test_both_native_sdks_stream_through_their_own_endpoint(server, provider):
    provider.tool = ("shell", {"cmd": "ls"})
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": "hi"}]) as s:
        assert "".join(s.text_stream) == "Hello there, friend."
        m = s.get_final_message()
    assert m.content[1].input == {"cmd": "ls"} and m.stop_reason == "tool_use" and m.model == "fake-sonnet"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key")
    text, args, finish = "", "", None
    for chunk in o.chat.completions.create(model="gpt", stream=True, messages=[{"role": "user", "content": "hi"}]):
        for choice in chunk.choices:
            text += choice.delta.content or ""
            args += "".join(c.function.arguments or "" for c in choice.delta.tool_calls or [])
            finish = choice.finish_reason or finish
    assert text == "Hello there, friend." and json.loads(args) == {"cmd": "ls"} and finish == "tool_calls"


def test_refusals(server):
    assert httpx.post(f"{server}/v1/messages", headers=H, json={**anthropic_body(), "stream": False}).status_code == 400
    assert httpx.post(f"{server}/v1/messages", headers={"x-api-key": "wrong"}, json=anthropic_body()).status_code == 401
    assert httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body("nope")).status_code == 404
    assert httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body("gpt")).status_code == 400          # wrong endpoint for its format
    assert httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body("coder")).status_code == 400
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "../x"}, json=anthropic_body()).status_code == 400
    models = httpx.get(f"{server}/v1/models", headers=H).json()["data"]
    assert {m["id"]: m["format"] for m in models}["gpt"] == "openai"


def test_a_providers_refusal_passes_through_with_its_status_and_body(server):
    r = httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body("broken"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body("broken-gpt"))
    assert r.status_code == 400 and "prompt is too long" in r.text


# ── observability: sessions ───────────────────────────────────────────────────

def test_runs_are_recorded_under_their_session_with_request_and_response(server, provider):
    provider.tool = ("shell", {"cmd": "pwd"})
    a = Anthropic(base_url=server, api_key="mi-test-key")
    for ask in ("first", "second"):
        with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": ask}], extra_headers={"X-Session": "s-obs"}) as s:
            s.get_final_message()
    sessions = httpx.get(f"{server}/v1/sessions", headers=H).json()["data"]
    assert [s["session"] for s in sessions] == ["s-obs"] and sessions[0]["runs"] == 2
    runs = httpx.get(f"{server}/v1/sessions/s-obs", headers=H).json()["runs"]
    assert [r["asked"] for r in runs] == ["first", "second"]
    assert runs[0]["answered"] == "Hello there, friend." and runs[0]["tool_calls"] == ["shell"] and runs[0]["stop_reason"] == "tool_use"
    assert runs[0]["usage"] == {"input_tokens": 10, "output_tokens": 7} and runs[0]["first_line_ms"] is not None and runs[0]["lines"] > 5
    full = httpx.get(f"{server}/v1/runs/{runs[1]['run']}", headers=H).json()
    assert full["request"]["messages"] == [{"role": "user", "content": "second"}] and full["request"]["model"] == "coder"
    assert full["response"]["content"][1] == {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}
    assert httpx.get(f"{server}/v1/runs/run_nope", headers=H).status_code == 404


def test_an_openai_run_is_recorded_in_the_same_shape(server, provider):
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key", default_headers={"X-Session": "s-oai"})
    list(o.chat.completions.create(model="gpt", stream=True, messages=[{"role": "user", "content": "hey"}]))
    run = httpx.get(f"{server}/v1/sessions/s-oai", headers=H).json()["runs"][0]
    assert run["format"] == "openai" and run["answered"] == "Hello there, friend." and run["stop_reason"] == "end_turn"
    assert run["usage"] == {"input_tokens": 10, "output_tokens": 7}


# ── the killswitch ────────────────────────────────────────────────────────────

def read_stream(server, path, body, session, after=None):
    """Stream a call; after N data lines, do something. Returns the data payloads seen."""
    seen = []
    with httpx.stream("POST", f"{server}{path}", headers={**H, "X-Session": session}, json=body) as r:
        run = r.headers["x-run"]
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(line[5:].strip())
                if after and len(seen) == after[0]:
                    after[1](run)
    return run, seen


def test_the_session_killswitch_cuts_a_running_anthropic_stream_and_the_provider_sees_the_hangup(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    run, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-kill",
                            after=(5, lambda _: httpx.put(f"{server}/v1/killswitch/s-kill", headers=H, json={"on": True})))
    events = [json.loads(s) for s in seen]
    assert [e["type"] for e in events[-3:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[-2]["delta"]["stop_reason"] == "interrupted" and len(events) < 14
    assert provider.closed_early
    rec = httpx.get(f"{server}/v1/runs/{run}", headers=H).json()
    assert rec["interrupted"] is True and rec["stop_reason"] == "interrupted" and rec["response"]["content"][0]["text"].startswith("one two")
    assert httpx.get(f"{server}/v1/killswitch", headers=H).json() == {"all": False, "sessions": ["s-kill"]}
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body()).status_code == 423
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "other"}, json=anthropic_body()).status_code == 200
    httpx.put(f"{server}/v1/killswitch/s-kill", headers=H, json={"on": False})
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body()).status_code == 200


def test_the_global_killswitch_cuts_an_openai_stream_and_refuses_everything_until_off(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    run, seen = read_stream(server, "/v1/chat/completions", openai_body(), "s-any",
                            after=(5, lambda _: httpx.put(f"{server}/v1/killswitch", headers=H, json={"on": True})))
    last = json.loads(seen[-2])
    assert seen[-1] == "[DONE]" and last["interrupted"] is True and last["choices"][0]["finish_reason"] == "stop"
    assert provider.closed_early and httpx.get(f"{server}/v1/runs/{run}", headers=H).json()["interrupted"] is True
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "anything"}, json=anthropic_body()).status_code == 423
    assert httpx.get(f"{server}/v1/killswitch", headers=H).json()["all"] is True
    httpx.put(f"{server}/v1/killswitch", headers=H, json={"on": False})
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "anything"}, json=anthropic_body()).status_code == 200


def test_the_sdks_see_a_cut_stream_as_a_whole_message(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    a = Anthropic(base_url=server, api_key="mi-test-key")
    threading.Timer(0.25, lambda: httpx.put(f"{server}/v1/killswitch/s-sdk", headers=H, json={"on": True})).start()
    with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": "go"}], extra_headers={"X-Session": "s-sdk"}) as s:
        text = "".join(s.text_stream)
        m = s.get_final_message()
    assert m.stop_reason == "interrupted" and 0 < len(text) < len(provider.text)
    httpx.put(f"{server}/v1/killswitch/s-sdk", headers=H, json={"on": False})


def test_the_killswitch_cuts_within_a_tenth_of_a_second_even_while_the_provider_is_silent(server, provider):
    provider.stall = (3, 8.0)                            # three lines, then eight seconds of nothing, as a thinking model does
    t0 = time.time()
    threading.Timer(0.5, lambda: httpx.put(f"{server}/v1/killswitch/s-silent", headers=H, json={"on": True})).start()
    run, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-silent")
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"the cut took {elapsed:.1f}s: it waited for the provider instead of the switch"
    events = [json.loads(s) for s in seen]
    assert events[-2]["delta"]["stop_reason"] == "interrupted" and events[-1]["type"] == "message_stop"
    assert httpx.get(f"{server}/v1/runs/{run}", headers=H).json()["interrupted"] is True
    httpx.put(f"{server}/v1/killswitch/s-silent", headers=H, json={"on": False})
    time.sleep(0.3)
    assert provider.closed_early

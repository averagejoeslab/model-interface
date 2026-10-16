import json
import threading
import time

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI

H = {"x-api-key": "mi-test-key", "X-Session": "demo"}      # the agent "coder"
ADMIN = {"x-api-key": "admin-key"}


def anthropic_body(model="fake-sonnet", **extra):
    return {"model": model, "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


def openai_body(model="fake-gpt", **extra):
    return {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


def watch(server, path, seen, ready, key="admin-key"):
    """Follow a live feed, stamping each record event with when it arrived, until a stream ends."""
    with httpx.stream("GET", f"{server}{path}", headers={"x-api-key": key}, timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                e = json.loads(line[5:])
                if "scope" in e and "type" not in e:
                    ready.set()
                    continue
                seen.append((time.time(), e))
                if e["type"] == "stream.ended":
                    return


def hung_up(provider, within=2.0) -> bool:
    """Whether the gateway closed its connection to the provider, allowing the provider a moment to notice."""
    end = time.time() + within
    while not provider.closed_early and time.time() < end:
        time.sleep(0.02)
    return provider.closed_early


def read_stream(server, path, body, session, after=None, key="mi-test-key"):
    seen = []
    with httpx.stream("POST", f"{server}{path}", headers={"x-api-key": key, "X-Session": session}, json=body) as r:
        assert r.status_code == 200, r.read()
        stream = r.headers["x-stream"]
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(line[5:].strip())
                if after and len(seen) == after[0]:
                    after[1](stream)
    return stream, seen


# ── pass-through ──────────────────────────────────────────────────────────────

def test_messages_passes_through_verbatim_with_the_providers_key_and_name(server, provider):
    body = anthropic_body(system="be brief", temperature=0.2, brand_new_field={"x": 1})
    r = httpx.post(f"{server}/v1/messages", headers=H, json=body)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-agent"] == "coder" and r.headers["x-session"] == "demo" and r.headers["x-stream"].startswith("str_")
    sent = provider.requests[-1]
    assert sent["path"] == "/v1/messages" and sent["body"] == {**body, "model": "fake-sonnet"}
    assert "x-session" not in sent["headers"] and sent["headers"]["x-api-key"] == "none"
    assert "data: " in r.text and "Hello" in r.text


def test_chat_completions_passes_through_to_the_openai_provider(server, provider):
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body(temperature=0))
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    assert provider.requests[-1]["path"] == "/v1/chat/completions" and provider.requests[-1]["body"]["model"] == "fake-gpt"


def test_both_native_sdks_stream_through_their_own_endpoint(server, provider):
    provider.tool = ("shell", {"cmd": "ls"})
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": "go"}]) as s:
        m = s.get_final_message()
    assert m.content[0].text == "Hello there, friend." and m.content[1].name == "shell" and m.stop_reason == "tool_use"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key")
    chunks = list(o.chat.completions.create(model="fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello there, friend."
    assert [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason] == ["tool_calls"]


def test_refusals(server):
    post = lambda path, body, **h: httpx.post(f"{server}{path}", headers={**H, **h}, json=body)
    assert post("/v1/messages", anthropic_body(), **{"x-api-key": "nope"}).status_code == 401
    assert post("/v1/messages", anthropic_body(), **{"x-api-key": "admin-key"}).status_code == 401, "the operator's key does not call models"
    assert post("/v1/messages", {**anthropic_body(), "stream": False}).status_code == 400
    assert post("/v1/messages", anthropic_body(model="nothing")).status_code == 404
    r = post("/v1/chat/completions", openai_body(model="fake-sonnet"))
    assert r.status_code == 400 and "/v1/messages" in r.json()["detail"], "a Messages model is pointed at its own endpoint"
    assert post("/v1/messages", anthropic_body(), **{"X-Session": "bad session!"}).status_code == 400
    r = post("/v1/chat/completions", openai_body(model="fake-sonnet"), **{"x-api-key": "helper-key"})
    assert r.status_code == 404 and "helper" in r.json()["detail"], "an agent only sees its own grants"


def test_a_providers_refusal_passes_through_with_its_status_and_body(server):
    r = httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body(model="bad"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body(model="bad"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"


def test_models_lists_what_each_key_may_use(server):
    mine = httpx.get(f"{server}/v1/models", headers={"x-api-key": "helper-key"}).json()["data"]
    assert [(m["id"], m["provider"], m["endpoint"]) for m in mine] == [("fake-gpt", "fake-openai", "/v1/chat/completions")]
    assert len(httpx.get(f"{server}/v1/models", headers=ADMIN).json()["data"]) == 4
    assert OpenAI(base_url=f"{server}/v1", api_key="helper-key").models.list().data[0].id == "fake-gpt"


# ── the record: agents of sessions of streams of blocks ───────────────────────

def test_a_session_holds_streams_of_blocks_with_times_at_every_level(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    a = Anthropic(base_url=server, api_key="mi-test-key")
    for ask in ("first", "second"):
        with a.messages.stream(model="fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": ask}], extra_headers={"X-Session": "s-rec"}) as s:
            s.get_final_message()
    s = httpx.get(f"{server}/v1/agents/coder/sessions/s-rec", headers=ADMIN).json()
    assert s["agent"] == "coder" and s["session"] == "s-rec" and s["started"] and s["ended"] and len(s["streams"]) == 2 and s["killswitch"] is False
    first = s["streams"][0]
    assert first["started"] <= first["ended"] <= s["ended"] and first["request"]["messages"] == [{"role": "user", "content": "first"}]
    assert first["provider"] == "fake-anthropic" and first["model"] == "fake-sonnet" and first["format"] == "anthropic"
    r = first["response"]
    assert r["id"] == "msg_1" and r["type"] == "message" and r["model"] == "fake-sonnet" and r["role"] == "assistant"
    assert r["stop_reason"] == "tool_use" and r["stop_sequence"] is None and r["usage"] == {"input_tokens": 10, "output_tokens": 7}
    assert r["content"] == [{"type": "thinking", "thinking": "Where am I?", "signature": "sig_abc"}, {"type": "text", "text": "Hello there, friend."},
                            {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}], "the response is whole, in the provider's shape"
    assert [b["type"] for b in first["blocks"]] == ["thinking", "text", "tool_use"] and [b["at"] for b in first["blocks"]] == ["content[0]", "content[1]", "content[2]"]
    th, tx, tu = first["blocks"]
    assert th["thinking"] == "Where am I?" and th["signature"] == "sig_abc" and th["started"] <= th["ended"] <= tx["started"]
    assert tx["text"] == "Hello there, friend." and tx["ended"] <= tu["started"] <= tu["ended"] <= first["ended"]
    assert tu == {"at": "content[2]", "type": "tool_use", "started": tu["started"], "ended": tu["ended"], "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}
    assert first["interrupted"] is None and first["error"] is None
    assert httpx.get(f"{server}/v1/streams/{first['id']}", headers=ADMIN).json()["blocks"] == first["blocks"]
    assert "blocks" not in httpx.get(f"{server}/v1/agents/coder/sessions/s-rec", headers=ADMIN, params={"blocks": "false"}).json()["streams"][0]
    agents = httpx.get(f"{server}/v1/agents", headers=ADMIN).json()["data"]
    assert [a["agent"] for a in agents] == ["coder", "helper"]
    assert agents[0]["sessions"] == 1 and agents[0]["streams"] == 2 and agents[0]["running"] == 0 and agents[0]["killswitch"] is False
    assert agents[1] == {"agent": "helper", "sessions": 0, "streams": 0, "running": 0, "interrupted": 0, "last": None, "killswitch": False}


def test_an_openai_stream_is_recorded_in_the_same_shape_with_its_own_block_types(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key", default_headers={"X-Session": "s-rec-o"})
    list(o.chat.completions.create(model="fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    st = httpx.get(f"{server}/v1/agents/coder/sessions/s-rec-o", headers=ADMIN).json()["streams"][0]
    assert [b["at"].removeprefix("choices[0].message.") for b in st["blocks"]] == ["role", "reasoning_content", "content", "tool_calls[0]"]
    role, reasoning, content, call = st["blocks"]
    assert reasoning["reasoning_content"] == "Where am I?" and content["content"] == "Hello there, friend."
    assert call["function"]["name"] == "shell" and json.loads(call["function"]["arguments"]) == {"cmd": "pwd"} and call["index"] == 0 and call["id"] == "call_1"
    assert all(b["started"] <= b["ended"] for b in st["blocks"])
    r = st["response"]
    assert r["id"] == "chatcmpl-1" and r["object"] == "chat.completion" and r["created"] == 1 and r["model"] == "fake-gpt"
    assert r["usage"] == {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17} and r["choices"][0]["finish_reason"] == "tool_calls"
    assert r["choices"][0]["message"] == {"role": "assistant", "content": "Hello there, friend.", "reasoning_content": "Where am I?",
                                          "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "pwd"}'}}]}


def test_an_agent_sees_only_its_own_record_and_the_operator_sees_all(server, provider):
    OpenAI(base_url=f"{server}/v1", api_key="helper-key", default_headers={"X-Session": "h1"}).chat.completions.create(
        model="fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]).response.read()
    httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body())
    helper = {"x-api-key": "helper-key"}
    assert [a["agent"] for a in httpx.get(f"{server}/v1/agents", headers=helper).json()["data"]] == ["helper"]
    assert httpx.get(f"{server}/v1/agents/coder", headers=helper).status_code == 403
    assert httpx.get(f"{server}/v1/agents/helper/sessions/h1", headers=helper).status_code == 200
    assert httpx.get(f"{server}/v1/me", headers=helper).json() == {"operator": False, "agent": "helper"}
    assert httpx.get(f"{server}/v1/me", headers=ADMIN).json() == {"operator": True, "agent": None}
    assert httpx.put(f"{server}/v1/killswitch", headers=helper, json={"on": True}).status_code == 401, "only the operator holds the switches"
    assert httpx.get(f"{server}/v1/agents", headers={"x-api-key": "nope"}).status_code == 401


# ── real time: the live feed shows blocks as they open, grow and close ────────

def test_the_live_feed_shows_each_block_opening_growing_and_closing_before_the_stream_ends(server, provider):
    provider.tool, provider.thinking, provider.delay = ("shell", {"cmd": "ls"}), "Look first.", 0.02
    seen, ready = [], threading.Event()
    watcher = threading.Thread(target=watch, args=(server, "/v1/agents/coder/sessions/s-live/live", seen, ready), daemon=True)
    watcher.start()
    assert ready.wait(5)
    t0 = time.time()
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": "go"}], extra_headers={"X-Session": "s-live"}) as s:
        s.get_final_message()
    t_end = time.time()
    watcher.join(5)
    kinds = [e["type"] for _, e in seen]
    assert kinds[0] == "stream.started" and kinds[1] == "stream.response" and kinds[-1] == "stream.ended"
    assert "response" not in seen[0][1]["snapshot"] and seen[1][1]["response"]["id"] == "msg_1"
    assert kinds.count("block.started") == 3 and kinds.count("block.ended") == 3 and kinds.count("block.delta") >= 5
    assert all(e["agent"] == "coder" and e["session"] == "s-live" and e["stream"] == seen[0][1]["stream"] for _, e in seen)
    opened = [(t, e) for t, e in seen if e["type"] == "block.started"]
    assert [e["block"]["type"] for _, e in opened] == ["thinking", "text", "tool_use"]
    assert t0 < opened[0][0] < opened[1][0] < opened[2][0] < t_end, "blocks must arrive while the stream is still running, in order"
    assert all(e["block"]["ended"] is None for _, e in opened) and all(e["block"]["ended"] for _, e in seen if e["type"] == "block.ended")
    assert {k for _, d in seen if d["type"] == "block.delta" for k in d["delta"]} == {"thinking", "signature", "text", "partial_json"}
    assert {d["at"] for _, d in seen if d["type"] == "block.delta"} == {"content[0]", "content[1]", "content[2]"}
    ended = [e for _, e in seen if e["type"] == "stream.ended"][0]
    assert ended["snapshot"]["response"]["stop_reason"] == "tool_use" and len(ended["snapshot"]["response"]["content"]) == 3
    assert "request" not in ended["snapshot"] and "blocks" not in ended["snapshot"]


def test_the_global_feed_carries_every_agent_and_an_agents_feed_only_its_own(server, provider):
    provider.delay = 0.01
    everything, agent_only, r1, r2 = [], [], threading.Event(), threading.Event()
    threading.Thread(target=watch, args=(server, "/v1/live?key=admin-key", everything, r1), daemon=True).start()
    threading.Thread(target=watch, args=(server, "/v1/live", agent_only, r2, "helper-key"), daemon=True).start()
    assert r1.wait(5) and r2.wait(5)
    httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "g1"}, json=anthropic_body())
    time.sleep(0.3)
    assert everything and all(e["agent"] == "coder" and e["session"] == "g1" for _, e in everything)
    assert everything[0][1]["type"] == "stream.started" and everything[0][1]["snapshot"]["request"]["messages"][0]["content"] == "hi"
    assert agent_only == [], "helper's feed never sees coder's streams"
    assert httpx.get(f"{server}/v1/live?key=nope").status_code == 401


# ── the killswitch: gateway, agent, session ───────────────────────────────────

def test_the_session_killswitch_cuts_that_session_only_and_records_the_block_it_landed_in(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/agents/coder/sessions/s-kill/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-kill", after=(5, lambda _: flip(True)))
    events = [json.loads(s) for s in seen]
    assert [e["type"] for e in events[-3:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[-2]["delta"]["stop_reason"] == "interrupted" and len(events) < 14 and hung_up(provider)
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["response"]["stop_reason"] == "interrupted" and rec["interrupted"]["by"] == "session" and rec["interrupted"]["at"]
    assert rec["interrupted"]["block"] == {"at": "content[0]", "type": "text"} and rec["blocks"][0]["text"].startswith("one two") and rec["blocks"][0]["ended"]
    r = httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body())
    assert r.status_code == 423 and r.json()["error"]["level"] == "session"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-other"}, json=anthropic_body()).status_code == 200, "other sessions run"
    assert httpx.get(f"{server}/v1/agents/coder/sessions/s-kill", headers=ADMIN).json()["killswitch"] is True
    assert httpx.get(f"{server}/v1/killswitch", headers=ADMIN).json() == {"gateway": False, "agents": [], "sessions": [{"agent": "coder", "session": "s-kill"}]}
    flip(False)
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body()).status_code == 200


def test_the_agent_killswitch_cuts_every_session_of_that_agent_and_no_other_agent(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/agents/coder/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/chat/completions", openai_body(), "a-one", after=(5, lambda _: flip(True)))
    last = json.loads(seen[-2])
    assert seen[-1] == "[DONE]" and last["interrupted"] is True and hung_up(provider)
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["interrupted"]["by"] == "agent" and rec["response"]["choices"][0]["finish_reason"] == "interrupted"
    assert rec["interrupted"]["block"]["at"] == "choices[0].message.content"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "a-two"}, json=anthropic_body()).json()["error"]["level"] == "agent"
    assert httpx.post(f"{server}/v1/chat/completions", headers={"x-api-key": "helper-key"}, json=openai_body()).status_code == 200, "helper runs"
    switches = {a["agent"]: a["killswitch"] for a in httpx.get(f"{server}/v1/agents", headers=ADMIN).json()["data"]}
    assert switches == {"coder": True, "helper": False}
    flip(False)
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "a-two"}, json=anthropic_body()).status_code == 200


def test_the_gateway_killswitch_refuses_everything_until_off(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "g-one", after=(5, lambda _: flip(True)))
    assert json.loads(seen[-2])["delta"]["stop_reason"] == "interrupted" and hung_up(provider)
    assert httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()["interrupted"]["by"] == "gateway"
    assert httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body()).json()["error"]["level"] == "gateway"
    assert httpx.post(f"{server}/v1/chat/completions", headers={"x-api-key": "helper-key"}, json=openai_body()).status_code == 423
    flip(False)
    assert httpx.post(f"{server}/v1/chat/completions", headers={"x-api-key": "helper-key"}, json=openai_body()).status_code == 200


def test_the_killswitch_cuts_within_a_tenth_of_a_second_even_while_the_provider_is_silent(server, provider):
    provider.stall = (3, 8.0)
    t0 = time.time()
    threading.Timer(0.5, lambda: httpx.put(f"{server}/v1/agents/coder/sessions/s-silent/killswitch", headers=ADMIN, json={"on": True})).start()
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-silent")
    assert time.time() - t0 < 1.5
    assert json.loads(seen[-2])["delta"]["stop_reason"] == "interrupted"
    httpx.put(f"{server}/v1/agents/coder/sessions/s-silent/killswitch", headers=ADMIN, json={"on": False})
    assert hung_up(provider)


def test_switch_changes_are_announced_on_the_feed(server):
    seen, ready = [], threading.Event()

    def follow():
        with httpx.stream("GET", f"{server}/v1/live", headers=ADMIN, timeout=10) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    e = json.loads(line[5:])
                    if e.get("type") == "killswitch":
                        seen.append(e)
                        if len(seen) == 2:
                            return
                    elif "scope" in e:
                        assert e["killswitch"]["gateway"] is False
                        ready.set()

    t = threading.Thread(target=follow, daemon=True)
    t.start()
    assert ready.wait(5)
    httpx.put(f"{server}/v1/agents/coder/killswitch", headers=ADMIN, json={"on": True})
    httpx.put(f"{server}/v1/agents/coder/killswitch", headers=ADMIN, json={"on": False})
    t.join(5)
    assert [(e["level"], e["on"], e["agent"]) for e in seen] == [("agent", True, "coder"), ("agent", False, "coder")]
    assert seen[0]["state"]["agents"] == ["coder"] and seen[1]["state"]["agents"] == []


# ── the page ──────────────────────────────────────────────────────────────────

def test_the_page_is_served_at_the_root(server):
    page = httpx.get(f"{server}/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "/v1/live?key=" in page.text


def test_a_cut_in_the_middle_of_a_tool_input_still_closes_the_record_with_the_input_as_far_as_it_got(server, provider):
    provider.tool, provider.delay = ("shell", {"cmd": "rm -rf /tmp/scratch && echo done"}), 0.05
    flip = lambda on: httpx.put(f"{server}/v1/agents/coder/sessions/s-tool-cut/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-tool-cut",
                               after=(8, lambda _: flip(True)))                   # after the tool block opened and its first input piece
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["ended"] and rec["interrupted"]["block"]["type"] == "tool_use", rec
    tool = rec["blocks"][-1]
    assert tool["ended"] and isinstance(tool["input"], dict) and "partial_json" not in tool, "the cut tool's input is parsed as far as it got, not left as text or lost"
    assert rec["response"]["stop_reason"] == "interrupted" and rec["response"]["content"][-1]["input"] == tool["input"]
    flip(False)

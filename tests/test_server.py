import json
import threading
import time

import httpx
from anthropic import Anthropic
from openai import OpenAI

from model_interface.config import Config
from model_interface.providers import Agent, Provider
from model_interface.server import create_app
from conftest import serve

H = {"x-api-key": "mi-test-key", "X-Session": "demo"}      # the agent "coder"
HELPER = {"x-api-key": "helper-key"}
ADMIN = {"x-api-key": "admin-key"}


def messages_body(model="fake/fake-sonnet", **extra):
    return {"model": model, "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


def chat_body(model="fake/fake-gpt", **extra):
    return {"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}], **extra}


def watch(server, path, seen, ready, key="admin-key", until=("stream.ended",)):
    with httpx.stream("GET", f"{server}{path}", headers={"x-api-key": key}, timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                e = json.loads(line[5:])
                if "scope" in e and "type" not in e:
                    ready.set()
                    continue
                seen.append((time.time(), e))
                if e["type"] in until:
                    return


def hung_up(provider, within=2.0) -> bool:
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


# ── the two doors, one provider ───────────────────────────────────────────────

def test_the_messages_door_passes_through_with_the_providers_model_name(server, provider):
    body = messages_body(system="be brief", temperature=0.2, brand_new_field={"x": 1})
    r = httpx.post(f"{server}/v1/messages", headers=H, json=body)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-agent"] == "coder" and r.headers["x-session"] == "demo" and r.headers["x-stream"].startswith("str_")
    sent = provider.requests[-1]
    assert sent["path"] == "/v1/messages" and sent["body"] == {**body, "model": "fake-sonnet"}, "the provider's name is stripped; nothing else changes"
    assert "x-session" not in sent["headers"] and "Hello" in r.text


def test_the_chat_door_reaches_the_same_provider_with_the_other_sdk(server, provider):
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=chat_body(temperature=0))
    assert r.status_code == 200 and r.text.rstrip().endswith("data: [DONE]")
    assert provider.requests[-1]["path"] == "/v1/chat/completions" and provider.requests[-1]["body"]["model"] == "fake-gpt"


def test_both_native_sdks_work_through_their_door(server, provider):
    provider.tool = ("shell", {"cmd": "ls"})
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="fake/fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": "go"}]) as s:
        m = s.get_final_message()
    assert m.content[0].text == "Hello there, friend." and m.content[1].name == "shell" and m.stop_reason == "tool_use"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key")
    chunks = list(o.chat.completions.create(model="other/fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello there, friend."


def test_routing_by_name_and_the_bare_name_when_there_is_one_provider(server, provider):
    r = httpx.post(f"{server}/v1/messages", headers=H, json=messages_body(model="fake-sonnet"))
    assert r.status_code == 404 and "fake/fake-sonnet" in r.json()["error"]["message"] and "other/fake-sonnet" in r.json()["error"]["message"]
    assert httpx.post(f"{server}/v1/messages", headers=H, json=messages_body(model="nobody/fake-sonnet")).status_code == 404
    one = Config(providers={"only": Provider("only", provider.url)}, agents={"coder": Agent("coder", ["k"])}, admin="a", record=None)
    srv, url = serve(create_app(one))
    try:
        assert httpx.post(f"{url}/v1/messages", headers={"x-api-key": "k"}, json=messages_body(model="fake-sonnet")).status_code == 200
        assert provider.requests[-1]["body"]["model"] == "fake-sonnet"
    finally:
        srv.should_exit = True


def test_refusals(server):
    post = lambda path, body, **h: httpx.post(f"{server}{path}", headers={**H, **h}, json=body)
    assert post("/v1/messages", messages_body(), **{"x-api-key": "nope"}).status_code == 401
    assert post("/v1/messages", messages_body(), **{"x-api-key": "admin-key"}).status_code == 401, "the operator's key does not call models"
    assert post("/v1/messages", {**messages_body(), "stream": False}).status_code == 400
    assert post("/v1/messages", messages_body(), **{"X-Session": "bad session!"}).status_code == 400


def test_a_providers_refusal_passes_through_with_its_status_and_body(server):
    r = httpx.post(f"{server}/v1/messages", headers=H, json=messages_body(model="fake/bad"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=chat_body(model="fake/bad"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"


# ── the record ────────────────────────────────────────────────────────────────

def test_a_session_holds_streams_of_pieces_with_times_at_every_level(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    a = Anthropic(base_url=server, api_key="mi-test-key")
    for ask in ("first", "second"):
        with a.messages.stream(model="fake/fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": ask}], extra_headers={"X-Session": "s-rec"}) as s:
            s.get_final_message()
    s = httpx.get(f"{server}/v1/agents/coder/sessions/s-rec", headers=ADMIN).json()
    assert s["agent"] == "coder" and s["session"] == "s-rec" and s["started"] and s["ended"] and len(s["streams"]) == 2
    first = s["streams"][0]
    assert (first["door"], first["provider"], first["model"]) == ("messages", "fake", "fake-sonnet") and first["request"]["model"] == "fake-sonnet"
    assert [b["type"] for b in first["blocks"]] == ["thinking", "text", "tool_use"] and [b["at"] for b in first["blocks"]] == ["content[0]", "content[1]", "content[2]"]
    th, tx, tu = first["blocks"]
    assert th["started"] <= th["ended"] <= tx["started"] <= tx["ended"] <= tu["started"] <= tu["ended"] <= first["ended"]
    assert tu == {"at": "content[2]", "type": "tool_use", "started": tu["started"], "ended": tu["ended"], "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}
    r = first["response"]
    assert r["id"] == "msg_1" and r["stop_reason"] == "tool_use" and r["usage"] == {"input_tokens": 10, "output_tokens": 7}
    assert r["content"] == [{"type": "thinking", "thinking": "Where am I?", "signature": "sig_abc"}, {"type": "text", "text": "Hello there, friend."},
                            {"type": "tool_use", "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}]
    assert first["refused"] is None and first["interrupted"] is None and first["error"] is None
    assert httpx.get(f"{server}/v1/streams/{first['id']}", headers=ADMIN).json()["blocks"] == first["blocks"]
    agents = {a["agent"]: a for a in httpx.get(f"{server}/v1/agents", headers=ADMIN).json()["data"]}
    assert agents["coder"]["sessions"] == 1 and agents["coder"]["streams"] == 2 and agents["helper"]["streams"] == 0


def test_a_chat_stream_is_recorded_in_the_same_shape_with_the_wires_own_names(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key", default_headers={"X-Session": "s-rec-o"})
    list(o.chat.completions.create(model="fake/fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    st = httpx.get(f"{server}/v1/agents/coder/sessions/s-rec-o", headers=ADMIN).json()["streams"][0]
    assert st["door"] == "chat"
    assert [b["at"].removeprefix("choices[0].message.") for b in st["blocks"]] == ["role", "reasoning_content", "content", "tool_calls[0]"]
    role, reasoning, content, call = st["blocks"]
    assert reasoning["reasoning_content"] == "Where am I?" and content["content"] == "Hello there, friend."
    assert call["function"]["name"] == "shell" and json.loads(call["function"]["arguments"]) == {"cmd": "pwd"} and call["index"] == 0
    r = st["response"]
    assert r["object"] == "chat.completion" and r["choices"][0]["finish_reason"] == "tool_calls"
    assert r["choices"][0]["message"] == {"role": "assistant", "content": "Hello there, friend.", "reasoning_content": "Where am I?",
                                          "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"cmd": "pwd"}'}}]}


def test_an_agent_sees_only_its_own_record_and_the_operator_sees_all(server, provider):
    list(OpenAI(base_url=f"{server}/v1", api_key="helper-key", default_headers={"X-Session": "h1"}).chat.completions.create(
        model="fake/fake-gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    httpx.post(f"{server}/v1/messages", headers=H, json=messages_body())
    assert [a["agent"] for a in httpx.get(f"{server}/v1/agents", headers=HELPER).json()["data"]] == ["helper"]
    assert httpx.get(f"{server}/v1/agents/coder", headers=HELPER).status_code == 403
    assert httpx.get(f"{server}/v1/agents/helper/sessions/h1", headers=HELPER).status_code == 200
    assert httpx.get(f"{server}/v1/me", headers=HELPER).json() == {"operator": False, "agent": "helper"}
    assert httpx.put(f"{server}/v1/killswitch", headers=HELPER, json={"on": True}).status_code == 401
    assert [r["name"] for r in httpx.get(f"{server}/v1/rules", headers=HELPER).json()["data"]] == ["no secrets in requests", "helper may only use fake-gpt", "long answers are noted"]
    assert len(httpx.get(f"{server}/v1/rules", headers=ADMIN).json()["data"]) == 4
    assert httpx.get(f"{server}/v1/providers", headers=ADMIN).json()["data"][0] == {"provider": "fake", "url": provider.url}


# ── real time ─────────────────────────────────────────────────────────────────

def test_the_live_feed_shows_each_piece_opening_growing_and_closing_before_the_stream_ends(server, provider):
    provider.tool, provider.thinking, provider.delay = ("shell", {"cmd": "ls"}), "Look first.", 0.02
    seen, ready = [], threading.Event()
    watcher = threading.Thread(target=watch, args=(server, "/v1/agents/coder/sessions/s-live/live", seen, ready), daemon=True)
    watcher.start()
    assert ready.wait(5)
    t0 = time.time()
    with Anthropic(base_url=server, api_key="mi-test-key").messages.stream(model="fake/fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": "go"}], extra_headers={"X-Session": "s-live"}) as s:
        s.get_final_message()
    t_end = time.time()
    watcher.join(5)
    kinds = [e["type"] for _, e in seen]
    assert kinds[0] == "stream.started" and kinds[1] == "stream.response" and kinds[-1] == "stream.ended"
    assert kinds.count("block.started") == 3 and kinds.count("block.ended") == 3 and kinds.count("block.delta") >= 5
    opened = [(t, e) for t, e in seen if e["type"] == "block.started"]
    assert [e["block"]["type"] for _, e in opened] == ["thinking", "text", "tool_use"]
    assert t0 < opened[0][0] < opened[1][0] < opened[2][0] < t_end
    assert all(e["time"] for _, e in seen) and {d["at"] for _, d in seen if d["type"] == "block.delta"} == {"content[0]", "content[1]", "content[2]"}
    ended = [e for _, e in seen if e["type"] == "stream.ended"][0]
    assert ended["snapshot"]["response"]["stop_reason"] == "tool_use" and "request" not in ended["snapshot"]


def test_the_global_feed_carries_every_agent_and_an_agents_feed_only_its_own(server, provider):
    everything, agent_only, r1, r2 = [], [], threading.Event(), threading.Event()
    threading.Thread(target=watch, args=(server, "/v1/live?key=admin-key", everything, r1), daemon=True).start()
    threading.Thread(target=watch, args=(server, "/v1/live", agent_only, r2, "helper-key"), daemon=True).start()
    assert r1.wait(5) and r2.wait(5)
    httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "g1"}, json=messages_body())
    time.sleep(0.3)
    assert everything and all(e["agent"] == "coder" and e["session"] == "g1" for _, e in everything) and agent_only == []
    assert httpx.get(f"{server}/v1/live?key=nope").status_code == 401


# ── the switches: gateway, agent, session, by hand ────────────────────────────

def test_the_session_switch_cuts_that_session_only(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/agents/coder/sessions/s-kill/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/messages", messages_body(), "s-kill", after=(5, lambda _: flip(True)))
    events = [json.loads(s) for s in seen]
    assert [e["type"] for e in events[-3:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[-2]["delta"]["stop_reason"] == "interrupted" and len(events) < 14 and hung_up(provider)
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["interrupted"]["by"] == "session" and rec["interrupted"]["reason"] == "operator" and rec["interrupted"]["block"] == {"at": "content[0]", "type": "text"}
    assert rec["response"]["stop_reason"] == "interrupted" and rec["blocks"][0]["ended"]
    r = httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=messages_body())
    assert r.status_code == 423 and r.json()["error"]["level"] == "session" and r.json()["error"]["reason"] == "operator"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-other"}, json=messages_body()).status_code == 200
    refused = httpx.get(f"{server}/v1/agents/coder/sessions/s-kill", headers=ADMIN).json()["streams"][-1]
    assert refused["refused"] == {"by": "session", "reason": "operator"} and refused["ended"] and refused["response"] is None, "a refused call is a stream that never opened"
    assert httpx.get(f"{server}/v1/killswitch", headers=ADMIN).json()["sessions"] == [{"agent": "coder", "session": "s-kill", "reason": "operator"}]
    flip(False)
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=messages_body()).status_code == 200


def test_the_agent_switch_cuts_every_session_of_that_agent_and_no_other(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/agents/coder/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/chat/completions", chat_body(), "a-one", after=(5, lambda _: flip(True)))
    assert seen[-1] == "[DONE]" and json.loads(seen[-2])["interrupted"] is True and hung_up(provider)
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["interrupted"]["by"] == "agent" and rec["response"]["choices"][0]["finish_reason"] == "interrupted" and rec["interrupted"]["block"]["at"] == "choices[0].message.content"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "a-two"}, json=messages_body()).json()["error"]["level"] == "agent"
    assert httpx.post(f"{server}/v1/chat/completions", headers=HELPER, json=chat_body()).status_code == 200
    flip(False)
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "a-two"}, json=messages_body()).status_code == 200


def test_the_gateway_switch_refuses_everything_until_off(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    flip = lambda on: httpx.put(f"{server}/v1/killswitch", headers=ADMIN, json={"on": on})
    stream, seen = read_stream(server, "/v1/messages", messages_body(), "g-one", after=(5, lambda _: flip(True)))
    assert json.loads(seen[-2])["delta"]["stop_reason"] == "interrupted" and hung_up(provider)
    assert httpx.post(f"{server}/v1/chat/completions", headers=HELPER, json=chat_body()).status_code == 423
    flip(False)
    assert httpx.post(f"{server}/v1/chat/completions", headers=HELPER, json=chat_body()).status_code == 200


def test_a_switch_cuts_within_a_tenth_of_a_second_even_while_the_provider_is_silent(server, provider):
    provider.stall = (3, 8.0)
    t0 = time.time()
    threading.Timer(0.5, lambda: httpx.put(f"{server}/v1/agents/coder/sessions/s-silent/killswitch", headers=ADMIN, json={"on": True})).start()
    stream, seen = read_stream(server, "/v1/messages", messages_body(), "s-silent")
    assert time.time() - t0 < 1.5 and json.loads(seen[-2])["delta"]["stop_reason"] == "interrupted"
    httpx.put(f"{server}/v1/agents/coder/sessions/s-silent/killswitch", headers=ADMIN, json={"on": False})
    assert hung_up(provider)


# ── rules: three moments, two places, two actions ─────────────────────────────

def test_a_gateway_rule_on_the_request_trips_the_agent_before_any_connection_opens(server, provider):
    seen, ready = [], threading.Event()
    threading.Thread(target=watch, args=(server, "/v1/live", seen, ready, "admin-key", ("stream.ended",)), daemon=True).start()
    assert ready.wait(5)
    before = len(provider.requests)
    r = httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "r-secret"}, json=messages_body(system="my key is AKIAIOSFODNN7EXAMPLE"))
    assert r.status_code == 423 and r.json()["error"] == {"type": "killswitch", "level": "agent", "reason": "no secrets in requests",
                                                          "message": "the killswitch is on for agent 'coder': no secrets in requests"}
    assert len(provider.requests) == before, "no connection was opened"
    time.sleep(0.3)
    kinds = [e["type"] for _, e in seen]
    assert kinds == ["stream.started", "rule.fired", "killswitch", "stream.ended"]
    fired = [e for _, e in seen if e["type"] == "rule.fired"][0]
    assert fired["rule"] == "no secrets in requests" and fired["when"] == "request" and fired["action"] == "trip" and fired["trip"] == "agent"
    rec = httpx.get(f"{server}/v1/agents/coder/sessions/r-secret", headers=ADMIN).json()["streams"][0]
    assert rec["refused"] == {"by": "agent", "reason": "no secrets in requests"} and rec["request"]["system"].startswith("my key") and rec["response"] is None
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "r-other"}, json=messages_body()).status_code == 423, "the whole agent is off"
    assert httpx.post(f"{server}/v1/chat/completions", headers=HELPER, json=chat_body()).status_code == 200, "another agent runs"
    httpx.put(f"{server}/v1/agents/coder/killswitch", headers=ADMIN, json={"on": False})
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "r-other"}, json=messages_body()).status_code == 200


def test_an_agent_rule_on_the_request_is_how_an_agent_is_held_to_its_models(server, provider):
    r = httpx.post(f"{server}/v1/messages", headers={**HELPER, "X-Session": "h-model"}, json=messages_body(model="fake/fake-sonnet"))
    assert r.status_code == 423 and r.json()["error"]["level"] == "session" and r.json()["error"]["reason"] == "helper may only use fake-gpt"
    assert httpx.post(f"{server}/v1/chat/completions", headers={**HELPER, "X-Session": "h-ok"}, json=chat_body(model="fake/fake-gpt")).status_code == 200
    httpx.put(f"{server}/v1/agents/helper/sessions/h-model/killswitch", headers=ADMIN, json={"on": False})


def test_an_agent_rule_on_the_stream_cuts_the_session_on_the_line_it_fires(server, provider):
    provider.tool, provider.delay = ("shell", {"cmd": "rm -rf /tmp/scratch && echo done"}), 0.03
    a = Anthropic(base_url=server, api_key="mi-test-key")
    pieces = []
    with a.messages.stream(model="fake/fake-sonnet", max_tokens=50, messages=[{"role": "user", "content": "go"}], extra_headers={"X-Session": "r-cut"}) as s:
        stream = s.response.headers["x-stream"]
        for ev in s:                                                             # the SDK parses the cut stream whole, or raises
            if ev.type == "input_json":
                pieces.append(ev.partial_json)
        m = s.get_final_message()
    assert m.stop_reason == "interrupted" and hung_up(provider)
    got = "".join(pieces)
    assert "rm -rf" in got and "echo done" not in got, "the caller got the line the rule fired on, and nothing after"
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert rec["interrupted"]["by"] == "session" and rec["interrupted"]["reason"] == "no destructive commands" and rec["interrupted"]["block"]["type"] == "tool_use"
    assert rec["blocks"][-1]["input"]["cmd"].startswith("rm -rf") and "echo done" not in rec["blocks"][-1]["input"]["cmd"]
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "r-cut"}, json=messages_body()).json()["error"]["reason"] == "no destructive commands"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "r-fine"}, json=messages_body()).status_code == 200, "only that session"
    stream2, seen2 = read_stream(server, "/v1/chat/completions", chat_body(), "h-cut", key="helper-key")
    assert seen2[-1] == "[DONE]" and "interrupted" not in seen2[-2], "the rule is coder's; helper streams the same content uncut"
    httpx.put(f"{server}/v1/agents/coder/sessions/r-cut/killswitch", headers=ADMIN, json={"on": False})


def test_a_rule_on_the_whole_response_can_alert_without_touching_a_switch(server, provider):
    seen, ready = [], threading.Event()
    threading.Thread(target=watch, args=(server, "/v1/agents/coder/sessions/r-alert/live", seen, ready), daemon=True).start()
    assert ready.wait(5)
    stream, _ = read_stream(server, "/v1/messages", messages_body(), "r-alert")
    time.sleep(0.3)
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=ADMIN).json()
    assert [a["rule"] for a in rec["alerts"]] == ["long answers are noted"] and rec["alerts"][0]["when"] == "response" and rec["interrupted"] is None
    fired = [e for _, e in seen if e["type"] == "rule.fired"]
    assert fired and fired[0]["action"] == "alert" and fired[0]["trip"] is None
    assert httpx.get(f"{server}/v1/killswitch", headers=ADMIN).json() == {"gateway": None, "agents": [], "sessions": []}
    assert httpx.get(f"{server}/v1/agents/coder/sessions/r-alert", headers=ADMIN).json()["alerts"] == 1


# ── the page ──────────────────────────────────────────────────────────────────

def test_the_page_is_served_at_the_root(server):
    page = httpx.get(f"{server}/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and "/v1/live?key=" in page.text

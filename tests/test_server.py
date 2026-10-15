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


def watch(server, session, seen, ready):
    """Follow a session's live feed, stamping each record event with when it arrived."""
    with httpx.stream("GET", f"{server}/v1/sessions/{session}/live", headers=H, timeout=30) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                e = json.loads(line[5:])
                if "session" in e and "type" not in e:
                    ready.set()
                    continue
                seen.append((time.time(), e))
                if e["type"] == "stream.ended":
                    return


# ── pass-through ──────────────────────────────────────────────────────────────

def test_messages_passes_through_verbatim_with_the_alias_swapped(server, provider):
    with httpx.stream("POST", f"{server}/v1/messages", headers={**H, "anthropic-beta": "some-beta"}, json=anthropic_body(metadata={"user_id": "u1"}, brand_new_field=1)) as r:
        assert r.status_code == 200 and r.headers["x-stream"].startswith("str_")
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
    provider.tool, provider.thinking = ("shell", {"cmd": "ls"}), "Look first."
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": "hi"}]) as s:
        assert "".join(s.text_stream) == "Hello there, friend."
        m = s.get_final_message()
    assert [b.type for b in m.content] == ["thinking", "text", "tool_use"] and m.content[2].input == {"cmd": "ls"} and m.stop_reason == "tool_use"
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
    assert httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body("gpt")).status_code == 400
    assert httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body("coder")).status_code == 400
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "../x"}, json=anthropic_body()).status_code == 400


def test_a_providers_refusal_passes_through_with_its_status_and_body(server):
    r = httpx.post(f"{server}/v1/messages", headers=H, json=anthropic_body("broken"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "prompt is too long"
    r = httpx.post(f"{server}/v1/chat/completions", headers=H, json=openai_body("broken-gpt"))
    assert r.status_code == 400 and "prompt is too long" in r.text


# ── the record: sessions of streams of blocks ─────────────────────────────────

def test_a_session_holds_streams_of_blocks_with_times_at_every_level(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    a = Anthropic(base_url=server, api_key="mi-test-key")
    for ask in ("first", "second"):
        with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": ask}], extra_headers={"X-Session": "s-rec"}) as s:
            s.get_final_message()
    s = httpx.get(f"{server}/v1/sessions/s-rec", headers=H).json()
    assert s["session"] == "s-rec" and s["started"] and s["ended"] and len(s["streams"]) == 2
    first = s["streams"][0]
    assert first["started"] <= first["ended"] <= s["ended"] and first["request"]["messages"] == [{"role": "user", "content": "first"}]
    assert first["message"] == {"id": "msg_1", "model": "fake-sonnet", "role": "assistant"}
    assert [b["type"] for b in first["blocks"]] == ["thinking", "text", "tool_use"]
    th, tx, tu = first["blocks"]
    assert th["thinking"] == "Where am I?" and th["signature"] == "sig_abc" and th["started"] <= th["ended"] <= tx["started"]
    assert tx["text"] == "Hello there, friend." and tx["ended"] <= tu["started"] <= tu["ended"] <= first["ended"]
    assert tu == {"index": 2, "type": "tool_use", "started": tu["started"], "ended": tu["ended"], "id": "t1", "name": "shell", "input": {"cmd": "pwd"}}
    assert first["stop_reason"] == "tool_use" and first["usage"] == {"input_tokens": 10, "output_tokens": 7} and first["interrupted"] is False
    assert httpx.get(f"{server}/v1/streams/{first['id']}", headers=H).json()["blocks"] == first["blocks"]
    assert httpx.get(f"{server}/v1/sessions", headers=H).json()["data"][0] == {"session": "s-rec", "started": s["started"], "ended": s["ended"], "streams": 2, "interrupted": 0}
    assert "blocks" not in httpx.get(f"{server}/v1/sessions/s-rec", headers=H, params={"blocks": "false"}).json()["streams"][0]


def test_an_openai_stream_is_recorded_in_the_same_shape_with_its_own_block_types(server, provider):
    provider.tool, provider.thinking = ("shell", {"cmd": "pwd"}), "Where am I?"
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key", default_headers={"X-Session": "s-oai"})
    list(o.chat.completions.create(model="gpt", stream=True, messages=[{"role": "user", "content": "hey"}]))
    st = httpx.get(f"{server}/v1/sessions/s-oai", headers=H).json()["streams"][0]
    assert st["format"] == "openai" and st["message"] == {"id": "chatcmpl-1", "model": "fake-gpt", "role": "assistant"}
    assert [b["type"] for b in st["blocks"]] == ["reasoning", "content", "tool_call"]
    assert st["blocks"][0]["reasoning"] == "Where am I?" and st["blocks"][1]["content"] == "Hello there, friend."
    assert st["blocks"][2]["name"] == "shell" and json.loads(st["blocks"][2]["arguments"]) == {"cmd": "pwd"} and st["blocks"][2]["call_index"] == 0
    assert all(b["started"] <= b["ended"] for b in st["blocks"]) and st["stop_reason"] == "tool_use" and st["finish_reason"] == "tool_calls"


# ── real time: the live feed shows blocks as they open, grow and close ────────

def test_the_live_feed_shows_each_block_opening_growing_and_closing_before_the_stream_ends(server, provider):
    provider.tool, provider.thinking, provider.delay = ("shell", {"cmd": "ls"}), "Look first.", 0.02
    seen, ready = [], threading.Event()
    watcher = threading.Thread(target=watch, args=(server, "s-live", seen, ready), daemon=True)
    watcher.start()
    assert ready.wait(5)
    t0 = time.time()
    a = Anthropic(base_url=server, api_key="mi-test-key")
    with a.messages.stream(model="coder", max_tokens=50, messages=[{"role": "user", "content": "go"}], extra_headers={"X-Session": "s-live"}) as s:
        s.get_final_message()
    t_end = time.time()
    watcher.join(5)
    kinds = [e["type"] for _, e in seen]
    assert kinds[0] == "stream.started" and kinds[1] == "stream.message" and kinds[-1] == "stream.ended"
    assert kinds.count("block.started") == 3 and kinds.count("block.ended") == 3 and kinds.count("block.delta") >= 5
    opened = [(t, e) for t, e in seen if e["type"] == "block.started"]
    assert [e["block"]["type"] for _, e in opened] == ["thinking", "text", "tool_use"]
    assert t0 < opened[0][0] < opened[1][0] < opened[2][0] < t_end, "blocks must arrive while the stream is still running, in order"
    assert all(e["block"]["ended"] is None for _, e in opened) and all(e["block"]["ended"] for _, e in seen if e["type"] == "block.ended")
    deltas = [e for _, e in seen if e["type"] == "block.delta"]
    assert {d["field"] for d in deltas} == {"thinking", "signature", "text", "input"}
    ended = [e for _, e in seen if e["type"] == "stream.ended"][0]
    assert ended["stream"] and ended["stream"]["stop_reason"] == "tool_use" and "request" not in ended["stream"]


def test_the_live_feed_works_for_chat_completions_too(server, provider):
    provider.thinking, provider.delay = "Think.", 0.02
    seen, ready = [], threading.Event()
    threading.Thread(target=watch, args=(server, "s-live-o", seen, ready), daemon=True).start()
    assert ready.wait(5)
    o = OpenAI(base_url=f"{server}/v1", api_key="mi-test-key", default_headers={"X-Session": "s-live-o"})
    list(o.chat.completions.create(model="gpt", stream=True, messages=[{"role": "user", "content": "go"}]))
    time.sleep(0.3)
    opened = [e["block"]["type"] for _, e in seen if e["type"] == "block.started"]
    assert opened == ["reasoning", "content"] and [e["type"] for _, e in seen][-1] == "stream.ended"


# ── the killswitch ────────────────────────────────────────────────────────────

def read_stream(server, path, body, session, after=None):
    seen = []
    with httpx.stream("POST", f"{server}{path}", headers={**H, "X-Session": session}, json=body) as r:
        stream = r.headers["x-stream"]
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(line[5:].strip())
                if after and len(seen) == after[0]:
                    after[1](stream)
    return stream, seen


def test_the_session_killswitch_cuts_a_running_stream_and_records_the_block_it_landed_in(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-kill",
                               after=(5, lambda _: httpx.put(f"{server}/v1/killswitch/s-kill", headers=H, json={"on": True})))
    events = [json.loads(s) for s in seen]
    assert [e["type"] for e in events[-3:]] == ["content_block_stop", "message_delta", "message_stop"]
    assert events[-2]["delta"]["stop_reason"] == "interrupted" and len(events) < 14 and provider.closed_early
    rec = httpx.get(f"{server}/v1/streams/{stream}", headers=H).json()
    assert rec["interrupted"] is True and rec["stop_reason"] == "interrupted" and rec["cut"]["by"] == "killswitch"
    assert rec["cut"]["block"] == {"index": 0, "type": "text"} and rec["blocks"][0]["text"].startswith("one two") and rec["blocks"][0]["ended"]
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body()).status_code == 423
    httpx.put(f"{server}/v1/killswitch/s-kill", headers=H, json={"on": False})
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "s-kill"}, json=anthropic_body()).status_code == 200


def test_the_global_killswitch_cuts_an_openai_stream_and_refuses_everything_until_off(server, provider):
    provider.text, provider.delay = "one two three four five six seven eight nine ten", 0.03
    stream, seen = read_stream(server, "/v1/chat/completions", openai_body(), "s-any",
                               after=(5, lambda _: httpx.put(f"{server}/v1/killswitch", headers=H, json={"on": True})))
    last = json.loads(seen[-2])
    assert seen[-1] == "[DONE]" and last["interrupted"] is True and provider.closed_early
    assert httpx.get(f"{server}/v1/streams/{stream}", headers=H).json()["cut"]["block"]["type"] == "content"
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "anything"}, json=anthropic_body()).status_code == 423
    httpx.put(f"{server}/v1/killswitch", headers=H, json={"on": False})
    assert httpx.post(f"{server}/v1/messages", headers={**H, "X-Session": "anything"}, json=anthropic_body()).status_code == 200


def test_the_killswitch_cuts_within_a_tenth_of_a_second_even_while_the_provider_is_silent(server, provider):
    provider.stall = (3, 8.0)
    t0 = time.time()
    threading.Timer(0.5, lambda: httpx.put(f"{server}/v1/killswitch/s-silent", headers=H, json={"on": True})).start()
    stream, seen = read_stream(server, "/v1/messages", anthropic_body(), "s-silent")
    assert time.time() - t0 < 1.5
    assert json.loads(seen[-2])["delta"]["stop_reason"] == "interrupted"
    httpx.put(f"{server}/v1/killswitch/s-silent", headers=H, json={"on": False})
    time.sleep(0.3)
    assert provider.closed_early

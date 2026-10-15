from model_interface.formats import PLATFORM_RESULT, PLATFORM_USE
from model_interface.loop import Loop

from conftest import call, reply, text


def types(content):
    return [b["type"] for b in content]


def request(**extra):
    return {"model": "coder", "max_tokens": 100, "system": "sys", "tools": [{"name": "shell"}],
            "messages": [{"role": "user", "content": "fix the tests"}], **extra}


def test_the_loop_runs_platform_tools_until_the_model_answers(config):
    loop = Loop(config.models, config.tools, config.sandboxes)
    response = loop.run(request(), "s1")
    assert types(response["content"]) == ["text", PLATFORM_USE, PLATFORM_RESULT, PLATFORM_USE, PLATFORM_RESULT, "text"]
    assert response["stop_reason"] == "end_turn" and response["usage"] == {"input_tokens": 30, "output_tokens": 15}
    assert config.sandboxes.get("s1").ran == ["python3 -m unittest -q", "sed -i fix prices.py && python3 -m unittest -q"]
    assert response["content"][2]["content"] == "FAIL: 15 != 22.5" and response["content"][4]["content"] == "OK"


def test_a_registered_tool_reaches_the_provider_with_its_full_definition(config):
    Loop(config.models, config.tools, config.sandboxes).run(request(), "s1")
    provider = config.models["coder"].provider
    assert provider.seen[0]["tools"][0]["description"].startswith("Run a command in sh")
    assert provider.seen[0]["model"] == "fake-model"
    assert [m["role"] for m in provider.seen[2]["messages"]] == ["user", "assistant", "user", "assistant", "user"]


def test_the_next_turn_sends_the_folded_transcript_and_the_provider_sees_real_turns(config):
    loop = Loop(config.models, config.tools, config.sandboxes)
    first = loop.run(request(), "s1")
    provider = config.models["coder"].provider
    provider.script.append(reply(text("Pushed.")))
    loop.run(request(messages=[{"role": "user", "content": "fix the tests"}, {"role": "assistant", "content": first["content"]}, {"role": "user", "content": "push it"}]), "s1")
    assert [m["role"] for m in provider.seen[3]["messages"]] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert all(b["type"] in ("text", "tool_use", "tool_result") for m in provider.seen[3]["messages"] if isinstance(m["content"], list) for b in m["content"])


def test_a_tool_of_theirs_hands_back_after_ours_has_run(config):
    provider = config.models["coder"].provider
    provider.script[:] = [reply(call("t1", cmd="ls"), call("s1", "search", q="x"), stop="tool_use")]
    response = Loop(config.models, config.tools, config.sandboxes).run(request(tools=[{"name": "shell"}, {"name": "search", "input_schema": {}}]), "s1")
    assert types(response["content"]) == [PLATFORM_USE, PLATFORM_RESULT, "tool_use"] and response["stop_reason"] == "tool_use"
    assert provider.seen[0]["tools"][1] == {"name": "search", "input_schema": {}}


def test_each_session_gets_its_own_sandbox(config):
    loop = Loop(config.models, config.tools, config.sandboxes)
    loop.run(request(), "a")
    config.models["coder"].provider.script[:] = [reply(call("t9", cmd="pwd"), stop="tool_use"), reply(text("done"))]
    loop.run(request(), "b")
    assert config.sandboxes.get("a").ran[0] == "python3 -m unittest -q" and config.sandboxes.get("b").ran == ["pwd"]

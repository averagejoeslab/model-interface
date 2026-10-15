import pytest
from fastapi.testclient import TestClient

from model_interface.server import create_app

from conftest import reply, text


@pytest.fixture
def client(config):
    return TestClient(create_app(config))


def test_messages_endpoint_runs_the_loop_and_answers_in_messages_format(client, config):
    r = client.post("/v1/messages", headers={"x-api-key": "mi-test-key", "X-Session": "demo"},
                    json={"model": "coder", "max_tokens": 100, "tools": [{"name": "shell"}], "messages": [{"role": "user", "content": "fix the tests"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "message" and body["stop_reason"] == "end_turn"
    assert [b["type"] for b in body["content"]] == ["text", "platform_tool_use", "platform_tool_result", "platform_tool_use", "platform_tool_result", "text"]
    assert config.sandboxes.get("demo").ran[0] == "python3 -m unittest -q"


def test_chat_completions_endpoint_runs_the_same_loop_and_answers_in_openai_format(client, config):
    r = client.post("/v1/chat/completions", headers={"authorization": "Bearer mi-test-key"},
                    json={"model": "coder", "tools": [{"type": "function", "function": {"name": "shell"}}], "messages": [{"role": "user", "content": "fix the tests"}]})
    assert r.status_code == 200, r.text
    message = r.json()["choices"][0]["message"]
    assert message["content"] == "Running the tests.Fixed." and len(message["platform"]) == 6
    assert config.sandboxes.get("default").ran == ["python3 -m unittest -q", "sed -i fix prices.py && python3 -m unittest -q"]


def test_unknown_key_model_session_and_streaming_are_refused(client):
    good = {"model": "coder", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/messages", headers={"x-api-key": "wrong"}, json=good).status_code == 401
    assert client.post("/v1/messages", headers={"x-api-key": "mi-test-key"}, json={**good, "model": "nope"}).status_code == 404
    assert client.post("/v1/messages", headers={"x-api-key": "mi-test-key", "X-Session": "../etc"}, json=good).status_code == 400
    assert client.post("/v1/messages", headers={"x-api-key": "mi-test-key"}, json={**good, "stream": True}).status_code == 400


def test_models_endpoint_lists_aliases_only(client):
    r = client.get("/v1/models", headers={"x-api-key": "mi-test-key"})
    assert r.json()["data"] == [{"id": "coder", "object": "model"}]


def test_a_text_only_answer_has_no_platform_blocks(client, config):
    config.models["coder"].provider.script[:] = [reply(text("hello"))]
    r = client.post("/v1/chat/completions", headers={"x-api-key": "mi-test-key"}, json={"model": "coder", "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["choices"][0]["message"] == {"role": "assistant", "content": "hello"}

"""Providers, models, agents and the file they are read from."""
import os
import textwrap

import pytest

from model_interface.config import load
from model_interface.providers import Agent, Provider, format_of


def test_the_endpoint_says_the_format_and_the_sdks_base_url():
    p = Provider("deepseek", "https://api.deepseek.com/anthropic/v1/messages", "k", ["deepseek-chat"])
    assert p.format.name == "anthropic" and p.url == "https://api.deepseek.com/anthropic" and p.models[0].id == "deepseek/deepseek-chat"
    g = Provider("groq", "https://api.groq.com/openai/v1/chat/completions/", "k", ["llama-3.3-70b-versatile"])
    assert g.format.name == "openai" and g.url == "https://api.groq.com/openai/v1"
    with pytest.raises(ValueError, match="/v1/messages or /chat/completions"):
        format_of("https://api.example.com/v1/responses")


def test_an_agent_resolves_a_model_name_on_the_format_it_called_and_may_not_be_ambiguous():
    direct = Provider("deepseek", "https://api.deepseek.com/v1/chat/completions", "k", ["deepseek-chat"])
    via = Provider("openrouter", "https://openrouter.ai/api/v1/chat/completions", "k", ["deepseek-chat"])
    messages = Provider("deepseek-messages", "https://api.deepseek.com/anthropic/v1/messages", "k", ["deepseek-chat"])
    both = Agent("a", ["k"], [direct.model("deepseek-chat"), messages.model("deepseek-chat")])
    assert both.resolve(direct.format, "deepseek-chat").provider is direct
    assert both.resolve(messages.format, "deepseek-chat").provider is messages
    assert both.resolve(direct.format, "other") is None
    with pytest.raises(ValueError, match="one provider per format"):
        Agent("b", ["k"], [direct.model("deepseek-chat"), via.model("deepseek-chat")])


def test_the_file_is_loaded_with_secrets_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("T_ADMIN", "adm")
    monkeypatch.setenv("T_CODER", "ck")
    monkeypatch.setenv("T_PROV", "pk")
    f = tmp_path / "mi.toml"
    f.write_text(textwrap.dedent('''
        admin = "env:T_ADMIN"
        record = "/tmp/x.jsonl"

        [providers.anthropic]
        endpoint = "https://api.anthropic.com/v1/messages"
        key = "env:T_PROV"
        models = ["claude-sonnet-5-5", "claude-haiku-5-5"]

        [providers.ollama]
        endpoint = "http://localhost:11434/v1/chat/completions"
        models = ["qwen2.5:3b"]

        [agents.coder]
        keys = ["env:T_CODER", "plain-second-key"]
        models = ["anthropic/claude-sonnet-5-5", "ollama/qwen2.5:3b"]
    '''))
    c = load(str(f))
    assert c.admin == "adm" and c.record == "/tmp/x.jsonl"
    assert c.providers["anthropic"].key == "pk" and c.providers["ollama"].key == ""
    assert c.agent_of("ck").name == "coder" and c.agent_of("plain-second-key").name == "coder" and c.agent_of("") is None
    assert [m.id for m in c.agents["coder"].models] == ["anthropic/claude-sonnet-5-5", "ollama/qwen2.5:3b"]
    f.write_text(f.read_text().replace('"ollama/qwen2.5:3b"', '"ollama/nope"'))
    with pytest.raises(ValueError, match="has no model 'nope'"):
        load(str(f))

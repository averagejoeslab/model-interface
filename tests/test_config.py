"""Providers, agents, rules, and the file they are read from."""
import textwrap

import pytest

from model_interface.config import Config, load
from model_interface.providers import Agent, Provider
from model_interface.rules import Rule


def test_a_provider_is_a_name_a_url_and_a_key_and_an_agent_a_name_and_keys():
    p = Provider("deepseek", "https://api.deepseek.com", "k")
    assert (p.name, p.url, p.key) == ("deepseek", "https://api.deepseek.com", "k") and not hasattr(p, "format")
    assert Agent("a", ["k1", "k2"]).keys == ["k1", "k2"]


def test_a_rule_knows_where_it_may_trip():
    assert Rule("g", "true").trip == "agent" and Rule("a", "true", on="coder").trip == "session"
    assert Rule("g2", "true", trip="session").trip == "session"
    with pytest.raises(ValueError, match="for people"):
        Rule("bad", "true", on="coder", trip="agent")
    with pytest.raises(ValueError, match="for people"):
        Rule("bad", "true", trip="gateway")
    with pytest.raises(ValueError, match="when must be"):
        Rule("bad", "true", when="sometime")
    with pytest.raises(ValueError, match="rule 'bad'"):
        Rule("bad", "this is not CEL (")


def test_a_rule_on_an_unknown_agent_is_refused():
    with pytest.raises(ValueError, match="not an agent"):
        Config(agents={}, rules=[Rule("r", "true", on="ghost")])


def test_the_file_is_loaded_with_secrets_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("T_ADMIN", "adm"); monkeypatch.setenv("T_CODER", "ck"); monkeypatch.setenv("T_PROV", "pk")
    f = tmp_path / "mi.toml"
    f.write_text(textwrap.dedent('''
        admin = "env:T_ADMIN"
        record = "/tmp/x.jsonl"

        [providers.anthropic]
        url = "https://api.anthropic.com"
        key = "env:T_PROV"

        [providers.ollama]
        url = "http://localhost:11434"

        [agents.coder]
        keys = ["env:T_CODER", "plain-second-key"]

        [[rules]]
        name = "coder stays on small models"
        on = "coder"
        when = "request"
        expr = 'model != "claude-haiku-5-5"'

        [[rules]]
        name = "no keys anywhere"
        when = "stream"
        expr = 'text.matches("sk-[A-Za-z0-9]{20,}")'
        action = "alert"
    '''))
    c = load(str(f))
    assert c.admin == "adm" and c.providers["anthropic"].key == "pk" and c.providers["ollama"].key == ""
    assert c.agent_of("ck").name == "coder" and c.agent_of("plain-second-key").name == "coder" and c.agent_of("") is None
    assert [(r.name, r.on, r.when, r.action, r.trip) for r in c.rules] == [
        ("coder stays on small models", "coder", "request", "trip", "session"), ("no keys anywhere", "gateway", "stream", "alert", "agent")]

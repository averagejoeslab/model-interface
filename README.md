# model-interface

A gateway for model calls, with real-time observability and a killswitch. The full definition is in [SPEC.md](SPEC.md).

An agent points its SDK at the gateway instead of at a provider, presents its agent key, and names the model as the provider names it. The gateway forwards the call in the provider's own wire format with the provider's own SDK, holding the provider's key, and reads a copy of every line of the response into a record as it streams back. The record is live: open the page and watch each block open, grow and close while the model is still talking. An operator can stop any of it, at the gateway, at an agent, or at a session.

```
agent (any SDK)                gateway                                          provider
POST /v1/messages ───────────► agent? switched off? model granted? ──────────► any /v1/messages endpoint
POST /v1/chat/completions ───► the same, with the OpenAI SDK ────────────────► any /chat/completions endpoint
                               │  each line: read a copy into the record, check the switch, forward verbatim
                               ▼
GET /                          the page: agents → sessions → streams → blocks, live, with the switches
GET /v1/live                   the record's events as they happen
PUT /v1/killswitch             the gateway switch; /v1/agents/{a}/killswitch; /v1/agents/{a}/sessions/{s}/killswitch
```

Any provider that serves either wire format is a provider here: Anthropic, OpenAI, DeepSeek, Groq, OpenRouter, Ollama, vLLM, and so on. Nothing is translated and nothing is executed. Rules over the record, configuration in the UI, and tools in sandboxes come next, on top of this.

## Running it

```bash
cp model-interface.example.toml model-interface.toml   # providers with their models, agents with their keys and grants
export MI_ADMIN_KEY=operator-secret CODER_KEY=agent-secret ANTHROPIC_API_KEY=...
uv run model-interface                                 # http://127.0.0.1:8080
```

Open http://127.0.0.1:8080 with the operator's key. Then make a call from any SDK with an agent's key. The only things that differ from calling the provider directly are the base URL and the key:

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8080", api_key="agent-secret")
with client.messages.stream(model="claude-sonnet-5-5", max_tokens=300, messages=[{"role": "user", "content": "hi"}],
                            extra_headers={"X-Session": "task-1"}) as s:
    for text in s.text_stream:
        print(text, end="")
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="agent-secret", default_headers={"X-Session": "task-1"})
for chunk in client.chat.completions.create(model="qwen2.5:3b", stream=True, messages=[{"role": "user", "content": "hi"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

The minimal harness agent in `examples/agent.py` does the same with a tool loop:

```bash
MI_URL=http://127.0.0.1:8080 MI_KEY=agent-secret MI_MODEL=claude-sonnet-5-5 MI_SESSION=task-1 uv run examples/agent.py "The tests are failing. Find out why and fix it."
```

Watch it, and stop it, from a shell:

```bash
curl -s -N -H "x-api-key: operator-secret" http://127.0.0.1:8080/v1/agents/coder/sessions/task-1/live
curl -s -X PUT -H "x-api-key: operator-secret" -H "content-type: application/json" -d '{"on": true}' \
     http://127.0.0.1:8080/v1/agents/coder/sessions/task-1/killswitch
```

## The code

| Module | What it is |
|---|---|
| `formats.py` | `Format`: how one format opens a streaming call with its SDK, reads the stream's lines into the record's blocks as they arrive, and ends a cut stream so the message is whole. `AnthropicFormat`, `OpenAIFormat`. |
| `providers.py` | `Provider`: an endpoint, a key, the format read off the endpoint, and its models. `Model`. `Agent`: keys and grants, and how a model name resolves. |
| `killswitch.py` | `Killswitch`: the state at gateway, agent and session level. |
| `gateway.py` | `Stream`, `Session`, `Record` with its watchers, and `Gateway.stream`, the loop over a stream's lines that reads, checks the switch, forwards, and cuts. |
| `server.py` | The endpoints and the page. |
| `ui.html` | The page. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network against a fake provider that speaks both formats over real HTTP, with the gateway on a real port, so streams are live, the feed is real, and the switches cut streams mid-way.

```bash
uv pip install -e ".[dev]" && pytest
```

## License

MIT.

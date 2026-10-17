# model-interface

A model gateway that taps the wire. The full definition is in [SPEC.md](SPEC.md).

Configure providers, create agents, author rules. An agent points its SDK at the gateway with its virtual key and names its model as `provider/model`. The gateway passes the call through verbatim, by the SDK the agent chose by its door, reads every line of the response into a record by shape as it streams, runs the rules in that loop, and can stop any of it at the gateway, an agent or a session.

```
agent (any SDK)                gateway                                                 provider
POST /v1/messages ───────────► who? where? request rules · switches ──── anthropic SDK ──► any root URL
POST /v1/chat/completions ───► the same ───────────────────────────────── openai SDK ────► any root URL
                               │  each line: tap · stream rules · switches · forward verbatim
                               ▼
GET /                          the page: the wire, the transcript, the record, live
GET /v1/live                   the record's events as they happen
PUT /v1/killswitch             by hand; /v1/agents/{a}/killswitch; /v1/agents/{a}/sessions/{s}/killswitch
```

## Running it

```bash
cp model-interface.example.toml model-interface.toml
export MI_ADMIN_KEY=operator-secret CODER_KEY=agent-secret ANTHROPIC_API_KEY=...
uv run model-interface                                 # http://127.0.0.1:8080
```

Open the page with the operator's key. Then call from any SDK with an agent's key:

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8080", api_key="agent-secret")
with client.messages.stream(model="anthropic/claude-sonnet-5-5", max_tokens=300, messages=[{"role": "user", "content": "hi"}],
                            extra_headers={"X-Session": "task-1"}) as s:
    for text in s.text_stream:
        print(text, end="")
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="agent-secret", default_headers={"X-Session": "task-1"})
for chunk in client.chat.completions.create(model="ollama/qwen2.5:3b", stream=True, messages=[{"role": "user", "content": "hi"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

A rule, in the config:

```toml
[[rules]]
name = "no destructive commands"
on = "coder"
when = "stream"
expr = 'text.contains("rm -rf")'
```

## The code

| Module | What it is |
|---|---|
| `doors.py` | The two doors: each one's SDK, the envelope of its stream, how to compose the whole response, how to end a cut stream. |
| `tap.py` | The merge that folds any delta into any piece by shape, and the reading that applies it as lines pass. |
| `providers.py` | `Provider`: name, URL, key. `Agent`: name, keys. |
| `rules.py` | `Rule`: CEL, a moment, an action, a place. `Rules`: which run when, and what they see. |
| `killswitch.py` | The three levels, with reasons. |
| `record.py` | Streams, sessions, the watchers, the file. |
| `gateway.py` | Routing, the three moments, the loop over a stream's lines. |
| `server.py` | The endpoints and the page. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network against a fake provider that speaks both doors over real HTTP, and against real wire captures from Anthropic, OpenAI and Ollama.

```bash
uv pip install -e ".[dev]" && pytest
```

## License

MIT.

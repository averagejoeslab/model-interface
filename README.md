# model-interface

A gateway for model calls with real-time observability. The full definition is in [SPEC.md](SPEC.md).

An agent points its SDK at the gateway instead of at a provider. The gateway forwards each call in the provider's own wire format with the provider's own SDK, holding the provider's key, and reads a copy of every line of the response into a record as it streams back. The record is live: open the page and watch each block open, grow and close while the model is still talking.

```
caller                         gateway                                     provider
POST /v1/messages ───────────► alias → model, Anthropic SDK, stream ─────► Messages API
POST /v1/chat/completions ───► alias → model, OpenAI SDK, stream ────────► Chat Completions API
                               │  each line: read a copy into the record, forward verbatim
                               ▼
GET /                          the page: callers → sessions → streams → blocks, live
GET /v1/live                   the record's events as they happen, for everything
GET /v1/sessions/{s}/live      the same, for one session
GET /v1/callers, /v1/sessions, /v1/sessions/{s}, /v1/streams/{id}
```

Nothing is translated, nothing is executed, nothing is stopped. The killswitch and rules over the record come next, on top of this.

## Running it

```bash
cp model-interface.example.toml model-interface.toml   # providers, aliases, caller keys, record path
export ANTHROPIC_API_KEY=... MI_KEY=some-secret
uv run model-interface                                 # http://127.0.0.1:8080
```

Open http://127.0.0.1:8080, enter the caller key, then make a call from any SDK:

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8080", api_key="some-secret")
with client.messages.stream(model="coder", max_tokens=300, messages=[{"role": "user", "content": "hi"}],
                            extra_headers={"X-Session": "task-1"}) as s:
    for text in s.text_stream:
        print(text, end="")
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="some-secret", default_headers={"X-Session": "task-1"})
for chunk in client.chat.completions.create(model="local-openai", stream=True, messages=[{"role": "user", "content": "hi"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

The minimal harness agent in `examples/agent.py` does the same with a tool loop:

```bash
MI_URL=http://127.0.0.1:8080 MI_KEY=some-secret MI_SESSION=task-1 uv run examples/agent.py "The tests are failing. Find out why and fix it."
```

Or watch from a shell:

```bash
curl -s -N -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/sessions/task-1/live
curl -s -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/sessions/task-1 | jq '.streams[].blocks[] | {type, started, ended}'
```

## The code

| Module | What it is |
|---|---|
| `formats.py` | `Format`: how one format opens a streaming call with its SDK and reads the stream's lines into the record's blocks as they arrive. `AnthropicFormat`, `OpenAIFormat`. |
| `providers.py` | `Provider`: a URL, a key, a format and that format's client. `Model`: an alias. |
| `gateway.py` | `Stream`, `Session`, `Record` with its watchers, and `Gateway.stream`, the loop over a stream's lines. |
| `server.py` | The endpoints and the page. |
| `ui.html` | The page. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network against a fake provider that speaks both formats over real HTTP, with the gateway on a real port, so streams and the live feed are real.

```bash
uv pip install -e ".[dev]" && pytest
```

## License

MIT.

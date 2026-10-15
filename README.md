# model-interface

One gateway for model calls in both wire formats, each passed through by its own SDK, streaming only, with every call recorded under a session and a killswitch that cuts a call mid-stream.

An agent points its SDK at the gateway instead of at a provider. It can be written from scratch or on any framework; the gateway never sees its loop or its tools. What it sees is every model call, and that is enough to record it, group it into a session, and stop it. Tool execution is a separate service with the same shape, kept apart on purpose.

## The shape

```
caller                         gateway                                   provider
POST /v1/messages ───────────► alias → model, Anthropic SDK, stream ───► Messages API
POST /v1/chat/completions ───► alias → model, OpenAI SDK, stream ──────► Chat Completions API
                               │
                               │  each line of the stream:
                               │    killswitch on?  → cut: close upstream, close the message
                               │    read a copy     → the run's record
                               │    forward verbatim
                               ▼
GET /v1/sessions, /v1/sessions/{s}, /v1/runs/{r}       what was asked, what came back, timings, tokens
GET /v1/killswitch   PUT /v1/killswitch {on}           everything
                     PUT /v1/killswitch/{s} {on}       one session
```

**Native pass-through.** Each endpoint serves one format and hands the body to that format's own SDK with the provider's key. The body is not translated or inspected on the way out; a field the SDK version does not know still goes through, as `extra_body`. The provider's stream lines come back to the caller verbatim. A model alias names a provider, and an alias only answers on its own format's endpoint.

**Streaming only.** A call must ask for a stream, and gets a 400 otherwise. A blocking call has no moment in it to observe or to stop; a stream has one every few milliseconds, and the gateway acts at each: it reads a copy of the line for the record, checks the killswitch, and forwards it.

**Sessions.** Every call is a run, with its id in the `X-Run` header, under the session named in the `X-Session` header. A run's record holds the request as received, the response as read from the stream, time to first line, duration, line count, tokens, stop reason, and whether it was cut. The sessions endpoints show a session's runs in order, so a whole task is readable turn by turn.

**The killswitch.** A flag per session and one for everything. On: any running stream in scope is cut at its next line, the upstream connection closes so the provider stops generating, and the caller's stream ends with a whole message whose stop reason is `interrupted`; new calls in scope are refused with 423 until the flag is off. Off: calls run.

## Running it

```bash
cp model-interface.example.toml model-interface.toml   # providers, aliases, caller keys, record path
export ANTHROPIC_API_KEY=... MI_KEY=some-secret
uv run model-interface                                 # http://127.0.0.1:8080
```

The minimal harness agent streams the model's words, runs its one tool itself, and keeps its own context:

```bash
MI_URL=http://127.0.0.1:8080 MI_KEY=some-secret MI_SESSION=task-1 uv run examples/agent.py "The tests are failing. Find out why and fix it."
```

Watch it, and stop it, from another shell:

```bash
curl -s -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/sessions/task-1 | jq '.runs[] | {asked, tool_calls, answered, stop_reason, duration_ms}'
curl -s -X PUT -H "x-api-key: some-secret" -H "content-type: application/json" -d '{"on": true}' http://127.0.0.1:8080/v1/killswitch/task-1
curl -s -X PUT -H "x-api-key: some-secret" -H "content-type: application/json" -d '{"on": true}' http://127.0.0.1:8080/v1/killswitch
```

## The code

| Module | What it is |
|---|---|
| `formats.py` | `Format`: how one format opens a streaming call with its SDK, reads the stream into a message for the record, and ends an interrupted stream so the message is whole. `AnthropicFormat` and `OpenAIFormat`. |
| `providers.py` | `Provider`: a URL, a key, a format and that format's client. `Model`: an alias. |
| `gateway.py` | `Run`, `Sessions`, `Killswitch`, and `Gateway.stream`, the loop over the stream's lines. |
| `server.py` | The endpoints. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network against a fake provider that speaks both formats over real HTTP, with the gateway on a real port, so streams are live and are cut mid-way by the killswitch.

```bash
uv pip install -e ".[dev]" && pytest
```

## License

MIT.

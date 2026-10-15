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

**The record: sessions of streams of blocks.** A session is named in the `X-Session` header. A stream is one request and the response that streams back for it, with its id in the `X-Stream` header. Blocks are what the response is made of: in Messages, `thinking`, `text` and `tool_use`, straight from the block events on the wire; in Chat Completions, `reasoning`, `content` and one `tool_call` per index, inferred from the deltas. Every level has an id, a start and an end. The request is kept verbatim on the stream; the blocks are the response.

```json
{"session": "task-1", "started": "…", "ended": "…",
 "streams": [
   {"id": "str_…", "started": "…", "ended": "…", "request": { "…verbatim…" },
    "message": {"id": "msg_…", "model": "claude-sonnet-5-5", "role": "assistant"},
    "blocks": [
      {"index": 0, "type": "thinking", "started": "…", "ended": "…", "thinking": "…", "signature": "…"},
      {"index": 1, "type": "text",     "started": "…", "ended": "…", "text": "…"},
      {"index": 2, "type": "tool_use", "started": "…", "ended": "…", "id": "toolu_…", "name": "shell", "input": {"cmd": "…"}}
    ],
    "stop_reason": "tool_use", "usage": {"input_tokens": 448, "output_tokens": 76}, "interrupted": false, "cut": null}
 ]}
```

**It is live.** The record is built from the stream's lines as they are forwarded, not after. A block appears the moment it opens, grows with each delta, and gets its end time when it closes. `GET /v1/sessions/{s}` returns the session as it stands at that instant. `GET /v1/sessions/{s}/live` is a feed of the record's own events as they happen: `stream.started`, `stream.message`, `block.started`, `block.delta`, `block.ended`, `stream.ended`. A policy can be evaluated at any of those moments over any slice: one block, the last few blocks, a stream, or the whole session.

**The killswitch.** A flag per session and one for everything. On: any running stream in scope is cut within a tenth of a second, whatever the provider is doing, the upstream connection closes so the provider stops generating, and the caller's stream ends with a whole message whose stop reason is `interrupted`; the record notes who cut it and in which block. New calls in scope are refused with 423 until the flag is off. Off: calls run.

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
curl -s -N -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/sessions/task-1/live        # blocks as they open, grow and close
curl -s -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/sessions/task-1 | jq '.streams[].blocks[] | {type, started, ended}'
curl -s -X PUT -H "x-api-key: some-secret" -H "content-type: application/json" -d '{"on": true}' http://127.0.0.1:8080/v1/killswitch/task-1
curl -s -X PUT -H "x-api-key: some-secret" -H "content-type: application/json" -d '{"on": true}' http://127.0.0.1:8080/v1/killswitch
```

## The code

| Module | What it is |
|---|---|
| `formats.py` | `Format`: how one format opens a streaming call with its SDK, reads the stream's lines into the record's blocks as they arrive, and ends an interrupted stream so the message is whole. `AnthropicFormat` and `OpenAIFormat`. |
| `providers.py` | `Provider`: a URL, a key, a format and that format's client. `Model`: an alias. |
| `gateway.py` | `Stream`, `Session`, `Record` with its watchers, `Killswitch`, and `Gateway.stream`, the loop over the stream's lines. |
| `server.py` | The endpoints. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network against a fake provider that speaks both formats over real HTTP, with the gateway on a real port, so streams are live and are cut mid-way by the killswitch.

```bash
uv pip install -e ".[dev]" && pytest
```

## License

MIT.

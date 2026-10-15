# model-interface

One endpoint for every model call, in either wire format, with the provider keys held here, every call watched, and every call stoppable while it runs.

An agent points its SDK at this gateway instead of at a provider. It can be written from scratch or on any framework; the gateway never sees its loop or its tools. What it sees is every model call, and that is enough to route it, record it, and stop it. Tool execution is a separate service with the same shape, kept apart on purpose.

## Streaming always on

The gateway always streams from the provider, whatever the caller asked for.

A non-streaming provider call is one blocking request: nothing arrives until the model has finished, so there is no moment to observe it and no way to stop it short. A streamed call arrives as events, one every few milliseconds. Each event is a point where the gateway can check the stop list, time the first token, count tokens, and, if told to, close the upstream connection, which makes the provider stop generating. So every upstream call streams. A caller that asked for a stream gets the events as they come. A caller that asked for a whole message gets the events assembled into one. Observability and interruptibility come from the same mechanism, and they cost the caller nothing.

## What it does

```
caller, in either format                  gateway                          provider
POST /v1/messages ──────────┐
POST /v1/chat/completions ──┤──► key? alias? ──► Run ──► stream ───────────► model
                            │                     │   ◄── events ◄──────────┘
                            │     on each event: stopped? record, pass on
                            │                     │
          events, or one ◄──┴─────────────────────┘
          assembled message
                                 POST /v1/interrupt/{run | session | key | *}
                                 GET  /v1/runs?session=…
```

- **Two endpoints, two formats.** Messages and Chat Completions in, either format out, with the translation done both ways, streams included.
- **Aliases and keys.** Callers name a model alias and present a key of their own. The provider and its key are the operator's, in config, read from the environment.
- **A run per call.** Each call gets a run id, returned in the `X-Run` header, and belongs to a session from the `X-Session` header and to the key that made it.
- **The stop list.** Stop a run, a session, a key, or everything. A running call ends at its next event: the upstream connection closes, the caller's stream closes with `stop_reason: "interrupted"`, and the message is whole, so every SDK handles it. A stopped scope refuses new calls until resumed.
- **The record.** One line per run: who, which model, when, time to first event, duration, events, tokens, stop reason, whether it was interrupted, any error. Kept in memory for `GET /v1/runs` and appended to a file.

## Running it

```bash
cp model-interface.example.toml model-interface.toml   # providers, aliases, caller keys, record path
export ANTHROPIC_API_KEY=... MI_KEY=some-secret
uv run model-interface                                 # http://127.0.0.1:8080
```

Any client that speaks either format then points at it:

```bash
MI_URL=http://127.0.0.1:8080 MI_KEY=some-secret uv run examples/agent.py "The tests are failing. Find out why and fix it."
```

The example streams the model's words as they arrive and runs its one tool itself. To stop it from outside, in another shell:

```bash
curl -X POST -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/interrupt/demo     # the session
curl -X DELETE -H "x-api-key: some-secret" http://127.0.0.1:8080/v1/interrupt/demo   # let it run again
curl -H "x-api-key: some-secret" "http://127.0.0.1:8080/v1/runs?session=demo"
```

## The code

| Module | What it is |
|---|---|
| `formats.py` | `Format`: one wire format, both directions, requests and streams. `AnthropicFormat` is the canonical shape. `OpenAIFormat` translates, including chunk-to-event and event-to-chunk. `assemble` folds events into one message. |
| `providers.py` | `Provider`: a URL, a key and a format; `stream` yields canonical events and closing it closes the connection. `Model`: an alias. |
| `gateway.py` | `Run`, `Stops`, `Record`, and `Gateway.stream`, the loop that watches each event. |
| `server.py` | The endpoints. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys or network, against a provider that plays a script of events, on a real port so streams are live and can be interrupted mid-way:

```bash
uv pip install -e ".[dev]" && pytest
```

## Not yet

Guardrails at the event points. Images and documents in messages. Thinking blocks across formats. Checkpoints. Tool execution, which belongs to a sealed execution service beside this one.

## License

MIT.

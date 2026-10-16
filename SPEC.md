# model-interface: specification

## What it is

A gateway for model calls with real-time observability.

An agent points its SDK at the gateway instead of at a provider. The gateway forwards each call to the provider in the provider's own wire format, using the provider's own SDK, with the provider's key held by the gateway. While the response streams back, the gateway reads a copy of every line into a record and forwards the line unchanged. The record is live: anyone watching sees the response take shape block by block, as it happens.

That is the whole product in this version. Nothing is translated, nothing is executed, nothing is stopped.

## What it is not

- Not a translator. Messages in, Messages out. Chat Completions in, Chat Completions out. A model alias speaks one format, and it only answers on that format's endpoint.
- Not a harness. The agent keeps its loop, its context, and its tools. The gateway never builds a request and never runs a tool.
- Not a store of conversations. The caller resends its transcript on every call, as the wire formats require. The record keeps a copy of what passed through, stamped; it is observation, not state the agent depends on.
- Not a killswitch, and not a policy engine. Those come next, on top of the record. This version contains neither.

## The two endpoints

| Endpoint | Format | Forwarded with | Serves aliases whose provider is |
|---|---|---|---|
| `POST /v1/messages` | Anthropic Messages | the `anthropic` SDK | `format = "anthropic"` |
| `POST /v1/chat/completions` | OpenAI Chat Completions | the `openai` SDK | `format = "openai"` |

Rules that apply to both:

- **Streaming only.** The body must carry `"stream": true`; otherwise `400`. A streamed call is the only kind that can be observed while it runs.
- **Pass-through.** The body is handed to the SDK as the SDK's own parameters; any field the installed SDK does not know is passed as `extra_body`. The `anthropic-beta` header is forwarded. The only change made to a body is the alias swapped for the provider's model name.
- **Verbatim response.** The provider's stream lines are forwarded as they arrive. The caller's SDK sees exactly what the provider sent.
- **Errors pass through.** A provider's refusal comes back with the provider's status and body. A failure before any line was forwarded is a `502` with the cause.
- **Caller keys.** A caller presents its own key, `x-api-key` or `Authorization: Bearer`. Keys are named in config; the name is who the record says made the call. No caller ever holds a provider key.
- **Session.** The `X-Session` header names the session; absent, `default`. Letters, digits, dots, dashes, underscores. The response carries `X-Stream`, the id of this stream in the record.

## The record

```
gateway
  caller                 a key, by name; every session it has run
    session              a conversation, named by the caller
      stream             one request and the response that streamed back for it
        block            one piece of the response
```

Every level has an id, a start and an end.

**Session.** `session`, `started` at its first stream, `ended` when its last stream ended, `key` of the caller, `streams[]`.

**Stream.** `id`, `session`, `key`, `model` (the alias), `format`, `started` when the request arrived, `ended` when the stream's last line arrived or the connection was lost, `request` exactly as received, `message` (the response's id, model and role, from its first line), `blocks[]`, `stop_reason`, `usage`, `error`. For Chat Completions also `finish_reason`, the format's own word.

**Block.** `index`, `type`, `started` when it opened, `ended` when it closed, and its content under the format's own field names:

| Format | Block types | Content fields |
|---|---|---|
| Messages | `thinking`, `text`, `tool_use` | `thinking` + `signature`; `text`; `id`, `name`, `input` (whole at close) |
| Chat Completions | `reasoning`, `content`, `tool_call` | `reasoning`; `content`; `id`, `name`, `arguments`, `call_index` |

In Messages, blocks are explicit on the wire: `content_block_start`, deltas, `content_block_stop`. In Chat Completions they are inferred: a block begins with the first delta of its kind and ends when a different kind begins or `finish_reason` arrives.

The request is kept verbatim on the stream. The response is not kept again as a body; the blocks are the response.

## Real time

The record is built from the stream's lines as they are forwarded, not afterwards. A block appears the moment it opens, grows with each delta, and gets its end time when it closes. Reading the record at any instant returns the stream as it stands at that instant.

Two feeds publish the record's own events as they happen:

- `GET /v1/sessions/{session}/live`, one session
- `GET /v1/live`, everything

Both are server-sent events. Each event names its `type`, `at`, the `session`, the `key` of the caller and the `stream`, by id, and carries its payload under the keys below:

| Event | Carries |
|---|---|
| `stream.started` | `snapshot`: the stream without blocks, request included |
| `stream.message` | `message`: the response's id, model and role |
| `block.started` | `block`: the block, with `started` and no `ended` |
| `block.delta` | `index`, `field`, `delta` |
| `block.ended` | `block`: the block, whole, with `ended` |
| `stream.ended` | `snapshot`: the stream without blocks or request: stop reason, usage, end time |

A keepalive comment is sent every fifteen seconds of silence.

## Reading the record

| Endpoint | Returns |
|---|---|
| `GET /v1/callers` | every caller: name, sessions, streams, last activity |
| `GET /v1/sessions` | every session: id, key, times, stream count |
| `GET /v1/sessions/{session}` | the session with its streams and their blocks; `?blocks=false` omits blocks |
| `GET /v1/streams/{stream}` | one stream with its blocks |
| `GET /v1/models` | the aliases and their formats |

Every finished stream is also appended, as one JSON line, to the record file named in config.

## The UI

`GET /` serves a single page: callers on the left, their sessions beneath them, and the selected session's streams in the middle, blocks and all, updating from the live feed as calls run. It needs a caller key, which it passes as `?key=` on the feed since browsers cannot set headers on a server-sent events connection; every `GET` therefore accepts the key as a query parameter as well as a header.

## Configuration

A TOML file. Values written `env:NAME` are read from the environment.

```toml
record = "~/.model-interface/streams.jsonl"

[keys]
team-a = "env:MI_KEY"

[providers.anthropic]
format = "anthropic"
url = "https://api.anthropic.com"
key = "env:ANTHROPIC_API_KEY"

[providers.ollama]
format = "openai"
url = "http://localhost:11434/v1"

[models]
coder = { provider = "anthropic", model = "claude-sonnet-5-5" }
local = { provider = "ollama", model = "qwen2.5:3b" }
```

## What comes next, and is not here

The killswitch: a standing state at gateway, caller or session level that cuts every running stream beneath it and refuses new calls until an operator clears it. Rules: expressions over the record, evaluated at the moments the feeds already publish, with two actions, stop and alert. Both are built on this record and this feed and change nothing about the endpoints.

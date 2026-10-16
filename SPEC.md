# model-interface: specification

## What it is

A gateway for model calls, with real-time observability and a killswitch.

An agent points its SDK at the gateway instead of at a provider, presents its agent key, and names the model as the provider names it. The gateway forwards the call to that provider in the provider's own wire format, using the provider's own SDK, with the provider's key held by the gateway. While the response streams back, the gateway reads a copy of every line into a record and forwards the line unchanged. The record is live: anyone watching sees the response take shape block by block, as it happens. An operator can stop any of it, at the gateway, at an agent, or at a session, and what is stopped is cut mid-stream and refused until turned back on.

Nothing is translated, nothing is executed, and no decision is made about a call except whether it may run.

## What it is not

- Not a translator. Messages in, Messages out. Chat Completions in, Chat Completions out. A provider speaks one format; a model is called on that format's endpoint.
- Not a harness. The agent keeps its loop, its context, and its tools. The gateway never builds a request and never runs a tool.
- Not a store of conversations. The caller resends its transcript on every call, as the wire formats require. The record keeps a copy of what passed through, stamped; it is observation, not state the agent depends on.
- Not a policy engine. Rules over the record, with stop and alert, come next, on top of this. This version has the switches an operator pulls by hand.

## The objects

```
gateway                        one process, one config file; holds the gateway killswitch
  provider                     an endpoint and a key; the endpoint's path says the format
    model                      the provider's own model name
  agent                        a name, its keys, the models it may use; holds its killswitch
    session                    named by the agent in X-Session; holds its killswitch
      stream                   one request and the response that streamed back for it
        block                  one piece of the response
```

**Provider.** `endpoint`, the full URL the provider serves, and `key`. The path of the endpoint says which format it speaks and so which SDK reaches it:

| Endpoint ends in | Format | SDK | The SDK's base_url is the endpoint without |
|---|---|---|---|
| `/v1/messages` | Anthropic Messages | `anthropic` | `/v1/messages` |
| `/chat/completions` | OpenAI Chat Completions | `openai` | `/chat/completions` |

A provider that serves both formats is two providers. `models` lists the names the provider uses.

**Agent.** `keys`, one or more, and `models`, as `provider/model`. The grants decide how a model name in a call resolves: on the format the call arrived on, among this agent's grants, by name. One model name resolves to one provider per format for an agent; a configuration that grants the same name on two providers of the same format is refused when loaded.

**Operator.** One `admin` key. It reads every agent's record, holds every switch, and opens the page. It cannot call a model.

## The two endpoints

| Endpoint | Format | Forwarded with | Serves models whose provider's endpoint ends in |
|---|---|---|---|
| `POST /v1/messages` | Anthropic Messages | the `anthropic` SDK | `/v1/messages` |
| `POST /v1/chat/completions` | OpenAI Chat Completions | the `openai` SDK | `/chat/completions` |

A call is handled in this order:

1. **Who.** The key, `x-api-key` or `Authorization: Bearer`, names an agent; otherwise `401`.
2. **Switched off.** The gateway, this agent, or this session has its killswitch on: `423`, with `error.level` naming which.
3. **Streaming only.** `"stream": true`, otherwise `400`. A streamed call is the only kind that can be observed and cut while it runs.
4. **Which model.** The body's `model`, resolved through the agent's grants on this format. Not granted: `404`. Granted on the other format: `400` naming the right endpoint.
5. **Pass-through.** The body goes to the SDK as the SDK's own parameters; any field the installed SDK does not know is passed as `extra_body`. The `anthropic-beta` header is forwarded. The provider's key is attached. The only change to the body is nothing; the model name is already the provider's.
6. **Verbatim response.** The provider's stream lines are forwarded as they arrive. The response carries `X-Agent`, `X-Session` and `X-Stream`.
7. **Errors pass through.** A provider's refusal comes back with the provider's status and body. A failure before any line was forwarded is a `502` with the cause; after, one error line in the format's own shape.

`X-Session` names the session; absent, `default`. Letters, digits, dots, dashes, underscores. Sessions are per agent: two agents may both have `default`.

`GET /v1/models` lists, in the shape both SDKs expect, the models an agent key may use, with each one's provider, format and endpoint; everything, for the operator.

## The record

Every level has an id, a start and an end.

**Agent.** `agent`, `sessions`, `streams`, `running`, `interrupted`, `last`, `killswitch`.

**Session.** `agent`, `session`, `started` at its first stream, `ended` when its last stream ended, `streams`, `interrupted`, `killswitch`.

**Stream.** One connection to the provider: `started` when it opened, `ended` when it closed. Two bodies, both in the provider's format: `request` exactly as received, and `response` as a non-streaming call would have returned it, assembled from the stream: in Messages the message with its `content`, `stop_reason`, `stop_sequence` and `usage`; in Chat Completions the completion with one choice, its `message` (`content`, `reasoning_content`, `tool_calls`), `finish_reason` and `usage`. Nothing in `response` is renamed. Then `blocks[]`, the response's content as it streamed, piece by piece, with times. Then the gateway's own fields: `id`, `agent`, `session`, `provider`, `model`, `format`, `interrupted` (`null`, or `by` the level that cut it, `at` when, and the `block` it landed in, by its `at`), `error`. A cut stream's response ends with the reason `interrupted`.

**Piece.** A piece is one object the provider sent as part of the response, grown by the deltas that followed it. The gateway adds three fields and nothing else: `at`, where the piece lives in the response; `started`, when it opened; `ended`, when it closed. Everything inside a piece is the provider's own: its `type` if it sent one, its fields under its own names, including names that did not exist when the gateway was written.

The gateway reads a stream with one rule and no vocabulary. It knows the envelope of each format, which part of a line is the head, which opens a piece, which grows one, and folds each delta into its piece by shape alone: strings grow, objects merge, lists of objects that carry an `index` merge entry by entry, anything else is set, `type` is set once, null never erases. No code in the tap names `text`, `thinking`, `tool_use`, `content`, `reasoning_content` or `tool_calls`. A block type or field that a provider adds next year is recorded whole the day it appears.

| Format | Where pieces live | What opens one | One convention |
|---|---|---|---|
| Messages | `response.content[i]`, at `content[i]` | the wire's own block start and stop | a piece's JSON text, sent as `partial_json`, is its `input`: parsed as far as it has got while open, whole when closed |
| Chat Completions | `response.blocks[]`, each at `choices[i].message.<key>` or `<key>[j]` | a delta to a different place than the open piece; a list of indexed objects is one piece per index | none |

In Chat Completions the native message has no pieces, so the pieces sit beside it inside `response` as `blocks`, the same deltas with their times. The message is those deltas merged.

Proof: the five real wire captures in `tests/fixtures`, from Anthropic, OpenAI, Anthropic's compatible endpoint and Ollama, assemble through this merge into exactly the object each provider's own SDK assembles, and a synthetic stream with a block type, a delta type and fields no SDK knows is recorded whole.

## Real time

The record is built from the stream's lines as they are forwarded, not afterwards. A block appears the moment it opens, grows with each delta, and gets its end time when it closes. Reading the record at any instant returns the stream as it stands at that instant.

Feeds publish the record's own events as server-sent events, at three scopes:

| Feed | Scope |
|---|---|
| `GET /v1/live` | everything the key may see: every agent for the operator, itself for an agent |
| `GET /v1/agents/{agent}/live` | one agent |
| `GET /v1/agents/{agent}/sessions/{session}/live` | one session |

Every event names its `type`, `at`, `agent`, `session` and, for stream events, `stream`, by id, and carries its payload under its own key:

| Event | Carries |
|---|---|
| `stream.started` | `snapshot`: the stream without blocks, request included |
| `stream.response` | `response`: what the provider said about the response before its blocks: id, model, role |
| `block.started` | `at`, `block`: the piece as it opened, with `started` and no `ended` |
| `block.delta` | `at`, `delta`: the delta object exactly as the provider sent it |
| `block.ended` | `at`, `block`: the piece, whole, with `ended` |
| `stream.ended` | `snapshot`: the stream without blocks or request: the whole `response`, the interruption, the end time |
| `killswitch` | `level`, `on`, `agent`, `session`, and the whole `state` |

The first event on any feed is `watching`, with the scope and the killswitch state. A keepalive comment is sent every fifteen seconds of silence.

## Reading the record

| Endpoint | Returns |
|---|---|
| `GET /v1/me` | whether the key is the operator's, or which agent's |
| `GET /v1/agents` | every agent the key may see, summarised |
| `GET /v1/agents/{agent}` | the agent, its grants, its switch, its sessions summarised |
| `GET /v1/agents/{agent}/sessions/{session}` | the session with its streams and their blocks; `?blocks=false` omits blocks |
| `GET /v1/streams/{stream}` | one stream with its blocks |

An agent's key sees that agent only; the operator's key sees all. Every finished stream is also appended, as one JSON line, to the record file named in config.

## The killswitch

A state at three levels. On means two things at once, for everything in scope: running streams are cut, new calls are refused with `423`. Off means calls run. There is no pause.

| Switch | Set with | Cuts | Refuses |
|---|---|---|---|
| gateway | `PUT /v1/killswitch {"on": true}` | every running stream | every call |
| agent | `PUT /v1/agents/{agent}/killswitch` | every running stream of that agent | that agent's calls |
| session | `PUT /v1/agents/{agent}/sessions/{session}/killswitch` | the running streams in that session | calls naming that session |

Only the operator's key sets a switch. `GET /v1/killswitch` returns the state: `gateway`, `agents[]`, `sessions[]`.

A cut lands within a tenth of a second of the switch, whatever the provider is doing, including while it is silent. The gateway closes its connection to the provider, so the provider stops generating. It ends the caller's stream with a whole message in the format's own shape, so the caller's SDK returns normally: in Messages, `content_block_stop` for the open block, `message_delta` with `stop_reason: "interrupted"`, `message_stop`; in Chat Completions, a final chunk marked `"interrupted": true` with a finish reason, then `[DONE]`. The record marks the stream `interrupted`, by which level, at what time, and at which piece, and that piece gets its end time.

Every switch change is published on the feeds as a `killswitch` event.

## The UI

`GET /` serves a single page. It takes the operator's key or an agent's key. Agents on the left with their sessions beneath; the selected session's streams in the middle, blocks and all, updating from the live feed as calls run. The killswitches are on the page: one in the header for the gateway, one on each agent, one on the selected session. They are live for the operator and shown, disabled, to an agent. A cut stream is marked, and the block it was cut in says so.

The page passes the key as `?key=` on the feed, since browsers cannot set headers on a server-sent events connection; every `GET` therefore accepts the key as a query parameter as well as a header.

## Configuration

A TOML file. Values written `env:NAME` are read from the environment. See `model-interface.example.toml`.

```toml
admin = "env:MI_ADMIN_KEY"
record = "~/.model-interface/streams.jsonl"

[providers.anthropic]
endpoint = "https://api.anthropic.com/v1/messages"
key = "env:ANTHROPIC_API_KEY"
models = ["claude-sonnet-5-5"]

[providers.ollama]
endpoint = "http://localhost:11434/v1/chat/completions"
models = ["qwen2.5:3b"]

[agents.coder]
keys = ["env:CODER_KEY"]
models = ["anthropic/claude-sonnet-5-5", "ollama/qwen2.5:3b"]
```

## What comes next, and is not here

Rules: expressions over the record's schema, at gateway scope for every agent or at agent scope for one, evaluated at the moments the feeds already publish, with two actions, stop and alert, and registered functions a rule may call. Configuration in the UI with an operator login and a database behind it, instead of this file. Tools, registered on the gateway and executed in sandboxes, recorded and cut the same way. Each is built on this record, this feed and these switches, and changes nothing about the endpoints.

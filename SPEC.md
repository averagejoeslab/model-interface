# model-interface: specification

## What it is

A gateway for model calls that taps the wire.

An agent points its SDK at the gateway, presents its virtual key, and names its model as `provider/model`. The gateway opens one connection to that provider through the SDK the agent chose by its door, passes the request through verbatim, and reads every line of the response as it streams back, folding each into a record by shape, never by name. Rules run in that loop at three moments and set the killswitch. Everything the gateway sees is published as it happens.

The gateway holds three kinds of thing, and makes no decisions itself:

- **Providers**: a name, a root URL, a key.
- **Agents**: a name, its virtual keys.
- **Rules**: CEL expressions over what the tap sees, at a moment, on the gateway or on one agent.

## What it is not

- Not a translator. The door the call comes through is the format it leaves by.
- Not a harness. The agent keeps its loop, its context and its tools; the gateway never builds a request and never runs a tool.
- Not a table of permissions. No grants, no aliases, no model lists. What an agent may do is a rule.
- Not a store of conversations. The caller resends its transcript on every call. The record is observation.

## The two doors

| Door | SDK behind it | Reaches |
|---|---|---|
| `POST /v1/messages` | `anthropic` | the provider's root; the SDK appends `/v1/messages` |
| `POST /v1/chat/completions` | `openai` | the provider's root plus `/v1`; the SDK appends `/chat/completions` |

A provider has no format. One provider serves both doors if it speaks both formats, and says so itself, with its own error passed through, if it does not. The caller picks the SDK by picking the door.

A call is handled in this order:

1. **Who.** The key, `x-api-key` or `Authorization: Bearer`, names an agent; otherwise `401`.
2. **Where.** `model` is `provider/model`; the provider is looked up by name, the rest is the model as the provider names it. A bare name works when one provider is configured; otherwise `404` naming the choices.
3. **Streaming only.** `"stream": true`, otherwise `400`.
4. **The request moment.** The request rules run over the whole request. A trip sets a switch.
5. **The switches.** The gateway, this agent, or this session has its switch on: `423` with the level and the reason, and the call is recorded as a refused stream that never opened a connection.
6. **Pass-through.** The body goes to the SDK as its own parameters, unknown fields as `extra_body`, the provider's key attached, `anthropic-beta` forwarded, the model name as the provider names it. The response's lines are forwarded as they arrive, and the response carries `X-Agent`, `X-Session`, `X-Stream`.
7. **The stream moment.** On every line: the line is tapped, the stream rules run over the response as far as it has got, and the line is forwarded. A switch set by a rule is seen on the next line or the next tenth of a second, and the stream is cut.
8. **The response moment.** After the last line: the response rules run over the whole response. A trip affects the next call; an alert is recorded.

`X-Session` names the session; absent, `default`. Sessions are per agent.

## The tap

The tap knows the envelope of each door and nothing about what is inside it:

| Door | Head | Opens a piece | Grows a piece |
|---|---|---|---|
| Messages | a line with `message`; a `delta` without index; `usage` | `content_block` with `index`, at `content[i]` | `delta` with `index` |
| Chat Completions | every chunk's top-level fields, minus per-chunk padding | a delta to a different place than the open piece, at `choices[i].message.<key>`, one per index for an indexed list | the same delta |

One merge folds every delta into its piece by shape: strings grow, objects merge, lists of objects with an `index` merge entry by entry, anything else is set, `type` is set once, null never erases. No name of any block or field appears in the tap. The one convention kept is the Messages door's: a piece's `partial_json` is its `input`, parsed as far as it has got while open. Proof: the real wire captures in `tests/fixtures` assemble through the merge into exactly what each provider's own SDK assembles, and a synthetic stream with a block type, delta type and fields no SDK knows is recorded whole.

## The record

```
agent
└─ session
   └─ stream                  one connection, opened and closed; or refused, never opened
      ├─ request              the body, verbatim, with the model as the provider names it
      └─ response             the body the provider would have returned whole, with its pieces timed
```

**Stream.** `id`, `agent`, `session`, `door`, `provider`, `model`, `started`, `ended`, `request`, `response`, `blocks` (the pieces, each with `at`, `started`, `ended` and the provider's own fields), `refused` (`by`, `reason`), `interrupted` (`by`, `reason`, `at`, `block`), `alerts` (`rule`, `when`, `at`), `error`. In Messages the pieces are `response.content`; in Chat Completions they sit beside the merged message as `response.blocks`.

Every finished stream is appended as one JSON line to the record file.

## Real time

Feeds publish the record's own events as server-sent events: `GET /v1/live` for everything the key may see, `GET /v1/agents/{agent}/live`, `GET /v1/agents/{agent}/sessions/{session}/live`. Every event names `type`, `time`, `agent`, `session` and, for stream events, `stream`:

| Event | Carries |
|---|---|
| `stream.started` | `snapshot`: the stream with its request |
| `stream.response` | `response`: what the head said that it had not said before |
| `block.started` | `at`, `block` |
| `block.delta` | `at`, `delta`, the object as the provider sent it |
| `block.ended` | `at`, `block` |
| `rule.fired` | `rule`, `when`, `action`, `trip` |
| `killswitch` | `level`, `on`, `agent`, `session`, `reason`, `state` |
| `stream.ended` | `snapshot`: the stream with its whole response, its refusal, interruption and alerts |

Reading: `GET /v1/agents`, `/v1/agents/{agent}`, `/v1/agents/{agent}/sessions/{session}` (`?blocks=false`), `/v1/streams/{stream}`, `/v1/providers`, `/v1/rules`, `/v1/me`. An agent's key sees that agent; the operator's key sees all.

## Rules

A rule has a name, an expression, a moment, an action and a place.

| | |
|---|---|
| `when` | `request`, `stream`, `response` |
| `action` | `trip`, which sets the killswitch; `alert`, which only records |
| `on` | `gateway`, which sees every agent; or an agent's name |
| `trip` | a gateway rule trips the `agent` (default) or the `session`; an agent rule trips the `session`; nothing trips the gateway |

A rule sees `agent`, `session`, `provider`, `model`, `door`, `request`, `response` as composed so far, `piece`, the one still open, and `text`, every string anywhere in what the moment is about. A rule that cannot be evaluated on a view does not fire.

```
text.matches("AKIA[0-9A-Z]{16}")                      on the gateway, when = request
!(model in ["gpt-5-mini"])                             on an agent, when = request
text.contains("rm -rf")                                on an agent, when = stream
response.usage.output_tokens > 4000                    anywhere, when = response, action = alert
```

The rules run in the loop, on the hot path: the line a stream rule fires on is the last line the caller receives. The rules of a moment are evaluated together on a thread pool and reported in order, so a moment costs the slowest rule, not the sum. CEL in pure Python holds the interpreter lock while it evaluates, so for rules that only compute, the gain is small; the gain is real once rules wait on anything outside the process.

## The killswitch

A state at three levels with the reason it was set: a rule's name, or `operator`. On: running streams in scope are cut within a tenth of a second, closed upstream so the provider stops, closed downstream as a whole message so the caller's SDK returns with `interrupted`; new calls are refused with `423`. Off: calls run. Only the operator's key sets a switch by hand, with `PUT /v1/killswitch`, `/v1/agents/{agent}/killswitch`, `/v1/agents/{agent}/sessions/{session}/killswitch`. The gateway level is set only by hand.

## The page

`GET /` serves a page on the feed: agents and sessions with their switches; for a session, the wire, a strip chart of its streams on its clock with pieces as segments, deltas as ticks, cuts and refusals marked; the transcript, each stream as it reads; and the structure, the session as the record holds it: every stream with its door, provider, model and how it stopped; its request, with the system prompt in view; its response, marked where a rule closed the connection and on which block; and a refused request marked with the rule that refused it and that no connection was opened. Nodes fold and unfold, the open piece is followed, and the whole comes back from the record on reload.

## Configuration

```toml
admin = "env:MI_ADMIN_KEY"

[providers.anthropic]
url = "https://api.anthropic.com"
key = "env:ANTHROPIC_API_KEY"

[agents.coder]
keys = ["env:CODER_KEY"]

[[rules]]
name = "no destructive commands"
on = "coder"
when = "stream"
expr = 'text.contains("rm -rf")'
```

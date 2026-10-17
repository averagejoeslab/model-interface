# Walkthrough

This document walks through running the gateway from nothing to a working setup: deploying it, configuring a provider, creating an agent, writing rules on the gateway and on an agent, testing those rules, and using the killswitch at each of its three levels. It follows the code as it is. [SPEC.md](SPEC.md) is the definition; this is the tour.

Everything the operator does is in one TOML file. Everything an agent does is a normal SDK call. Everything the gateway sees is on the page.

## 1. What runs

The gateway is one process. It serves two doors, a record, a live feed, the switches and a page:

```
POST /v1/messages              the Messages door: the Anthropic SDK reaches the provider
POST /v1/chat/completions      the Chat Completions door: the OpenAI SDK reaches the provider
GET  /v1/agents, /v1/agents/{agent}, /v1/agents/{agent}/sessions/{session}, /v1/streams/{stream}
GET  /v1/live, /v1/agents/{agent}/live, /v1/agents/{agent}/sessions/{session}/live
GET  /v1/providers, /v1/rules, /v1/me, /v1/killswitch
PUT  /v1/killswitch, /v1/agents/{agent}/killswitch, /v1/agents/{agent}/sessions/{session}/killswitch
GET  /                         the page
```

Three kinds of key reach it:

| Key | Who | What it opens |
|---|---|---|
| a provider's key | the gateway, never a caller | the provider, from inside the gateway |
| an agent's virtual key | the agent's code | the two doors, and that agent's own record and feed |
| the operator's key (`admin`) | a person | every record, every feed, the switches, the page |

A key is presented as `x-api-key`, as `Authorization: Bearer`, or as `?key=` on a feed URL. Both SDKs send one of the first two on their own, so an agent's code sets only `api_key`.

## 2. Deployment

### As a container

The repository has a `Dockerfile`. The image holds the package and nothing else; the config is mounted in and the keys it names come from the environment.

```bash
docker build -t model-interface .
docker run -d --name model-interface -p 8080:8080 \
  -v "$PWD/model-interface.toml:/etc/model-interface.toml:ro" \
  -v model-interface-record:/var/lib/model-interface \
  -e MI_ADMIN_KEY=... -e CODER_KEY=... -e ANTHROPIC_API_KEY=... -e OPENAI_API_KEY=... \
  model-interface
```

Set `record` in the config to a path under the mounted volume, such as `/var/lib/model-interface/streams.jsonl`, so finished streams outlive the container. The page is then at `http://host:8080/`.

The entrypoint is the `model-interface` command with `--host 0.0.0.0 --port 8080`; the default argument is `--config /etc/model-interface.toml`. Pass a different `--config` after the image name to override it.

### On a host

```bash
uv pip install -e .                                    # or: pip install .
cp model-interface.example.toml model-interface.toml   # edit it
export MI_ADMIN_KEY=... CODER_KEY=... ANTHROPIC_API_KEY=...
model-interface --config model-interface.toml --host 127.0.0.1 --port 8080
```

`--host` defaults to `127.0.0.1` and `--port` to `8080`. Put TLS in front of it the way you would any HTTP service; the gateway speaks plain HTTP.

### What to deploy with it

- **The record file.** Every finished stream is appended as one JSON line to the path in `record`. Unset, it is `~/.model-interface/streams.jsonl`; set to an empty string, no file is kept. The last 2000 streams also stay in memory for the page and the read endpoints; older ones are only in the file.
- **No database, no queue.** The live feed is in-process. If you run more than one replica, each has its own record and its own switches. One replica is the shape this is built for.
- **The keys.** Any value in the config written as `env:NAME` is read from that environment variable when the process starts. Nothing secret needs to be in the file.

## 3. The config file

One file holds everything. This is the whole of the example, and the sections that follow take it apart:

```toml
admin = "env:MI_ADMIN_KEY"
record = "~/.model-interface/streams.jsonl"

[providers.anthropic]
url = "https://api.anthropic.com"
key = "env:ANTHROPIC_API_KEY"

[providers.openai]
url = "https://api.openai.com"
key = "env:OPENAI_API_KEY"

[providers.ollama]
url = "http://localhost:11434"

[agents.coder]
keys = ["env:CODER_KEY"]

[agents.support]
keys = ["env:SUPPORT_KEY"]

[[rules]]
name = "no credentials in requests"
when = "request"
expr = 'text.matches("AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|BEGIN (RSA|OPENSSH) PRIVATE KEY")'

[[rules]]
name = "support stays on small models"
on = "support"
when = "request"
expr = '!(model in ["gpt-5-mini", "claude-haiku-5-5"])'

[[rules]]
name = "no destructive commands"
on = "coder"
when = "stream"
expr = 'text.matches("rm -rf|DROP TABLE|mkfs|git push --force")'

[[rules]]
name = "long answers are noted"
when = "response"
action = "alert"
expr = 'has(response.usage) && (response.usage.output_tokens > 4000 || response.usage.completion_tokens > 4000)'
```

The file is read once, at start. A mistake in it stops the process with a message naming the rule or value: a rule on an agent that does not exist, a rule with a moment or action that is not one of the known ones, a rule that may not trip where it says, or a CEL expression that does not compile.

## 4. Setting up a provider

A provider is a name, a root URL and a key. That is the whole of it.

```toml
[providers.anthropic]
url = "https://api.anthropic.com"
key = "env:ANTHROPIC_API_KEY"
```

**The name** is how callers reach it. An agent names its model as `provider/model`, so with the block above a call for `anthropic/claude-sonnet-5-5` goes to this provider as `claude-sonnet-5-5`. The gateway rewrites the model field to the bare name before forwarding, so the provider never sees the prefix. When exactly one provider is configured, a bare model name is routed to it. With more than one, a bare name is refused with 404 and a message listing the choices.

**The URL** is the root. The door the caller uses decides what is appended. The Messages door hands the root to the Anthropic SDK as its base URL, which adds `/v1/messages`. The Chat Completions door hands the root plus `/v1` to the OpenAI SDK, which adds `/chat/completions`. If the URL already ends in `/v1` it is left as it is. So one root serves both doors, and a provider that speaks both formats is reachable by both SDKs with one block.

**The key** is sent by whichever SDK makes the call, in that SDK's own header. A provider with no key, such as a local Ollama, leaves `key` out; the SDKs are given a placeholder so they do not refuse to start.

**There is no format and no model list.** The gateway does not know what a provider speaks or serves. A call through a door the provider does not understand gets the provider's own error, with its own status and body, passed back to the caller as it came. A call for a model the provider does not serve gets the same treatment. The only thing the gateway checks before forwarding is that the provider name exists.

Three providers that work as written in the example: Anthropic through the Messages door, OpenAI through the Chat Completions door, and Ollama through the Chat Completions door. Anthropic also answers on the Chat Completions door through its compatibility endpoint, which is one example of a root that serves both.

## 5. Setting up an agent

An agent is a name and its virtual keys.

```toml
[agents.coder]
keys = ["env:CODER_KEY"]
```

**The name** is the top of the record. Every call made with one of these keys is recorded under this agent, in whichever session the call named. The name is also what an agent rule binds to, and what the agent-level switch cuts.

**The keys** are strings you make up. An agent may have several, so one can be rotated without stopping the other. A key reaches the two doors, this agent's own record (`GET /v1/agents/coder`, its sessions, its streams) and this agent's own feed. It cannot read another agent's record, and it cannot touch any switch.

**There are no grants.** An agent is not given providers or models. Anything an agent may or may not do is said by a rule, which is section 6.

**Sessions** are named by the caller, not configured. Each call carries an `X-Session` header; calls with the same name under the same agent are one session, in order. The header defaults to `default`. The name is letters, digits, dots, dashes and underscores, up to 64 characters. One agent may run many sessions at once, and a session may hold any number of streams. A stream is one call: one connection opened, one response read to its end or cut.

### What the agent's code looks like

The agent points its SDK at the gateway, uses its virtual key, and names its model as `provider/model`. Nothing else in its code changes.

With the Anthropic SDK, on the Messages door:

```python
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8080", api_key="the coder key")
with client.messages.stream(model="anthropic/claude-sonnet-5-5", max_tokens=300,
                            messages=[{"role": "user", "content": "hi"}],
                            extra_headers={"X-Session": "task-1"}) as s:
    for text in s.text_stream:
        print(text, end="")
```

With the OpenAI SDK, on the Chat Completions door:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="the coder key",
                default_headers={"X-Session": "task-1"})
for chunk in client.chat.completions.create(model="openai/gpt-5", stream=True,
                                            messages=[{"role": "user", "content": "hi"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

Two things to know:

- **Streaming only.** A call without `stream: true` is refused with 400. The gateway reads the wire line by line; that is what the tap, the rules and the switches work on.
- **Verbatim.** The request body goes to the provider as it came, apart from the model name. Fields the SDK does not know are passed through as extra body. The `anthropic-beta` header is forwarded on the Messages door. The response comes back line by line as the provider wrote it.

`examples/agent.py` is a complete small agent on this pattern: it streams, runs its one tool, keeps its own context, and stops when a response comes back `interrupted`.

## 6. Rules

A rule is CEL, the Common Expression Language, over what the tap sees. It has five parts:

| Part | Values | Default |
|---|---|---|
| `name` | any string; it becomes the reason on a switch and on the record | required |
| `expr` | a CEL expression that is true when the rule fires | required |
| `when` | `request`, `stream`, `response` | `stream` |
| `action` | `trip`, `alert` | `trip` |
| `on` | `gateway`, or an agent's name | `gateway` |
| `trip` | `agent`, `session` | `agent` for a gateway rule, `session` for an agent rule |

### The three moments

Rules run in the gateway's own loop, on the hot path, at three moments of every stream:

1. **`request`**: after the call is routed and recorded, before any connection to the provider is opened. A trip here means no connection is ever opened: the record shows the stream as `refused`, with the rule's name, and the caller gets 423.
2. **`stream`**: on every line of the response while the connection is open, after the line is folded into the record and before it is forwarded. A trip here sets the switch; the loop sees the switch on its next pass, which is the next line or a tenth of a second, whichever comes first. The connection to the provider is shut, and the caller's stream is closed as a whole message with `stop_reason: "interrupted"` on the Messages door or `"interrupted": true` on the Chat Completions door. The line the rule fired on is the last line the caller receives.
3. **`response`**: after the last line, over the whole response. A trip here does not cut anything, since the stream is over; it sets the switch for whatever comes next in scope. An alert here is the usual choice.

The rules of one moment run together on a thread pool and report in order, so a moment costs its slowest rule rather than the sum of them.

### What a rule sees

Every rule is evaluated over the same set of names:

| Name | What it is |
|---|---|
| `agent` | the agent's name |
| `session` | the session's name |
| `provider` | the provider's name |
| `model` | the model as the provider names it, without the prefix |
| `door` | `messages` or `chat` |
| `request` | the request body as received |
| `response` | the response as composed so far: the whole of it at the `response` moment, as far as it has got at the `stream` moment, `null` at the `request` moment |
| `piece` | at the `stream` moment, the block that is still open: a text block, a tool call, a thinking block, whatever the provider is writing; `null` otherwise |
| `text` | every string anywhere in what the moment is about, joined: the request at the request moment, the response at the other two |

`text` is the one to reach for first. It is every string in the request, or in the response so far: the system prompt, the messages, the tool definitions, the model's words, the tool call's arguments as they are written. `text.contains("rm -rf")` does not need to know whether the string sits in a text block, a tool call's `input` or a Chat Completions `arguments` field.

`response` and `piece` are the record's own shape, which is the provider's own shape. On the Messages door a response has `content`, `usage`, `stop_reason` and the rest as Anthropic names them; on the Chat Completions door it has `choices`, `usage`, `finish_reason` and the rest as OpenAI names them. A rule that reads a field that is not there does not fire: an exception during evaluation is treated as false. Guard with `has()` when a field may be absent, as the long-answer example does.

### Two places

**A gateway rule** (`on = "gateway"`, the default) sees every agent's streams. When it trips, it trips the agent the stream belongs to, or with `trip = "session"` only that session.

**An agent rule** (`on = "coder"`) sees only that agent's streams. When it trips, it trips the session. It may not trip the whole agent, and nothing may trip the gateway: the gateway switch is set by people only. The config loader refuses a rule that says otherwise.

At any moment, the rules that run for a stream are the gateway's rules for that moment followed by the agent's own.

### Two actions

**`trip`** sets the killswitch at the rule's level with the rule's name as the reason. It stays set until an operator turns it off. Everything in scope is cut and refused until then.

**`alert`** records: the stream gets an entry in its `alerts` with the rule's name, the moment and the time, and the feed carries a `rule.fired` event with `action: "alert"`. No switch moves.

### Creating gateway rules

A gateway rule is for things no agent should do. The example file has two:

```toml
[[rules]]
name = "no credentials in requests"
when = "request"
expr = 'text.matches("AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|BEGIN (RSA|OPENSSH) PRIVATE KEY")'
```

This runs before any connection opens, over every string in the request: the system prompt, every message, every tool definition. If anything looks like an AWS access key, an API key or a private key, the request is refused, no connection is opened, and the agent is tripped. Every further call from that agent is refused with 423 until an operator turns the agent back on. `trip` is left at its default, `agent`, since an agent that puts credentials in prompts should stop entirely.

```toml
[[rules]]
name = "long answers are noted"
when = "response"
action = "alert"
expr = 'has(response.usage) && (response.usage.output_tokens > 4000 || response.usage.completion_tokens > 4000)'
```

This runs after the stream ends, over the whole response. It reads the usage as each door names it: `output_tokens` on Messages, `completion_tokens` on Chat Completions. One of the two will be absent, and reading an absent field would stop evaluation, which is why the expression is written with `||` so the first true branch wins, and why it is guarded with `has(response.usage)`. It only alerts, so nothing is cut.

Other gateway rules that fit this shape:

```toml
[[rules]]
name = "only our providers"
when = "request"
trip = "session"
expr = '!(provider in ["anthropic", "openai"])'

[[rules]]
name = "no tool calls to unknown tools"
when = "stream"
trip = "session"
expr = 'has(piece.name) && !(piece.name in ["shell", "read_file"])'
```

The second shows `piece`: at the stream moment, the open block is the tool call as it is being written, and on the Messages door a tool call's `name` arrives on the line that opens the block, before its arguments. The rule fires on that line, so the arguments never reach the caller. `has(piece.name)` is false when no piece is open and when the open piece is not a tool call, so the rest is only read on a tool call.

### Creating agent rules

An agent rule is for what one agent may do. The example has two:

```toml
[[rules]]
name = "support stays on small models"
on = "support"
when = "request"
expr = '!(model in ["gpt-5-mini", "claude-haiku-5-5"])'
```

This is how an agent is held to its models. There is no grant on the agent; there is a rule that trips the session when the agent asks for anything else. The request is refused with 423 before any connection opens, the session is off, and the agent's other sessions run as before. The refusal body names the rule, so the agent's code sees exactly why.

```toml
[[rules]]
name = "no destructive commands"
on = "coder"
when = "stream"
expr = 'text.matches("rm -rf|DROP TABLE|mkfs|git push --force")'
```

This runs on every line of every one of coder's responses. The moment one of those strings appears in the response so far, which in practice is while the model is writing the arguments of a tool call, the session is tripped and the stream is cut. The caller's SDK gets a whole message ending in `interrupted` whose tool call holds the arguments as far as they got. The record holds the same, and marks the block the cut landed on. Nothing after that line was forwarded, so the agent's code never has a complete destructive command to run.

An agent rule is evaluated only for that agent's streams: the `support` agent streaming the same content is not cut by coder's rule.

### Writing CEL

The expressions above use most of what rules need:

- `text.contains("...")`, `text.matches("regex")`, `text.startsWith("...")`
- `x in ["a", "b"]`, `!`, `&&`, `||`, `==`, `!=`, `<`, `>`
- `has(response.usage)` to test a field exists before reading it
- `has(piece.name)` before reading a field of the open piece: it is false when no piece is open, since `piece` is then null and null cannot be compared or read
- `size(request.messages) > 20` for lengths
- `request.messages.exists(m, m.role == "system")` for lists

A rule's expression is compiled when the config loads. A syntax error stops the process at start, with the rule's name. A runtime error, such as reading a field that is not there, makes that one evaluation false and the stream goes on.

## 7. Testing rules

There are three ways to see a rule work, from the cheapest to the most real.

### Against the fake provider, in the test suite

The test suite runs without keys or network. `tests/conftest.py` starts a fake provider on a real port that speaks both formats, word by word, with a tool call whose arguments arrive in seven-character pieces, and puts the gateway in front of it with two agents and four rules. The four rule tests in `tests/test_server.py` are the pattern to copy for a rule of your own:

- `test_a_gateway_rule_on_the_request_trips_the_agent_before_any_connection_opens` sends a system prompt holding a fake AWS key, checks the 423 and its body, checks that the fake provider received no request, reads the feed and expects exactly `stream.started`, `rule.fired`, `killswitch`, `stream.ended`, reads the record and expects `refused` with the rule's name and `response: null`, then shows the agent's other session is refused too, another agent runs, and turning the agent back on lets it run.
- `test_an_agent_rule_on_the_request_is_how_an_agent_is_held_to_its_models` asks for the wrong model, expects 423 at the session level with the rule's name, and shows the right model runs.
- `test_an_agent_rule_on_the_stream_cuts_the_session_on_the_line_it_fires` has the fake provider write a tool call whose command is `rm -rf /tmp/scratch && echo done`, reads the stream through the real Anthropic SDK, expects `stop_reason == "interrupted"`, expects the pieces the SDK saw to contain `rm -rf` and not `echo done`, expects the provider to have noticed the hang-up, reads the record and expects `interrupted.by == "session"` with the rule's name and the block's type, and shows the other agent streams the same content uncut.
- `test_a_rule_on_the_whole_response_can_alert_without_touching_a_switch` streams a short answer, expects one alert on the record with the rule and moment, a `rule.fired` event with `action: "alert"`, and every switch still off.

To test a rule of your own, add it to the `rules` list in the `config` fixture, give the fake provider the text or tool call that should fire it (`provider.text`, `provider.tool`, `provider.thinking`), and assert on the response status, the record and the feed as those tests do. Then run:

```bash
uv pip install -e ".[dev]" && pytest
```

The rule's own evaluation can also be tested without a server. `test_the_rules_at_a_moment_run_together_and_report_in_order` in `tests/test_config.py` builds `Rules` with a few `Rule` objects and calls `fired` with a `Stream`; the same works for checking that an expression fires on the view you expect.

### Against a real provider, by hand

Run the gateway with real keys, open the page with the operator's key, and make calls that should and should not fire. Three calls that exercise the example rules, with the Anthropic SDK on the Messages door:

```python
from anthropic import Anthropic
a = Anthropic(base_url="http://127.0.0.1:8080", api_key="the coder key")
H = {"X-Session": "rules-check"}
TOOL = [{"name": "shell", "description": "Run a shell command",
         "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}]

# 1. a request rule: refused before any connection opens; the agent is tripped
try:
    with a.messages.stream(model="anthropic/claude-sonnet-5-5", max_tokens=50,
                           system="deploy with AKIAIOSFODNN7EXAMPLE",
                           messages=[{"role": "user", "content": "hi"}], extra_headers=H) as s:
        s.get_final_message()
except Exception as e:
    print(e)      # Error code: 423 - {'error': {'type': 'killswitch', 'level': 'agent', 'reason': 'no credentials in requests', ...}}

# turn the agent back on with the operator's key before going on:
#   curl -X PUT -H "x-api-key: $MI_ADMIN_KEY" -d '{"on": false}' http://127.0.0.1:8080/v1/agents/coder/killswitch

# 2. a stream rule: cut while the tool call is being written; the session is tripped
with a.messages.stream(model="anthropic/claude-sonnet-5-5", max_tokens=300, tools=TOOL,
                       messages=[{"role": "user", "content": "Use the shell tool to run exactly: rm -rf /tmp/scratch-abc && echo done"}],
                       extra_headers=H) as s:
    m = s.get_final_message()
print(m.stop_reason)                                           # interrupted
print([c.input for c in m.content if c.type == "tool_use"])    # the arguments as far as they got, e.g. {} or a partial command
```

The Anthropic SDK parses a partial tool input as `{}` until it is whole; the record on the gateway holds the arguments as far as they were written, such as `{"cmd": "rm -rf /tmp/"}`, because the tap parses JSON as far as it has got.

What to look at afterwards:

```bash
curl -H "x-api-key: $MI_ADMIN_KEY" http://127.0.0.1:8080/v1/agents/coder/sessions/rules-check | jq '.streams[] | {id, refused, interrupted, alerts}'
```

The refused stream has `refused: {"by": "agent", "reason": "no credentials in requests"}` and `response: null`. The cut stream has `interrupted: {"by": "session", "reason": "no destructive commands", "at": "...", "block": {"at": "content[0]", "type": "tool_use"}}`.

### On the page

The page shows the same as it happens. Open it with the operator's key, pick the agent, pick the session, and:

- **The wire** at the top is a strip chart of the session's streams on one clock. A refused request is a short stub labelled with the rule. A cut is a mark on the bar labelled with the rule, at the moment it landed.
- **The transcript** shows each stream as it reads, with badges for refused, interrupted and alerts, and a footer line for the cut or the refusal.
- **The structure** shows the session as the record holds it. Each stream's row says how it ended: `end_turn`, `refused · agent · no credentials in requests`, or `cut · session · no destructive commands · at content[0]`. Open a stream to read its request with the system prompt in view, and its response marked with "connection closed by" and the time; open the content to find the block marked "cut here" and read the arguments as far as they got. A refused request is marked "refused by ...: no connection was opened".

The feed under the page, `GET /v1/live` with the operator's key, carries every event as a server-sent event. Watching it with `curl -N` is the quickest way to see a rule's order of events: `stream.started`, `rule.fired`, `killswitch`, `stream.ended`.

## 8. The killswitch

The killswitch is one state at three levels, each with the reason it was set:

```json
{"gateway": null,
 "agents": [{"agent": "coder", "reason": "no credentials in requests"}],
 "sessions": [{"agent": "support", "session": "s-7", "reason": "operator"}]}
```

`GET /v1/killswitch` returns it, with any key. Each `PUT` returns it too.

### What on means

Setting a level on does two things to everything in its scope, at once:

- **Running streams are cut.** The loop checks the switches on every line and every tenth of a second of silence, so the cut lands within that whatever the provider is doing, even mid-silence. The provider's connection is shut from outside the reader thread, so the provider stops generating. The caller's stream is ended as a whole message, with `interrupted` where the format says why it stopped, so the caller's SDK returns normally rather than raising. The record marks the stream `interrupted` with the level, the reason, the time and the block it landed on.
- **New calls are refused.** A call in scope is recorded as a stream that was refused, with the level and reason, and the caller gets 423 with a body saying `the killswitch is on for ...: reason`.

Nothing is paused and nothing is queued. Off means calls run again. A stream that was cut is over; the caller makes a new call if it wants to go on.

### Scope

| Level | Set by | Cuts and refuses |
|---|---|---|
| gateway | a person only | everything |
| agent | a person, or a gateway rule with `trip = "agent"` | every session of that agent |
| session | a person, or any rule with `trip = "session"` | that one session |

The widest level that is on is the one reported: with the gateway on, a refusal says `gateway` even if the agent is on too. Turning off a narrower level while a wider one is on changes nothing until the wider one is off.

### By hand

Each `PUT` takes `{"on": true}` or `{"on": false}` and needs the operator's key. The reason recorded is `operator`.

```bash
A="x-api-key: $MI_ADMIN_KEY"; GW=http://127.0.0.1:8080

# the whole gateway: every agent, every session
curl -X PUT -H "$A" -d '{"on": true}'  $GW/v1/killswitch
curl -X PUT -H "$A" -d '{"on": false}' $GW/v1/killswitch

# one agent: every session of coder
curl -X PUT -H "$A" -d '{"on": true}'  $GW/v1/agents/coder/killswitch
curl -X PUT -H "$A" -d '{"on": false}' $GW/v1/agents/coder/killswitch

# one session
curl -X PUT -H "$A" -d '{"on": true}'  $GW/v1/agents/coder/sessions/task-1/killswitch
curl -X PUT -H "$A" -d '{"on": false}' $GW/v1/agents/coder/sessions/task-1/killswitch
```

An agent's key cannot set any of these; it gets 401.

### On the page

The page has the same three switches as buttons: `gateway` in the header, `kill` beside each agent in the list, and `kill session` in the title of the picked session. Each shows its state and, when on, the reason: `operator`, or a rule's name. Turning one off is the same button. With an agent's key the page shows that agent's record and the switches greyed out: their state is visible, and only the operator's key can press them.

### Turning a rule's trip back off

A rule that trips sets a switch and the switch stays set. That is the design: the rule found something, and a person decides when to go on. The switch is turned off by hand, with the `PUT` or the page button, at the level the rule tripped. The reason on the switch and on the refused streams is the rule's name, so it is always clear which rule did it. The same rule will trip again on the next call that matches, so fix the cause before turning it off.

### What the caller sees

Through the Anthropic SDK, a refused call raises `APIStatusError` with status 423 and the body above; a cut stream returns a message whose `stop_reason` is `interrupted`. Through the OpenAI SDK, a refused call raises `APIStatusError` with 423; a cut stream's last chunk before `[DONE]` carries `"interrupted": true` with `finish_reason: "stop"`. `examples/agent.py` checks for `interrupted` after every call and stops its loop.

## 9. Reading the record

Once rules and switches have done something, the record is where to read what happened. Three endpoints cover it; an agent's key sees its own, the operator's sees all:

```bash
curl -H "$A" $GW/v1/agents                                   # every agent: sessions, streams, running, interrupted, alerts
curl -H "$A" $GW/v1/agents/coder                             # one agent's sessions
curl -H "$A" $GW/v1/agents/coder/sessions/task-1             # a session: its streams whole, with request, response and blocks
curl -H "$A" $GW/v1/agents/coder/sessions/task-1?blocks=false
curl -H "$A" $GW/v1/streams/str_d3132eca0bd8                 # one stream
curl -N -H "$A" $GW/v1/live                                  # every event as it happens
```

A stream holds `request` as received, `response` as the provider's own non-streaming shape composed from the stream, `blocks` with where each sits (`at`), when it started and ended and the provider's own fields, and then `refused`, `interrupted`, `alerts` and `error`. A refused request has `response: null` and no blocks. The record file holds the same objects, one per line, for every finished stream.

## 10. Where things are in the code

| You want to change | Look in |
|---|---|
| what a door's SDK does, how a cut stream is ended | `doors.py` |
| how a line becomes part of the record | `tap.py` |
| what a rule can see, when rules run | `rules.py` |
| the levels of the switch and what they report | `killswitch.py` |
| the loop: routing, the three moments, cutting | `gateway.py` |
| the record's shape and the feed's events | `record.py` |
| endpoints, keys, scoping | `server.py` |
| the file and its validation | `config.py` |
| the page | `ui.html` |

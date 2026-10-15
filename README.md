# model-interface

The model call and the tool execution, taken out of the agent.

An agent sends one request, in Anthropic's Messages format or OpenAI's Chat Completions format. This platform calls the model with its own provider key, runs any registered tool the model asks for in a container the agent can't reach, and answers with everything that happened. The agent keeps its context and its input. It never holds a provider key, and it never runs a command.

That is the split Satya Nadella asked for: the model and its actions inside an envelope the agent doesn't control, so that what the agent does can be bounded, seen and stopped from outside. This is the envelope. Guardrails, traces and the stop button attach to it next; this version is the envelope alone.

## How it works

```
their side                                  this platform                              provider
──────────                                  ─────────────                              ────────
context, input ──► POST /v1/messages ─────► key? model alias? ──► translate ──────────► model
                   or /v1/chat/completions         │                                      │
                                                   │  ◄── text, or a tool call ◄──────────┘
                                                   │
                                          registered tool? run it in the session's
                                          container, send the result back, call again
                                                   │
                   ◄── one response ───────────────┘  the model's words, and every call
                       with everything that ran       and result that ran here, folded in
```

Two loops, nested. Theirs captures input, assembles context, sends a request, shows the answer. Ours calls the model, runs tools in the sandbox, and repeats until the model answers or asks for a tool they run themselves. One request to us may be many model calls. From their side it looks like one call that did work on the way.

## Running it

```bash
cp model-interface.example.toml model-interface.toml   # providers, model aliases, tools, sandbox image, caller keys
export ANTHROPIC_API_KEY=... MI_KEY=some-secret
uv run model-interface                                 # http://127.0.0.1:8080, needs Docker for the sandbox
```

Then any client that speaks either format points at it:

```bash
MI_URL=http://127.0.0.1:8080 MI_KEY=some-secret uv run examples/agent.py "The tests are failing. Find out why and fix it."
```

```
> I'll run the tests to see what's failing.
$ python3 -m unittest -q
  └ FAIL: test_ten_percent_off ... AssertionError: 15 != 22.5
> total() subtracts the discount as a flat amount. It should take off a percentage.
$ sed -i 's/subtotal - discount/subtotal * (1 - discount \/ 100)/' prices.py && python3 -m unittest -q
  └ OK
> Fixed. Both tests pass.
```

The two commands ran in a container on the platform, in that session's workspace, with no network. The agent is 40 lines and holds nothing but a platform key.

## The request and the response

A request names a model alias and, optionally, tools by name. A tool named in the registry is ours: the model gets its full definition and we run it. Any other tool is theirs: it passes through, and when the model asks for it, we hand the response back for them to run it.

The response is one message. What ran here is folded into its content so the caller can show it and resend it unchanged:

```json
{"role": "assistant", "content": [
  {"type": "text", "text": "I'll run the tests."},
  {"type": "platform_tool_use", "id": "t1", "name": "shell", "input": {"cmd": "python3 -m unittest -q"}},
  {"type": "platform_tool_result", "tool_use_id": "t1", "content": "FAIL ..."},
  {"type": "text", "text": "Fixed."}
], "stop_reason": "end_turn", "usage": {"input_tokens": 1810, "output_tokens": 212}}
```

Usage is summed across every model call in the loop. On the next turn the caller sends the whole transcript back, folded blocks included, and we unfold it into the turns a provider expects. We keep no conversation state; the only state per session is its sandbox.

In Chat Completions format the same folded content rides on the message as a `platform` field, and `content` holds the model's words.

A session is named by the `X-Session` header. Each session gets one container, started on first use, with its workspace mounted at `/work`.

## The code

One class per concern, each small:

| Module | What it is |
|---|---|
| `formats.py` | `Format`: converts between a wire format and the canonical shape, both directions. `AnthropicFormat` is the identity. `OpenAIFormat` does the work. Each serves both a provider that speaks it and a caller that speaks it. |
| `providers.py` | `Provider`: a URL, a key and a format. `Model`: an alias pointing at a provider and its model name. |
| `sandbox.py` | `Sandbox` and `DockerSandbox`: one container per session, no network, workspace only. `Sandboxes` makes them on first use. |
| `tools.py` | `Tool` and `ShellTool`: a definition the model sees and a `run` that acts in the sandbox. |
| `transcript.py` | `unfold`: the caller's folded transcript, as the provider's turns. |
| `loop.py` | `Loop`: the inner loop. |
| `server.py` | The two endpoints and a models listing. |
| `config.py` | The TOML file, loaded into those objects. |

Tests run without keys, against a provider that plays a script and a sandbox that records commands. The sandbox's own tests run against a real container when a Docker daemon is reachable, and skip when it isn't:

```bash
uv pip install -e ".[dev]" && pytest
```

## Not yet

Streaming in either direction. Guardrails at the loop's yield points. A record of every step. Interrupts and checkpoints. Images in messages. Tools other than a shell. Each attaches to the loop as it stands; none changes its shape.

## License

MIT.

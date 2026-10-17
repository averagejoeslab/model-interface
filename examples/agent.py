# /// script
# dependencies = ["anthropic"]
# ///
"""A minimal harness agent on the gateway: streams the model's words, runs its own one tool, keeps its own context.

    MI_URL=http://127.0.0.1:8080 MI_KEY=<agent key> MI_MODEL=anthropic/claude-sonnet-5-5 MI_SESSION=demo uv run examples/agent.py "The tests are failing. Find out why and fix it."

The gateway holds the provider key, routes by the provider named in the model, taps every call as it
streams, runs the rules in the loop, and can cut any call with the killswitch. Open the gateway's page
to watch. The tool runs here; that line is where a sealed execution call goes.
"""
import os
import subprocess
import sys

from anthropic import Anthropic

client = Anthropic(base_url=os.environ["MI_URL"], api_key=os.environ["MI_KEY"])   # the gateway, with the agent's key
SESSION = os.environ.get("MI_SESSION", "demo")
MODEL = os.environ.get("MI_MODEL", "anthropic/claude-sonnet-5-5")                # provider/model

TOOLS = [{"name": "shell", "description": "Run a shell command in the current folder.",
          "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}]


def system():
    return ("You are an agent, working for the person who messages you. You have one tool, shell, and it is the only way "
            "to see or change anything: never ask for files or logs, look yourself. Check your work by reading it back.")


def shell(cmd):                                          # the body: replace with a sealed call when you have one
    print(f"$ {cmd}")
    done = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = done.stdout or f"(exit {done.returncode})"
    print("  └ " + out.rstrip().replace("\n", "\n    "))
    return out


messages = [{"role": "user", "content": " ".join(sys.argv[1:]) or input("< ")}]
for _ in range(8):
    with client.messages.stream(model=MODEL, max_tokens=2048, system=system(), tools=TOOLS, messages=messages,
                                extra_headers={"X-Session": SESSION}) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
        message = stream.get_final_message()
    print()
    messages.append({"role": "assistant", "content": message.content})
    if message.stop_reason == "interrupted":
        print("[cut by the killswitch]")
        break
    calls = [b for b in message.content if b.type == "tool_use"]
    if not calls:
        break
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": shell(c.input["cmd"])} for c in calls]})

# /// script
# dependencies = ["anthropic"]
# ///
"""An agent on the gateway: it streams the model's words as they arrive, and runs its own tools.

    MI_URL=http://127.0.0.1:8080 MI_KEY=... uv run examples/agent.py "The tests are failing. Find out why and fix it."

The gateway holds the provider key and watches every call. The tool runs here, on this machine;
that is the line to replace with a sealed execution service when you have one.
"""
import os
import subprocess
import sys

from anthropic import Anthropic

client = Anthropic(base_url=os.environ["MI_URL"], api_key=os.environ["MI_KEY"])   # the gateway, not a provider
SESSION = os.environ.get("MI_SESSION", "demo")                                   # what an operator can stop

TOOLS = [{"name": "shell", "description": "Run a shell command in the current folder.",
          "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}]


def system():
    return "You are an agent, working for the person who messages you. Use the shell tool to look and act. Check your work."


def shell(cmd):                                          # the body: replace with a sealed call when you have one
    print(f"$ {cmd}")
    done = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return done.stdout or f"(exit {done.returncode})"


messages = [{"role": "user", "content": " ".join(sys.argv[1:]) or input("< ")}]
while True:
    with client.messages.stream(model="coder", max_tokens=4096, system=system(), tools=TOOLS, messages=messages,
                                extra_headers={"X-Session": SESSION}) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
        message = stream.get_final_message()
    print()
    messages.append({"role": "assistant", "content": message.content})
    if message.stop_reason == "interrupted":
        print("[stopped by the gateway]")
        break
    calls = [b for b in message.content if b.type == "tool_use"]
    if not calls:
        break
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": c.id, "content": shell(c.input["cmd"])} for c in calls]})

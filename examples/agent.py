# /// script
# dependencies = ["anthropic"]
# ///
"""An agent on the platform: it keeps its context and its input, and nothing else.

    MI_URL=http://127.0.0.1:8080 MI_KEY=... uv run examples/agent.py "The tests are failing. Find out why and fix it."
"""
import os
import sys

from anthropic import Anthropic

client = Anthropic(base_url=os.environ["MI_URL"], api_key=os.environ["MI_KEY"])   # the platform, not a provider
SESSION = os.environ.get("MI_SESSION", "demo")          # one sandbox on the platform, kept between turns


# ── context ─────────────────────────────────────────────────────────────────
def system():
    return """You are an agent, working for the person who messages you.
Do what you're asked, fully and correctly. Know, don't guess: check what you do by reading it back.
Reading is always safe. Anything that changes the world happens one step at a time. If it can't be undone, ask first."""


# ── input ───────────────────────────────────────────────────────────────────
def capture_input():
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    line = ""
    while not line.strip():
        line = input("< ")
    return line


# ── showing what happened: nothing here runs a command ──────────────────────
def show(content):
    for b in content:
        if b.type == "text":
            print("> " + b.text.replace("\n", "\n  "))
        if b.type == "platform_tool_use":
            print(f"$ {b.input['cmd']}")
        if b.type == "platform_tool_result":
            print("  └ " + b.content.rstrip().replace("\n", "\n    "))


# ── the outer loop: call, show, hand back ───────────────────────────────────
messages = [{"role": "user", "content": capture_input()}]
while True:
    response = client.messages.create(model="coder", max_tokens=16384, system=system(), tools=[{"name": "shell"}],
                                      messages=messages, extra_headers={"X-Session": SESSION})
    messages.append({"role": "assistant", "content": response.content})
    show(response.content)
    if len(sys.argv) > 1:
        break
    messages.append({"role": "user", "content": capture_input()})

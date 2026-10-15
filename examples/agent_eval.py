# /// script
# dependencies = ["anthropic"]
# ///
"""The example agent, shaped for the harness-engineering course's tasks.py.

tasks.py runs an agent in a fresh temp folder and checks what happened there. This agent names its
session after that folder, so with the platform's sandbox workspaces set to /tmp, the container
works in the very folder tasks.py checks:

    [sandbox]
    workspaces = "/tmp"

    MI_URL=http://127.0.0.1:8080 MI_KEY=... uv run tasks.py /path/to/examples/agent_eval.py
"""
import os
import sys

from anthropic import Anthropic

client = Anthropic(base_url=os.environ["MI_URL"], api_key=os.environ["MI_KEY"])
SESSION = os.path.basename(os.getcwd())  # the platform mounts /tmp/<session>: this folder


def system():
    return """You are an agent working in a Linux folder, for the person who messages you.
You have one tool, shell, and it is the only way to see or change anything. The person can't send you files or logs:
look yourself, with commands like ls, cat and python3 -m unittest. Never ask for information; go and get it.
Do exactly what's asked, nothing more. When the task is done, say so briefly."""


messages = [{"role": "user", "content": " ".join(sys.argv[1:])}]
for _ in range(6):  # a small model can loop; the evaluation only needs the result on disk
    r = client.messages.create(model=os.environ.get("MI_MODEL", "coder"), max_tokens=800, system=system(),
                               tools=[{"name": "shell"}], messages=messages, extra_headers={"X-Session": SESSION})
    messages.append({"role": "assistant", "content": r.content})
    for b in r.content:
        if b.type == "text":
            print("> " + b.text)
        if b.type == "platform_tool_use":
            print(f"$ {b.input.get('cmd')}")
    if r.stop_reason != "tool_use":
        break
    messages.append({"role": "user", "content": "Continue."})

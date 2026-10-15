# /// script
# dependencies = ["anthropic", "openai"]
# ///
"""Every path, live: both caller SDKs against both model aliases, each run in its own sandbox.

    MI_URL=http://127.0.0.1:8080 MI_KEY=... uv run examples/check.py

Needs a platform whose config has a "coder" alias in Messages format and a "coder-openai" alias in
Chat Completions format, and whose sandbox workspaces are under WORKSPACES (default ~/.model-interface/workspaces).
"""
import os
import shutil
import time
import uuid

from anthropic import Anthropic
from openai import OpenAI

URL, KEY = os.environ["MI_URL"], os.environ["MI_KEY"]
WORKSPACES = os.path.expanduser(os.environ.get("WORKSPACES", "~/.model-interface/workspaces"))
TASK = "Write the names of the .py files in this folder, one per line, sorted, into files.txt. Use the shell tool, then say done."
SYSTEM = "You are an agent working in a Linux folder. Use the shell tool to act. Check your work by reading it back."
WANT = "prices.py\ntest_prices.py\n"


def session():
    """A fresh session whose workspace holds two small files."""
    s = f"check-{uuid.uuid4().hex[:6]}"
    os.makedirs(f"{WORKSPACES}/{s}")
    open(f"{WORKSPACES}/{s}/prices.py", "w").write("def total(items):\n    return sum(p * q for p, q in items)\n")
    open(f"{WORKSPACES}/{s}/test_prices.py", "w").write("from prices import total\nassert total([(10, 2)]) == 20\n")
    return s


def via_anthropic_sdk(model, s):
    client = Anthropic(base_url=URL, api_key=KEY)
    r = client.messages.create(model=model, max_tokens=600, system=SYSTEM, tools=[{"name": "shell"}],
                               messages=[{"role": "user", "content": TASK}], extra_headers={"X-Session": s})
    return [b.input["cmd"] for b in r.content if b.type == "platform_tool_use"], r.stop_reason


def via_openai_sdk(model, s):
    client = OpenAI(base_url=URL + "/v1", api_key=KEY, default_headers={"X-Session": s})
    r = client.chat.completions.create(model=model, max_tokens=600, tools=[{"type": "function", "function": {"name": "shell"}}],
                                       messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}])
    platform = r.choices[0].message.model_extra.get("platform") or []
    return [b["input"]["cmd"] for b in platform if b["type"] == "platform_tool_use"], r.choices[0].finish_reason


print(f"{'caller SDK':<12} {'alias':<14} {'tool ran':<9} {'files.txt right':<16} {'stop':<10} time")
for caller, fn in (("anthropic", via_anthropic_sdk), ("openai", via_openai_sdk)):
    for model in ("coder", "coder-openai"):
        s, t = session(), time.time()
        try:
            cmds, stop = fn(model, s)
            path = f"{WORKSPACES}/{s}/files.txt"
            got = open(path).read() if os.path.exists(path) else None
            print(f"{caller:<12} {model:<14} {str(bool(cmds)):<9} {str(got == WANT):<16} {stop:<10} {time.time() - t:.0f}s")
        except Exception as e:
            print(f"{caller:<12} {model:<14} ERROR {type(e).__name__}: {str(e)[:160]}")
        shutil.rmtree(f"{WORKSPACES}/{s}", ignore_errors=True)

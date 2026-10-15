"""The inner loop.

Call the model. If it asks for a tool the platform has, run it in the session's sandbox, send the
result back, and call again. Stop when the model answers, or asks for a tool the caller runs.
"""
from .formats import PLATFORM_RESULT, PLATFORM_USE
from .providers import Model
from .sandbox import Sandboxes
from .tools import Tool
from .transcript import unfold


class Loop:
    def __init__(self, models: dict[str, Model], tools: dict[str, Tool], sandboxes: Sandboxes):
        self.models = models
        self.tools = tools
        self.sandboxes = sandboxes

    def run(self, request: dict, session: str) -> dict:
        model = self.models[request["model"]]
        asked = [t["name"] for t in request.get("tools", [])]
        ours = [name for name in asked if name in self.tools]
        tools = [self.tools[n].definition for n in ours] + [t for t in request.get("tools", []) if t["name"] not in ours]
        messages = unfold(request["messages"])
        content, usage = [], {"input_tokens": 0, "output_tokens": 0}
        while True:
            response = model.complete({**request, "tools": tools, "messages": messages})
            for k in usage:
                usage[k] += response["usage"].get(k, 0)
            messages.append({"role": "assistant", "content": response["content"]})
            calls = [b for b in response["content"] if b["type"] == "tool_use" and b["name"] in ours]
            theirs = [b for b in response["content"] if b["type"] == "tool_use" and b["name"] not in ours]
            content += [{**b, "type": PLATFORM_USE} if b in calls else b for b in response["content"] if b not in theirs]
            if calls:
                results = [{"type": "tool_result", "tool_use_id": c["id"],
                            "content": self.tools[c["name"]].run(self.sandboxes.get(session), c["input"])} for c in calls]
                messages.append({"role": "user", "content": results})
                content += [{**r, "type": PLATFORM_RESULT} for r in results]
            if theirs or not calls:  # the model answered, or asked for a tool the caller runs: hand back
                return {**response, "content": content + theirs, "usage": usage}

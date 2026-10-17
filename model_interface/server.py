"""The endpoints: two doors, streaming only; the record, read and live; the switches; the page."""
import itertools
import json
import pathlib
import queue
import re

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import Config
from .doors import DOORS, Door
from .gateway import Gateway, Refused
from .killswitch import Killswitch
from .providers import Agent
from .record import Record
from .rules import Rules

SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UI = pathlib.Path(__file__).with_name("ui.html")
ALL = "*"


def create_app(config: Config, gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(title="model-interface")
    gateway = gateway or Gateway(config.providers, Record(config.record), Killswitch(), Rules(config.rules))
    record, killswitch = gateway.record, gateway.killswitch

    # ── who is asking ────────────────────────────────────────────────────────

    def presented(x_api_key: str = Header(""), authorization: str = Header(""), key: str = Query("")) -> str:
        return x_api_key or authorization.removeprefix("Bearer ").strip() or key

    def agent(key: str = Depends(presented)) -> Agent:
        a = config.agent_of(key)
        if a is None:
            raise HTTPException(401, "unknown agent key")
        return a

    def viewer(key: str = Depends(presented)) -> str:
        if config.admin and key == config.admin:
            return ALL
        a = config.agent_of(key)
        if a is None:
            raise HTTPException(401, "unknown key")
        return a.name

    def operator(key: str = Depends(presented)) -> str:
        if not config.admin or key != config.admin:
            raise HTTPException(401, "the operator's key is needed")
        return ALL

    def visible(who: str, name: str) -> Agent:
        if name not in config.agents:
            raise HTTPException(404, "no such agent")
        if who not in (ALL, name):
            raise HTTPException(403, "another agent's record")
        return config.agents[name]

    @app.get("/v1/me")
    def me(who: str = Depends(viewer)):
        return {"operator": who == ALL, "agent": None if who == ALL else who}

    # ── the two doors ────────────────────────────────────────────────────────

    def serve(door: Door, body: dict, a: Agent, session: str, headers: dict):
        if not body.get("stream"):
            raise HTTPException(400, "this gateway streams only: send stream=true")
        if not SESSION.fullmatch(session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        try:
            stream = gateway.open(a.name, session, door, body)
        except Refused as r:
            return JSONResponse({"error": r.error}, status_code=r.status)
        lines = gateway.stream(stream, headers)
        try:
            first = next(lines)                        # the provider's refusal, if any, surfaces here
        except StopIteration:
            raise HTTPException(502, "the provider sent nothing")
        except Exception as e:
            status = door.status_of(e)
            if status:
                return JSONResponse(status[1], status_code=status[0])
            raise HTTPException(502, f"the provider failed: {e}")
        return StreamingResponse(itertools.chain([first], lines), media_type="text/event-stream",
                                 headers={"X-Stream": stream.id, "X-Session": session, "X-Agent": a.name})

    @app.post("/v1/messages")
    def messages(body: dict, request: Request, a: Agent = Depends(agent), x_session: str = Header("default")):
        return serve(DOORS["messages"], body, a, x_session, dict(request.headers))

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request, a: Agent = Depends(agent), x_session: str = Header("default")):
        return serve(DOORS["chat"], body, a, x_session, dict(request.headers))

    # ── what is configured ───────────────────────────────────────────────────

    @app.get("/v1/providers")
    def providers(who: str = Depends(viewer)):
        return {"data": [{"provider": p.name, "url": p.url} for p in config.providers.values()]}

    @app.get("/v1/rules")
    def rules(who: str = Depends(viewer)):
        return {"data": [{"name": r.name, "on": r.on, "when": r.when, "action": r.action, "trip": r.trip, "expr": r.expr}
                         for r in config.rules if who == ALL or r.on in ("gateway", who)]}

    # ── the record: agents, sessions, streams, pieces ────────────────────────

    def with_switch(summary: dict) -> dict:
        return {**summary, "killswitch": killswitch.is_on(summary["agent"], summary.get("session"))}

    @app.get("/v1/agents")
    def agents(who: str = Depends(viewer)):
        names = list(config.agents) if who == ALL else [who]
        return {"data": [with_switch(a) for a in record.agents(names)]}

    @app.get("/v1/agents/{name}")
    def agent_detail(name: str, who: str = Depends(viewer)):
        a = visible(who, name)
        return {"agent": a.name, "killswitch": killswitch.is_on(a.name), "sessions": [with_switch(s) for s in record.list(a.name)]}

    @app.get("/v1/agents/{name}/sessions/{session}")
    def session_detail(name: str, session: str, blocks: bool = True, who: str = Depends(viewer)):
        visible(who, name)
        s = record.session(name, session)
        if s is None:
            raise HTTPException(404, "no such session")
        return with_switch(s.snapshot(blocks))

    @app.get("/v1/streams/{stream}")
    def stream_detail(stream: str, who: str = Depends(viewer)):
        s = record.stream(stream)
        if s is None or who not in (ALL, s.agent):
            raise HTTPException(404, "no such stream")
        return s.snapshot()

    # ── live: the record's own events as they happen ─────────────────────────

    def feed(scope: str):
        q = record.watch(scope)

        def events():
            try:
                yield "event: watching\ndata: " + json.dumps({"scope": scope, "killswitch": killswitch.state()}) + "\n\n"
                while True:
                    try:
                        e = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
            finally:
                record.unwatch(scope, q)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/live")
    def live(who: str = Depends(viewer)):
        return feed(who)

    @app.get("/v1/agents/{name}/live")
    def agent_live(name: str, who: str = Depends(viewer)):
        return feed(visible(who, name).name)

    @app.get("/v1/agents/{name}/sessions/{session}/live")
    def session_live(name: str, session: str, who: str = Depends(viewer)):
        return feed(f"{visible(who, name).name}/{session}")

    # ── the killswitch: gateway, agent, session ──────────────────────────────

    @app.get("/v1/killswitch")
    def killswitch_state(who: str = Depends(viewer)):
        return killswitch.state()

    @app.put("/v1/killswitch")
    def killswitch_gateway(body: dict, who: str = Depends(operator)):
        killswitch.set(bool(body.get("on")))
        return killswitch.state()

    @app.put("/v1/agents/{name}/killswitch")
    def killswitch_agent(name: str, body: dict, who: str = Depends(operator)):
        killswitch.set(bool(body.get("on")), visible(who, name).name)
        return killswitch.state()

    @app.put("/v1/agents/{name}/sessions/{session}/killswitch")
    def killswitch_session(name: str, session: str, body: dict, who: str = Depends(operator)):
        killswitch.set(bool(body.get("on")), visible(who, name).name, session)
        return killswitch.state()

    # ── the page ─────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(UI, media_type="text/html")

    return app

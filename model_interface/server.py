"""The endpoints: one per format, streaming only; the record, read and live; the killswitch; and the page."""
import itertools
import json
import pathlib
import queue
import re

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .config import Config
from .formats import FORMATS, Format
from .gateway import Gateway, Record, Stream
from .killswitch import Killswitch
from .providers import Agent

SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UI = pathlib.Path(__file__).with_name("ui.html")
ALL = "*"                                  # the operator's view: every agent


def create_app(config: Config, gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(title="model-interface")
    gateway = gateway or Gateway(Record(config.record), Killswitch())
    record, killswitch = gateway.record, gateway.killswitch

    # ── who is asking ────────────────────────────────────────────────────────

    def presented(x_api_key: str = Header(""), authorization: str = Header(""), key: str = Query("")) -> str:
        """The key, as a header or as ?key= for the browser's event source."""
        return x_api_key or authorization.removeprefix("Bearer ").strip() or key

    def agent(key: str = Depends(presented)) -> Agent:
        """An agent, by its key: the only identity that may call a model."""
        a = config.agent_of(key)
        if a is None:
            raise HTTPException(401, "unknown agent key")
        return a

    def viewer(key: str = Depends(presented)) -> str:
        """Who may read the record: the operator sees every agent, an agent sees itself."""
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

    # ── the two endpoints ────────────────────────────────────────────────────

    def serve(format: Format, body: dict, a: Agent, session: str, headers: dict):
        if not body.get("stream"):
            raise HTTPException(400, "this gateway streams only: send stream=true")
        if not SESSION.fullmatch(session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        model = a.resolve(format, body.get("model", ""))
        if model is None:
            elsewhere = a.granted(body.get("model", ""))
            if elsewhere:
                raise HTTPException(400, f"{body['model']!r} is granted to {a.name!r} on {elsewhere[0].format.name}; use {elsewhere[0].format.endpoint}")
            raise HTTPException(404, f"{body.get('model')!r} is not a model agent {a.name!r} may use on {format.endpoint}")
        stream = Stream(agent=a.name, session=session, model=model, request=body)
        lines = gateway.stream(stream, headers)
        try:
            first = next(lines)                        # the provider's refusal, if any, surfaces here
        except StopIteration:
            raise HTTPException(502, "the provider sent nothing")
        except Exception as e:
            status = format.status_of(e)
            if status:
                return JSONResponse(status[1], status_code=status[0])
            raise HTTPException(502, f"the provider failed: {e}")
        return StreamingResponse(itertools.chain([first], lines), media_type="text/event-stream",
                                 headers={"X-Stream": stream.id, "X-Session": session, "X-Agent": a.name})

    def refused(level: str, a: Agent, session: str):
        where = {"gateway": "the whole gateway", "agent": f"agent {a.name!r}", "session": f"session {session!r} of agent {a.name!r}"}[level]
        return JSONResponse({"error": {"type": "killswitch", "level": level, "message": f"the killswitch is on for {where}"}}, status_code=423)

    @app.post("/v1/messages")
    def messages(body: dict, request: Request, a: Agent = Depends(agent), x_session: str = Header("default")):
        level = killswitch.scope_of(a.name, x_session)
        return refused(level, a, x_session) if level else serve(FORMATS["anthropic"], body, a, x_session, dict(request.headers))

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request, a: Agent = Depends(agent), x_session: str = Header("default")):
        level = killswitch.scope_of(a.name, x_session)
        return refused(level, a, x_session) if level else serve(FORMATS["openai"], body, a, x_session, dict(request.headers))

    @app.get("/v1/models")
    def models(who: str = Depends(viewer)):
        """The models an agent may use, in the list shape both SDKs expect; everything, for the operator."""
        agents = config.agents.values() if who == ALL else [config.agents[who]]
        seen = {m.id: m for a in agents for m in a.models}
        return {"object": "list", "data": [{"id": m.name, "object": "model", "provider": m.provider.name, "format": m.format.name,
                                            "endpoint": m.format.endpoint} for m in seen.values()]}

    # ── the record: agents, sessions, streams, blocks ────────────────────────

    def with_switch(summary: dict) -> dict:
        return {**summary, "killswitch": killswitch.is_on(summary["agent"], summary.get("session"))}

    @app.get("/v1/agents")
    def agents(who: str = Depends(viewer)):
        names = list(config.agents) if who == ALL else [who]
        return {"data": [with_switch(a) for a in record.agents(names)]}

    @app.get("/v1/agents/{name}")
    def agent_detail(name: str, who: str = Depends(viewer)):
        a = visible(who, name)
        return {"agent": a.name, "killswitch": killswitch.is_on(a.name), "models": [m.id for m in a.models],
                "sessions": [with_switch(s) for s in record.list(a.name)]}

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

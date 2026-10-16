"""The endpoints: one per format, streaming only; the record, read and live; and the page that shows it."""
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

SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UI = pathlib.Path(__file__).with_name("ui.html")


def create_app(config: Config, gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(title="model-interface")
    gateway = gateway or Gateway(config.models, Record(config.record))
    record = gateway.record

    def authorized(x_api_key: str = Header(""), authorization: str = Header(""), key: str = Query("")) -> str:
        """The caller's name. The key comes as a header, or as ?key= for the browser's event source."""
        presented = x_api_key or authorization.removeprefix("Bearer ").strip() or key
        if presented not in config.keys:
            raise HTTPException(401, "unknown key")
        return config.keys[presented]

    def serve(format: Format, body: dict, caller: str, session: str, headers: dict):
        if not body.get("stream"):
            raise HTTPException(400, "this gateway streams only: send stream=true")
        if not SESSION.fullmatch(session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        model = config.models.get(body.get("model"))
        if model is None:
            raise HTTPException(404, f"unknown model {body.get('model')!r}")
        if model.provider.format is not format:
            raise HTTPException(400, f"{body['model']!r} speaks {model.provider.format.name}; use {model.provider.format.endpoint}")
        stream = Stream(session=session, key=caller, model=body["model"], format=format.name, request=body)
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
                                 headers={"X-Stream": stream.id, "X-Session": session})

    def feed(scope: str):
        """The record's events for one session, or for everything, as server-sent events."""
        q = record.watch(scope)

        def events():
            try:
                yield "event: watching\ndata: " + json.dumps({"scope": scope}) + "\n\n"
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

    # ── the two endpoints ────────────────────────────────────────────────────

    @app.post("/v1/messages")
    def messages(body: dict, request: Request, caller: str = Depends(authorized), x_session: str = Header("default")):
        return serve(FORMATS["anthropic"], body, caller, x_session, dict(request.headers))

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request, caller: str = Depends(authorized), x_session: str = Header("default")):
        return serve(FORMATS["openai"], body, caller, x_session, dict(request.headers))

    @app.get("/v1/models")
    def models(caller: str = Depends(authorized)):
        return {"object": "list", "data": [{"id": alias, "object": "model", "format": m.provider.format.name} for alias, m in config.models.items()]}

    # ── the record: callers, sessions, streams, blocks ───────────────────────

    @app.get("/v1/callers")
    def callers(caller: str = Depends(authorized)):
        return {"data": record.callers()}

    @app.get("/v1/sessions")
    def sessions(caller: str = Depends(authorized)):
        return {"data": record.list()}

    @app.get("/v1/sessions/{session}")
    def session(session: str, blocks: bool = True, caller: str = Depends(authorized)):
        s = record.session(session)
        if s is None:
            raise HTTPException(404, "no such session")
        return s.snapshot(blocks)

    @app.get("/v1/sessions/{session}/live")
    def session_live(session: str, caller: str = Depends(authorized)):
        return feed(session)

    @app.get("/v1/live")
    def live(caller: str = Depends(authorized)):
        return feed("*")

    @app.get("/v1/streams/{stream}")
    def stream(stream: str, caller: str = Depends(authorized)):
        s = record.stream(stream)
        if s is None:
            raise HTTPException(404, "no such stream")
        return s.snapshot()

    # ── the page ─────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(UI, media_type="text/html")

    return app

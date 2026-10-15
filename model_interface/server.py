"""The endpoints: one per format, streaming only; the record, live; and the killswitch."""
import itertools
import json
import queue
import re

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .formats import FORMATS, Format
from .gateway import Gateway, Killswitch, Record, Stream

SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def create_app(config: Config, gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(title="model-interface")
    gateway = gateway or Gateway(config.models, Killswitch(), Record(config.record))

    def authorize(x_api_key: str, authorization: str) -> str:
        key = x_api_key or authorization.removeprefix("Bearer ").strip()
        if key not in config.keys:
            raise HTTPException(401, "unknown key")
        return config.keys[key]

    def serve(format: Format, body: dict, key: str, session: str, headers: dict):
        if not body.get("stream"):
            raise HTTPException(400, "this gateway streams only: send stream=true")
        if not SESSION.fullmatch(session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        model = config.models.get(body.get("model"))
        if model is None:
            raise HTTPException(404, f"unknown model {body.get('model')!r}")
        if model.provider.format is not format:
            raise HTTPException(400, f"{body['model']!r} speaks {model.provider.format.name}; use {model.provider.format.endpoint}")
        if gateway.killswitch.is_on(session):
            raise HTTPException(423, f"the killswitch is on for {'everything' if gateway.killswitch.all else session}")
        stream = Stream(session=session, key=key, model=body["model"], format=format.name, request=body)
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

    @app.post("/v1/messages")
    def messages(body: dict, request: Request, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        return serve(FORMATS["anthropic"], body, authorize(x_api_key, authorization), x_session, dict(request.headers))

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, request: Request, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        return serve(FORMATS["openai"], body, authorize(x_api_key, authorization), x_session, dict(request.headers))

    @app.get("/v1/models")
    def models(x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return {"object": "list", "data": [{"id": alias, "object": "model", "format": m.provider.format.name} for alias, m in config.models.items()]}

    # ── the record: sessions of streams of blocks, live ──────────────────────

    @app.get("/v1/sessions")
    def sessions(x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return {"data": gateway.record.list()}

    @app.get("/v1/sessions/{session}")
    def session(session: str, blocks: bool = True, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        s = gateway.record.session(session)
        if s is None:
            raise HTTPException(404, "no such session")
        return s.snapshot(blocks)

    @app.get("/v1/sessions/{session}/live")
    def live(session: str, x_api_key: str = Header(""), authorization: str = Header("")):
        """The session's record events as they happen: streams opening and ending, blocks opening, growing, closing."""
        authorize(x_api_key, authorization)
        q = gateway.record.watch(session)

        def events():
            try:
                yield "event: watching\ndata: " + json.dumps({"session": session}) + "\n\n"
                while True:
                    try:
                        e = q.get(timeout=15)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
            finally:
                gateway.record.unwatch(session, q)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/streams/{stream}")
    def stream(stream: str, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        s = gateway.record.stream(stream)
        if s is None:
            raise HTTPException(404, "no such stream")
        return s.snapshot()

    # ── the killswitch: a flag for everything, and one per session ───────────

    @app.get("/v1/killswitch")
    def killswitch(x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return gateway.killswitch.state()

    @app.put("/v1/killswitch")
    def killswitch_all(body: dict, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        gateway.killswitch.set(bool(body.get("on")))
        return gateway.killswitch.state()

    @app.put("/v1/killswitch/{session}")
    def killswitch_session(session: str, body: dict, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        gateway.killswitch.set(bool(body.get("on")), session)
        return gateway.killswitch.state()

    return app

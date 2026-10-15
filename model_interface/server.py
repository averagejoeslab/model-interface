"""The endpoints: two for model calls, in either format, and a few to watch and stop them."""
import itertools
import re

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .formats import FORMATS, Format, assemble
from .gateway import Gateway, Record, Run, Stops

SESSION = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"


def create_app(config: Config, gateway: Gateway | None = None) -> FastAPI:
    app = FastAPI(title="model-interface")
    gateway = gateway or Gateway(config.models, Stops(), Record(config.record))

    def authorize(x_api_key: str, authorization: str) -> str:
        key = x_api_key or authorization.removeprefix("Bearer ").strip()
        if key not in config.keys:
            raise HTTPException(401, "unknown key")
        return config.keys[key]

    def serve(format: Format, body: dict, key: str, session: str):
        if not re.fullmatch(SESSION, session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        request = format.request_in(body)
        if request.get("model") not in config.models:
            raise HTTPException(404, f"unknown model {request.get('model')!r}")
        run = Run(session=session, key=key, model=request["model"], format=format.name, stream=bool(body.get("stream")))
        events = gateway.stream(run, request)
        first = next(events, None)                       # the provider's refusal, if any, surfaces here
        if first is None:
            raise HTTPException(502, "the provider sent nothing")
        if first["type"] == "error":
            raise HTTPException(502, f"the provider failed: {run.error}")
        events = itertools.chain([first], events)
        headers = {"X-Run": run.id, "X-Session": session}
        if run.stream:
            return StreamingResponse(format.events_out(events), media_type="text/event-stream", headers=headers)
        return JSONResponse(format.response_out(assemble(events)), headers=headers)

    @app.post("/v1/messages")
    def messages(body: dict, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        return serve(FORMATS["anthropic"], body, authorize(x_api_key, authorization), x_session)

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        return serve(FORMATS["openai"], body, authorize(x_api_key, authorization), x_session)

    @app.get("/v1/models")
    def models(x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return {"object": "list", "data": [{"id": alias, "object": "model"} for alias in config.models]}

    @app.get("/v1/runs")
    def runs(session: str | None = None, limit: int = 50, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return {"data": gateway.record.runs(session, limit)}

    @app.post("/v1/interrupt/{scope}")                   # a run id, a session, a key name, or * for everything
    def interrupt(scope: str, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        gateway.stops.add(scope)
        return {"stopped": sorted(gateway.stops.scopes)}

    @app.delete("/v1/interrupt/{scope}")
    def resume(scope: str, x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        gateway.stops.remove(scope)
        return {"stopped": sorted(gateway.stops.scopes)}

    return app

"""The two endpoints: Messages and Chat Completions. Same loop behind both."""
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .config import Config
from .formats import FORMATS, Format
from .loop import Loop


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="model-interface")
    loop = Loop(config.models, config.tools, config.sandboxes)

    def authorize(x_api_key: str, authorization: str):
        key = x_api_key or authorization.removeprefix("Bearer ").strip()
        if key not in config.keys:
            raise HTTPException(401, "unknown key")

    def serve(format: Format, body: dict, session: str):
        if body.get("stream"):
            raise HTTPException(400, "streaming isn't supported yet: send stream=false")
        if not config.sandboxes.valid(session):
            raise HTTPException(400, "X-Session must be letters, digits, dots, dashes or underscores")
        request = format.request_in(body)
        if request.get("model") not in config.models:
            raise HTTPException(404, f"unknown model {request.get('model')!r}")
        try:
            response = loop.run(request, session)
        except httpx.HTTPStatusError as e:  # the provider refused: pass its answer through
            return JSONResponse(e.response.status_code, e.response.json() if e.response.headers.get("content-type", "").startswith("application/json") else {"error": e.response.text})
        return format.response_out(response)

    @app.post("/v1/messages")
    def messages(body: dict, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        authorize(x_api_key, authorization)
        return serve(FORMATS["anthropic"], body, x_session)

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict, x_api_key: str = Header(""), authorization: str = Header(""), x_session: str = Header("default")):
        authorize(x_api_key, authorization)
        return serve(FORMATS["openai"], body, x_session)

    @app.get("/v1/models")
    def models(x_api_key: str = Header(""), authorization: str = Header("")):
        authorize(x_api_key, authorization)
        return {"object": "list", "data": [{"id": alias, "object": "model"} for alias in config.models]}

    return app

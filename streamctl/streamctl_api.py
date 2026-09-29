#!/usr/bin/env python3
"""streamctl HTTP API - FastAPI surface over streamctl_core.

Same verbs as the CLI. Mutating calls (create/deploy/destroy) require
Authorization: Bearer <STREAMCTL_API_TOKEN> when the token is set in conf.
Runs on 127.0.0.1:$STREAMCTL_API_PORT (localhost-only by design).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typing import Annotated

import streamctl_core as core
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="streamctl API", version="1.0")
CONF = core.load_conf()


def auth(request: Request) -> None:
    token = CONF.get("STREAMCTL_API_TOKEN", "")
    if not token:
        return
    supplied = request.headers.get("Authorization", "")
    if supplied != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


@app.exception_handler(core.StreamctlError)
def streamctl_error(request: Request, exc: core.StreamctlError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/healthz")
def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/apps")
def apps() -> list[dict[str, object]]:
    return core.status(CONF)


@app.get("/apps/{name}")
def one(name: str) -> dict[str, object]:
    try:
        return core.get_app(CONF, name)
    except core.StreamctlError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.post("/apps")
def create_app(body: dict[str, object], _: Annotated[None, Depends(auth)]) -> dict[str, object]:
    name = str(body.get("name", ""))
    source = str(body.get("source", ""))
    return core.create(CONF, name, source)


@app.post("/apps/{name}/deploy")
def deploy_app(name: str, _: Annotated[None, Depends(auth)]) -> dict[str, object]:
    return core.deploy(CONF, name)


@app.delete("/apps/{name}")
def destroy_app(name: str, _: Annotated[None, Depends(auth)]) -> dict[str, object]:
    return core.destroy(CONF, name)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app, host="127.0.0.1", port=int(CONF["STREAMCTL_API_PORT"]), log_level="warning"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""streamctl HTTP API - FastAPI surface over streamctl_core.

Same verbs as the CLI. Mutating calls (create/deploy/destroy) require
Authorization: Bearer <STREA...KEN> when the token is set in conf.
Runs on 127.0.0.1:$STREAMCTL_API_PORT (localhost-only by design), also
publicly exposed at https://streamlit.internal.goodshepherdinsights.com
(since 2026-10-07) via the gsi-homeserver Cloudflare tunnel.

Swagger UI: /docs  - OpenAPI schema: /openapi.json
The schema below carries rich per-route metadata: summaries, descriptions
with curl examples, and response payload examples.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import streamctl_core as core  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

app = FastAPI(
    title="streamctl API",
    version="1.0",
    description=(
        "GSI Streamlit fleet controller (self-host backend).\n\n"
        "Create, deploy, and destroy Streamlit apps. Each app boots as its own\n"
        "`streamlit@<name>.service` systemd unit on the next free port from the\n"
        "pool 8502-8599, with its own uv venv and auto-installed requirements.txt.\n\n"
        "**Base URLs**\n"
        "- Local: `http://127.0.0.1:8510` (localhost only)\n"
        "- Public: `https://streamlit.internal.goodshepherdinsights.com` "
        "(Cloudflare tunnel, since 2026-10-07)\n\n"
        "**Auth:** mutating routes (create/deploy/destroy) require "
        "`Authorization: Bearer <STREAMCTL_API_TOKEN>` when the token is set in "
        "`/etc/streamctl/streamctl.conf`. GET routes are open by design.\n\n"
        "**App lifecycle:** create (clone/copy source, venv, pip, boot, health-wait) "
        "-> deploy (reboot/redeploy) -> destroy (unit off, dir removed, deregistered).\n\n"
        "**Public per-app URLs:** `https://{name}.{STREAMCTL_DOMAIN}` when "
        "`STREAMCTL_DOMAIN` is set in conf (unset today -> rows show `public_url: \"-\"`).\n\n"
        "**Fleet state source:** `/etc/streamctl/apps.tsv`.\n\n"
        "Related skill: `streamctl-containers` (the Cloudflare Containers backend API).\n\n"
        "Pitfalls (from source):\n"
        "- `destroy` deletes the deployed copy under `/home/dev/streamlit-apps/<name>`;\n"
        "  it never touches the SOURCE directory (a local `source` dir or the origin of "
        "a git clone survives).\n"
        "- `create` is synchronous and health-waits up to 60s; failures return 400 with "
        "the journalctl hint.\n"
        "- App `name` must be DNS-safe: `[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?`."
    ),
)

CONF = core.load_conf()


def auth(request: Request) -> None:
    token = CONF.get("STREAMCTL_API_TOKEN", "")
    if not token:
        return
    supplied = request.headers.get("Authorization", "")
    if supplied != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


class CreateAppBody(BaseModel):
    """Request body for creating a new Streamlit app."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"name": "myapp", "source": "/tmp/appsrc-myapp"},
                {"name": "myapp", "source": "https://github.com/org/my-app.git"},
            ]
        }
    }

    name: str = Field(
        description=(
            "DNS-safe app name (lowercase alnum + hyphen). Becomes the systemd unit "
            "suffix (`streamlit@<name>`), the fleet key, and - when STREAMCTL_DOMAIN "
            "is set - the subdomain `<name>.<domain>`."
        )
    )
    source: str = Field(
        description=(
            "Where the app code comes from: an absolute directory containing app.py "
            "(copied) or a git URL (shallow-cloned). If the source lacks app.py a "
            "starter placeholder app is written."
        )
    )


@app.exception_handler(core.StreamctlError)
def streamctl_error(request: Request, exc: core.StreamctlError) -> JSONResponse:
    """Map streamctl operational errors to HTTP 400 with the raw message.

    Common causes: bad name, name already exists, source missing, health check
    failed (boots the journalctl -u streamlit@<name> -n 50 hint).
    """
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get(
    "/healthz",
    summary="Liveness probe",
    description=(
        "Always returns `{\"ok\": true}`. Used by tunnel health checks and the "
        "gsictl watchdog. No auth, never changes."
    ),
)
def healthz() -> dict:
    return {"ok": True}


@app.get(
    "/apps",
    summary="List the fleet",
    description=(
        "Full fleet status: every registered app with its port, source, public URL, "
        "systemd unit, and live health probe result.\n\n"
        "Health is probed at request time against `http://127.0.0.1:<port>` using the "
        "app's configured HEALTH_PATH; `ok` is true when the probe returns an OK code "
        "(200|204 by default).\n\n"
        "**This route is public** (no auth) - it exposes app names/ports/source paths "
        "only, and is reachable via the public tunnel. Do NOT add secrets-bearing "
        "fields to the status payload without enabling auth for it."
    ),
    response_description="List of app rows (fleet registry + live health).",
)
def apps() -> list[dict]:
    """Return core.status(CONF) - the fleet registry joined with live health probes."""
    return core.status(CONF)


APPS_EXAMPLE = [
    {
        "name": "tunneltest",
        "port": "8502",
        "source": "/tmp/appsrc-r780badk",
        "public_url": "-",
        "unit": "streamlit@tunneltest.service",
        "health": "200",
        "ok": True,
    }
]


@app.get(
    "/apps/{name}",
    summary="Get one app",
    description=(
        "One app row by name. Same fields as the /apps list elements.\n\n"
        "`public_url` reads `-` (placeholder) when `STREAMCTL_DOMAIN` is unset - it "
        "means no automatic URL bookkeeping, not necessarily that the app is private "
        "(a manually-tunneled app can still be public)."
    ),
    response_description="App row or 404.",
    responses={
        404: {
            "description": "Unknown app name (not in /etc/streamctl/apps.tsv)",
            "content": {
                "application/json": {
                    "example": {"detail": "unknown app: nope"},
                }
            },
        }
    },
)
def one(name: str) -> dict:
    try:
        return core.get_app(CONF, name)
    except core.StreamctlError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


CREATE_RESPONSE_EXAMPLE = {
    "name": "tunneltest",
    "port": "8502",
    "dir": "/home/dev/streamlit-apps/tunneltest",
    "unit": "streamlit@tunneltest.service",
    "public_url": "-",
}


@app.post(
    "/apps",
    summary="Create + boot a new app",
    description=(
        "Full lifecycle in one synchronous call:\n"
        "1. Validate name (DNS-safe) and uniqueness (already exists -> 400).\n"
        "2. Materialize source: git shallow-clone (source is a URL) or copytree "
        "(source is a dir). Dir-exists-but-unregistered -> 400 (destroy first).\n"
        "3. Write starter app.py if the source lacks the app file.\n"
        "4. Pick the next free port (8502-8599).\n"
        "5. `uv venv .venv` + pip install (requirements.txt if present, else streamlit).\n"
        "6. Write STREAMCTL_PORT to ports/<name>, daemon-reload, "
        "`systemctl enable --now streamlit@<name>.service`.\n"
        "7. Health-wait up to STREAMCTL_HEALTH_WAIT (60s) for 200.\n"
        "8. Register in /etc/streamctl/apps.tsv + gsictl registry.\n\n"
        "**On step 7 failure the unit is auto-disabled** and a 400 carries the journalctl "
        "hint. No partial registration survives.\n\n"
        "curl example:\n"
        "```bash\n"
        "TOKEN=...  # STREAMCTL_API_TOKEN from /etc/streamctl/streamctl.conf\n"
        "curl -s -X POST https://streamlit.internal.goodshepherdinsights.com/apps \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        "  -d '{\"name\":\"myapp\",\"source\":\"https://github.com/org/my-app.git\"}'\n"
        "```\n"
        "Local variant: base URL http://127.0.0.1:8510, same body."
    ),
    response_description="The created app row (port, dir, unit, public_url).",
    responses={
        400: {
            "description": "Bad name / already exists / source problem / health-wait timeout",
            "content": {
                "application/json": {
                    "example": {"error": "bad name (must match [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?): My App"},
                }
            },
        },
        401: {
            "description": "Missing/wrong bearer token (mutating route)",
            "content": {"application/json": {"example": {"detail": "bad or missing bearer token"}}},
        },
    },
    status_code=200,
)
def create_app(body: CreateAppBody, _: None = Depends(auth)) -> dict:
    return core.create(CONF, str(body.name), str(body.source))


@app.post(
    "/apps/{name}/deploy",
    summary="Deploy (redeploy/restart) an existing app",
    description=(
        "Redeploys the app's current source into its running unit - pulls the "
        "installed code forward (re-install of requirements if changed) and restarts "
        "the systemd unit. Used after editing files, upstream git changes, or by the "
        "watch timer (STREAMCTL_WATCH_INTERVAL) for git-HEAD moved cases.\n\n"
        "Same unit, same port, same public URL - an in-place refresh.\n\n"
        "curl example:\n"
        "```bash\n"
        "TOKEN=...\n"
        "curl -s -X POST https://streamlit.internal.goodshepherdinsights.com/apps/myapp/deploy \\\n"
        "  -H \"Authorization: Bearer $TOKEN\"\n"
        "```"
    ),
    response_description="Deployed app row (as create).",
    responses={
        404: {"description": "Unknown app"},
        400: {"description": "Deploy failed (build/install/health) - error carries the cause"},
        401: {"description": "Bad/missing bearer token"},
    },
)
def deploy_app(name: str, _: None = Depends(auth)) -> dict:
    return core.deploy(CONF, name)


@app.delete(
    "/apps/{name}",
    summary="Destroy an app",
    description=(
        "Full teardown, verified reverse order:\n"
        "1. `systemctl disable --now streamlit@<name>.service` (stops + disables).\n"
        "2. Remove from gsictl registry.\n"
        "3. `rm -rf /home/dev/streamlit-apps/<name>` (the deployed copy incl. venv).\n"
        "4. Delete the port file.\n"
        "5. Drop the row from apps.tsv.\n\n"
        "**The SOURCE directory is never touched** (local source dir or git origin survives "
        "the destroy - only the deployed copy dies). Port is freed for the next create.\n\n"
        "curl example:\n"
        "```bash\n"
        "TOKEN=...\n"
        "curl -s -X DELETE https://streamlit.internal.goodshepherdinsights.com/apps/myapp \\\n"
        "  -H \"Authorization: Bearer $TOKEN\"\n"
        "```\n"
        "Success response: `{\"name\": \"myapp\", \"destroyed\": true}`."
    ),
    response_description="Confirmation object.",
    responses={
        200: {
            "description": "Destroyed",
            "content": {"application/json": {"example": {"name": "myapp", "destroyed": True}}},
        },
        404: {"description": "Unknown app"},
        401: {"description": "Bad/missing bearer token"},
    },
)
def destroy_app(name: str, _: None = Depends(auth)) -> dict:
    return core.destroy(CONF, name)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(CONF["STREAMCTL_API_PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
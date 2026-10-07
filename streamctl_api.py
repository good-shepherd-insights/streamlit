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

API_DESCRIPTION = (
    "## streamctl API (self-host backend)\n\n"
    "Fleet controller for **Streamlit apps running on the host machine** - one\n"
    "systemd unit per app, exposed via Cloudflare tunnel when configured.\n\n"
    "---\n\n"
    "## Two backends - this is the SELF-HOST one\n\n"
    "|  | SELF-HOST (**this API**) | CLOUDFLARE (skill `streamctl-containers`) |\n"
    "|---|---|---|\n"
    "| Compute | local streamlit processes | CF Workers/Containers at the edge |\n"
    "| Endpoint | `http://127.0.0.1:8510` or tunnel URL | the CF worker router URL |\n"
    "| Request | `{name, source}` plain JSON | HMAC-signed intent w/ `repo` |\n"
    "| Auth | `Bearer <token>` header | HMAC-SHA256 `mac` in body |\n"
    "| Lifecycle | systemd boot + 60s health-wait | docker build + wrangler deploy |\n"
    "| Public URL | `{name}.{STREAMCTL_DOMAIN}` if set; `-` if not | `{name}.{PUBLIC_DOMAIN}` always |\n\n"
    "Same *verbs*, different schemas - don't send a CF intent to this API or vice versa.\n\n"
    "---\n\n"
    "## Auth\n"
    "Mutating routes (create / deploy / destroy) require:\n\n"
    "```\n"
    "Authorization: Bearer <STREAMCTL_API_TOKEN>\n"
    "```\n\n"
    "The token lives in the conf file on the host:\n"
    "```bash\n"
    "TOKEN=$(grep STREAMCTL_API_TOKEN <conf-path> | cut -d= -f2)\n"
    "```\n\n"
    "Or click **Authorize** (top right) and paste it once - Swagger then attaches it\n"
    "to every request, surviving page reloads.\n\n"
    "GET routes (/healthz, /apps, /apps/{name}) are open by design: app names/ports,\n"
    "no secrets.\n\n"
    "---\n\n"
    "## Examples\n\n"
    "### Create from a LOCAL directory\n"
    "```bash\n"
    "TOKEN=$(grep STREAMCTL_API_TOKEN <conf-path> | cut -d= -f2)\n"
    "curl -s -X POST http://127.0.0.1:8510/apps \\\n"
    "  -H \"Authorization: Bearer $TOKEN\" \\\n"
    "  -H \"Content-Type: application/json\" \\\n"
    "  -d '{\"name\":\"myapp\",\"source\":\"/home/me/src/myapp\"}'\n"
    "```\n\n"
    "### Create from a GIT URL\n"
    "```bash\n"
    "curl -s -X POST https://<tunnel-url>/apps \\\n"
    "  -H \"Authorization: Bearer $TOKEN\" \\\n"
    "  -H \"Content-Type: application/json\" \\\n"
    "  -d '{\"name\":\"shopapp\",\"source\":\"https://github.com/org/shop-app.git\"}'\n"
    "```\n"
    "Response (200, app LIVE when returned):\n"
    "```json\n"
    "{\"name\":\"myapp\",\"port\":\"8502\",\"dir\":\"/.../myapp\",\"unit\":\"streamlit@myapp.service\",\"public_url\":\"-\"}\n"
    "```\n\n"
    "### List the fleet (no auth)\n"
    "```bash\n"
    "curl -s http://127.0.0.1:8510/apps\n"
    "```\n"
    "```json\n"
    "[{\"name\":\"myapp\",\"port\":\"8502\",\"source\":\"...\",\"public_url\":\"-\",\"unit\":\"streamlit@myapp.service\",\"health\":\"200\",\"ok\":true}]\n"
    "```\n\n"
    "### Get one app\n"
    "```bash\n"
    "curl -s http://127.0.0.1:8510/apps/myapp\n"
    "```\n\n"
    "### Redeploy (refresh in place; after code edits or upstream git change)\n"
    "```bash\n"
    "curl -s -X POST https://<tunnel-url>/apps/myapp/deploy -H \"Authorization: Bearer $TOKEN\"\n"
    "```\n\n"
    "### Destroy (source code NEVER touched - only the deployed copy)\n"
    "```bash\n"
    "curl -s -X DELETE https://<tunnel-url>/apps/myapp -H \"Authorization: Bearer $TOKEN\"\n"
    "# \"name\":\"myapp\",\"destroyed\":true\n"
    "```\n\n"
    "### Full round-trip (Python)\n"
    "```python\n"
    "import requests\n"
    "BASE = \"http://127.0.0.1:8510\"   # or the tunnel URL\n"
    "H = {\"Authorization\": \"Bearer <TOKEN>\"}\n\n"
    "# create (LIVE on return)\n"
    "app = requests.post(f\"{BASE}/apps\", json={\"name\": \"myapp\",\"source\": \"https://github.com/org/shop-app.git\"}, headers=H).json()\n"
    "print(app)   # {name, port, dir, unit, public_url}\n\n"
    "# list fleet\n"
    "print(requests.get(f\"{BASE}/apps\").json())\n\n"
    "# redeploy after edits\n"
    "requests.post(f\"{BASE}/apps/myapp/deploy\", headers=H)\n\n"
    "# destroy\n"
    "requests.delete(f\"{BASE}/apps/myapp\", headers=H)\n"
    "```\n\n"
    "---\n\n"
    "## Common errors (exact body strings)\n\n"
    "| HTTP | Body | Meaning |\n|---|---|---|\n"
    "| 400 | `bad name (must match [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?): My App` | name not DNS-safe |\n"
    "| 400 | `already exists: myapp` | app already created |\n"
    "| 400 | `source not a directory or git url: ...` | bad source path/URL |\n"
    "| 400 | `directory exists but not registered: ... (destroy it first)` | stale dir, not in fleet |\n"
    "| 400 | `health check failed for myapp after <wait>s; unit disabled. Logs: journalctl -u streamlit@myapp -n 50` | app crashed at boot |\n"
    "| 401 | `bad or missing bearer token` | wrong/no token on a mutating call |\n"
    "| 404 | `unknown app: nope` | name not in the fleet |\n"
    "| 422 | `Field required` | missing required body field |"
)


CONF = core.load_conf()

# Public URL (from conf) for servers list
PUBLIC_URL = (CONF.get("STREAMCTL_PUBLIC_URL") or "").rstrip("/")
SERVERS = ([{"url": "http://127.0.0.1:8510", "description": "Local (host machine only)"}]
          + ([{"url": PUBLIC_URL, "description": "Public (Cloudflare tunnel)"}] if PUBLIC_URL else []))

app = FastAPI(
    title="streamctl API (self-host)",
    version="1.0",
    description=API_DESCRIPTION,
    servers=SERVERS,
    openapi_tags=[
        {"name": "Overview",
         "description": "Health and fleet discovery - start here. No auth required."},
        {"name": "Lifecycle",
         "description": "Create, deploy, destroy apps. Bearer auth required - click Authorize and paste the token, then Try-it-out carries it automatically."},
    ],
    # Swagger UI behavior upgrades:
    swagger_ui_parameters={
        "persistAuthorization": True,   # token survives page reloads
        "displayRequestDuration": True, # show latency per call
        "docExpansion": "none",         # collapsed by default: clean scan first
        "filter": True,                 # search box in the top bar
        "tryItOutEnabled": False,       # explicit Try-it-out click - no accidental fires
    },
)

# ---- Security scheme: enables the "Authorize" button in Swagger UI ----
def _openapi_with_security():
    """Augment the default schema; everything route-level from decorators stays."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description,
        routes=app.routes, tags=app.openapi_tags)
    scheme_name = "BearerAuth"
    schema["components"]["securitySchemes"] = {
        scheme_name: {
            "type": "http", "scheme": "bearer",
            "description": ("Paste the STREAMCTL_API_TOKEN (plain token, no Bearer prefix). "
                            "Swagger attaches it to every request, surviving reloads."),
        }
    }
    schema["security"] = [{scheme_name: []}]   # default=auth required; GET ops override with security=[]
    schema["servers"] = SERVERS
    # Request-body examples for POST /apps (route-level so they show in Swagger's "Example Value")
    post_apps = schema["paths"].get("/apps", {}).get("post")
    if post_apps:
        post_apps["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/CreateAppBody"},
                    "examples": {
                        "local-dir": {
                            "summary": "Local directory source",
                            "value": {"name": "myapp", "source": "/home/me/src/myapp"},
                        },
                        "git-url": {
                            "summary": "Git URL source",
                            "value": {"name": "shopapp", "source": "https://github.com/org/shop-app.git"},
                        },
                    },
                }
            },
        }
    app.openapi_schema = schema
    return schema

app.openapi = _openapi_with_security

# Servers: localhost + public tunnel - selector in Swagger UI top bar
def _servers():
    base = (CONF.get("STREAMCTL_PUBLIC_URL") or "").rstrip("/")
    servers = [{"url": "http://127.0.0.1:8510", "description": "Local (host machine only)"}]
    if base:
        servers.append({"url": base, "description": "Public (Cloudflare tunnel)"})
    return servers

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
                {"name": "myapp", "source": "/path/to/your/app"},
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
        default="",
        description=(
            "Where the app code comes from: an absolute directory containing app.py "
            "(copied) or a git URL (shallow-cloned). If the source lacks app.py a "
            "starter placeholder app is written. Empty or missing -> 400 "
            "`source not a directory or git url`."
        ),
        examples=["/home/me/src/myapp", "https://github.com/org/shop-app.git"],
    )


@app.exception_handler(core.StreamctlError)
def streamctl_error(request: Request, exc: core.StreamctlError) -> JSONResponse:
    """Map streamctl operational errors to HTTP 400 with the raw message.

    Common causes: bad name, name already exists, source missing, health check
    failed (boots the unit log hint).
    """
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get(
    "/healthz",
    tags=["Overview"],
    openapi_extra={"security": []},
    summary="Liveness probe",
    description=(
        "Always returns `{\"ok\": true}`. Used by tunnel/monitoring health checks. No auth, never changes."
    ),
)
def healthz() -> dict:
    return {"ok": True}


@app.get(
    "/apps",
    tags=["Overview"],
    summary="List the entire fleet (no auth) - START HERE",
    openapi_extra={"security": []},
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
    responses={
        200: {
            "description": "The fleet",
            "content": {"application/json": {"example": [
                {"name": "myapp", "port": "8502", "source": "https://github.com/org/shop-app.git",
                 "public_url": "-", "unit": "streamlit@myapp.service", "health": "200", "ok": True},
                {"name": "demodash", "port": "8503", "source": "/home/me/src/demo",
                 "public_url": "-", "unit": "streamlit@demodash.service", "health": "200", "ok": True},
            ]}},
        }
    },
)
def apps() -> list[dict]:
    """Return core.status(CONF) - the fleet registry joined with live health probes."""
    return core.status(CONF)


APPS_EXAMPLE = [
    {
        "name": "tunneltest",
        "port": "8502",
        "source": "<local-or-git-source>",
        "public_url": "-",
        "unit": "streamlit@myapp.service",
        "health": "200",
        "ok": True,
    }
]


@app.get(
    "/apps/{name}",
    tags=["Overview"],
    summary="Get one app (no auth)",
    openapi_extra={"security": []},
    description=(
        "One app row by name. Same fields as the /apps list elements.\n\n"
        "`public_url` reads `-` (placeholder) when `STREAMCTL_DOMAIN` is unset - it "
        "means no automatic URL bookkeeping, not necessarily that the app is private "
        "(a manually-tunneled app can still be public)."
    ),
    response_description="App row or 404.",
    responses={
        200: {
            "description": "The app",
            "content": {"application/json": {"example": {
                "name": "myapp", "port": "8502", "source": "https://github.com/org/shop-app.git",
                "public_url": "-", "unit": "streamlit@myapp.service", "health": "200", "ok": True}}},
        },
        404: {
            "description": "Unknown app name",
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
    "dir": "<app-dir>",
    "unit": "streamlit@myapp.service",
    "public_url": "-",
}


@app.post(
    "/apps",
    tags=["Lifecycle"],
    summary="Create + boot a new app (Bearer auth)",
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
        "8. Register in the fleet registry.\n\n"
        "**On step 7 failure the unit is auto-disabled** and a 400 carries the unit log "
        "hint. No partial registration survives.\n\n"
        "### curl - LOCAL base (http://127.0.0.1:8510)\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X POST http://127.0.0.1:8510/apps \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        "  -d '{\"name\":\"myapp\",\"source\":\"/home/me/src/myapp\"}'\n"
        "```\n"
        "### curl - PUBLIC tunnel base (self-host URL)\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X POST https://<selfhost-tunnel-url>/apps \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        "  -d '{\"name\":\"myapp\",\"source\":\"https://github.com/org/my-app.git\"}'\n"
        "```\n"
        "### Python\n"
        "```python\n"
        "import requests\n"
        "app = requests.post(\"http://127.0.0.1:8510/apps\",\n"
        "                    json={\"name\": \"myapp\",\n"
        "                          \"source\": \"https://github.com/org/my-app.git\"},\n"
        "                    headers={\"Authorization\": \"Bearer <TOKEN>\"}).json()\n"
        "print(app)  # {name, port, dir, unit, public_url} - LIVE now\n"
        "```"
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
    tags=["Lifecycle"],
    summary="Deploy (redeploy/restart) an existing app (Bearer auth)",
    description=(
        "Redeploys the app's current source into its running unit - pulls the "
        "installed code forward (re-install of requirements if changed) and restarts "
        "the systemd unit. Used after editing files, upstream git changes, or by the "
        "watch timer (STREAMCTL_WATCH_INTERVAL) for git-HEAD moved cases.\n\n"
        "Same unit, same port, same public URL - an in-place refresh.\n\n"
        "### curl - LOCAL\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X POST http://127.0.0.1:8510/apps/myapp/deploy \\\n"
        "  -H \"Authorization: Bearer $TOKEN\"\n"
        "```\n"
        "### curl - PUBLIC\n"
        "```bash\n"
        "curl -s -X POST https://<selfhost-tunnel-url>/apps/myapp/deploy \\\n"
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
    tags=["Lifecycle"],
    summary="Destroy an app (Bearer auth)",
    description=(
        "Full teardown, verified reverse order:\n"
        "1. `systemctl disable --now streamlit@<name>.service` (stops + disables).\n"
        "2. Deregister from the auxiliary registry.\n"
        "3. Delete the app directory (the deployed copy incl. venv).\n"
        "4. Delete the port file.\n"
        "5. Drop the fleet registry row.\n\n"
        "**The SOURCE directory is never touched** (local source dir or git origin survives "
        "the destroy - only the deployed copy dies). Port is freed for the next create.\n\n"
        "### curl - LOCAL\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X DELETE http://127.0.0.1:8510/apps/myapp \\\n"
        "  -H \"Authorization: Bearer $TOKEN\"\n"
        "```\n"
        "### curl - PUBLIC\n"
        "```bash\n"
        "curl -s -X DELETE https://<selfhost-tunnel-url>/apps/myapp \\\n"
        "  -H \"Authorization: Bearer $TOKEN\"\n"
        "```"
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
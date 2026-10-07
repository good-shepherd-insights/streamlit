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
    title="streamctl API (self-host)",
    version="1.0",
    description=(
        "## streamctl API - SELF-HOST Streamlit fleet controller\n\n"
        "This is the **self-host backend**: apps run as streamlit processes on the\n"
        "host machine, one systemd unit per app, exposed publicly (optionally) via\n"
        "a Cloudflare tunnel.\n\n"
        "**NOT the Cloudflare Containers API** (that skill: `streamctl-containers`,\n"
        "apps run on CF's edge as Workers/Containers). Same verbs, different schema:\n\n"
        "| | SELF-HOST (this API) | CLOUDFLARE (streamctl-containers) |\n"
        "|---|---|---|\n"
        "| Compute | host machine (streamlit processes) | Cloudflare Containers/Workers |\n"
        "| Endpoint | local port or the public tunnel URL | CF worker router URL |\n"
        "| Request shape | `{name, source}` plain JSON | HMAC-signed intent w/ `repo` |\n"
        "| Auth | `Authorization: Bearer <token>` | HMAC-SHA256 `mac` in body |\n"
        "| Lifecycle | systemd boot + health-wait 60s | docker build + wrangler push + deploy |\n"
        "| Public URL | `{name}.{STREAMCTL_DOMAIN}` if set; `-` otherwise | `{name}.{PUBLIC_DOMAIN}` always |\n\n"
        "## Auth\n\n"
        "Mutating routes (create/deploy/destroy) require:\n"
        "```\n"
        "Authorization: Bearer <STREAMCTL_API_TOKEN>\n"
        "```\n"
        "Read the token from the conf file on the host:\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN <conf-path> | cut -d= -f2)\n"
        "```\n"
        "GET routes are open by design - app names/ports/source paths only, no secrets.\n\n"
        "## EXAMPLES\n\n"
        "Complete curl examples live on each endpoint (click one in the sidebar).\n"
        "Python end-to-end:\n"
        "```python\n"
        "import requests\n"
        "BASE = \"http://127.0.0.1:8510\"          # or the public tunnel URL\n"
        "H = {\"Authorization\": \"Bearer <TOKEN>\"}\n"
        "\n"
        "# create (LIVE when it returns)\n"
        "app = requests.post(f\"{BASE}/apps\",\n"
        "                    json={\"name\": \"myapp\",\n"
        "                          \"source\": \"https://github.com/org/shop-app.git\"},\n"
        "                    headers=H).json()\n"
        "print(app)   # {name, port, dir, unit, public_url}\n"
        "\n"
        "# list fleet (no auth)\n"
        "print(requests.get(f\"{BASE}/apps\").json())\n"
        "\n"
        "# redeploy in place after code changes\n"
        "requests.post(f\"{BASE}/apps/myapp/deploy\", headers=H)\n"
        "\n"
        "# destroy (source code NOT touched - only the deployed copy)\n"
        "requests.delete(f\"{BASE}/apps/myapp\", headers=H)\n"
        "```\n\n"
        "## Common errors (exact body strings)\n\n"
        "| HTTP | Body | Meaning |\n"
        "|---|---|---|\n"
        "| 400 | `bad name (must match [a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?): My App` | name not DNS-safe |\n"
        "| 400 | `already exists: myapp` | app already created |\n"
        "| 400 | `source not a directory or git url: ...` | bad source path/URL |\n"
        "| 400 | `directory exists but not registered: ... (destroy it first)` | stale dir, not in fleet |\n"
        "| 400 | `health check failed for myapp after 60s; unit disabled. Logs: unit-logs (journalctl -u <unit> -n 50)` | app crashed at boot |\n"
        "| 401 | `bad or missing bearer token` | wrong/no token on a mutating call |\n"
        "| 404 | `unknown app: nope` | name not in the fleet |"
    ),
)

CONF = core.load_conf()


def _route_audit(request: Request, action: str, app: str, result: str, **extra: object) -> None:
    """Business-action audit line (distinct from the generic access middleware):
    {ts, ip, action, app, result, error?}. Never raises."""
    core.audit(CONF, {
        "kind": "action",
        "ip": request.client.host if request.client else "",
        "action": action,
        "app": app,
        "result": result,
        **extra,
    })


def auth(request: Request) -> None:
    token = CONF.get("STREAMCTL_API_TOKEN", "")
    if not token:
        return
    supplied = request.headers.get("Authorization", "")
    if supplied != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


def auth_check(request: Request) -> None:
    """auth() plus a route-level audit line on auth failure (per-route audit must
    cover validation/auth failures, not only successful calls)."""
    token = CONF.get("STREAMCTL_API_TOKEN", "")
    if token and request.headers.get("Authorization", "") != f"Bearer {token}":
        _route_audit(request, request.method, "-", "auth_failed")
    auth(request)


class CreateAppBody(BaseModel):
    """Request body for creating a new Streamlit app."""

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"name": "myapp", "source": "/path/to/your/app",
                 "domain": "marylandinsights.com"},
                {"name": "myapp", "source": "https://github.com/org/my-app.git",
                 "domain": "marylandinsights.com"},
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
    domain: str = Field(
        default="",
        description=(
            "Caller-chosen base domain for the public URL `<name>.<domain>`. The "
            "server validates the domain is a CF zone readable by its credentials, "
            "resolves zone/account/tunnel itself, wires DNS + tunnel ingress, and "
            "verifies real rendered content at the public URL before responding. "
            "Empty = private app (public_url `-`). Unservable domain -> 400."
        ),
        examples=["marylandinsights.com"],
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
    summary="Liveness probe",
    description=(
        "Always returns `{\"ok\": true}`. Used by tunnel/monitoring health checks. No auth, never changes."
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
    """Unified fleet: self-host rows (core.status) + CF Containers rows from the
    router, tagged backend=selfhost|cloudflare."""
    rows: list[dict] = []
    for r in core.status(CONF):
        r["backend"] = "selfhost"
        rows.append(r)
    for r in _router_fleet():
        r["backend"] = "cloudflare"
        if not any(x.get("name") == r.get("name") and x.get("backend") == "cloudflare" for x in rows):
            rows.append(r)
    return rows


def _router_fleet() -> list[dict]:
    """CF Containers fleet from the local router intent store (mirror of the KV
    rows; the deployed Worker is the source of truth and this is its twin).
    Failures are surfaced as one cloudflare row with health='router-error',
    never swallowed."""
    import json as _json
    import sqlite3 as _sq

    db = CONF.get("STREAMCTL_ROUTER_DB") or "/var/lib/streamctl/router-intents.sqlite3"
    try:
        con = _sq.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT id, status, intent, hostname, failed_reason, repo, port "
            "FROM intents ORDER BY rowid DESC"
        ).fetchall()
        con.close()
    except Exception as e:
        return [{"name": "-", "port": "-", "source": "-", "public_url": "-",
                 "unit": "-", "health": f"router-error: {e}", "ok": False}]
    out: list[dict] = []
    for _id, status, raw, hostname, failed_reason, repo, port in rows:
        try:
            intent = _json.loads(raw) if raw else {}
        except Exception:
            intent = {}
        out.append({
            "name": intent.get("app") or _id,
            "port": port or "-",
            "source": repo or intent.get("repo") or "-",
            "public_url": f"https://{hostname}" if hostname else "-",
            "unit": f"container:{intent.get('target') or 'container'}",
            "health": status if status != "failed" else f"failed: {failed_reason or '?'}",
            "ok": status == "done",
        })
    return out


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
    summary="Get one app",
    description=(
        "One app row by name. Same fields as the /apps list elements.\n\n"
        "`public_url` reads `-` when the app was created without a `domain` - it "
        "means the app is private (no automatic URL was wired at create time)."
    ),
    response_description="App row or 404.",
    responses={
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
        "8. Register in the fleet registry.\n"
        "9. If `domain` was supplied: wire DNS + tunnel ingress for `<name>.<domain>`, "
        "then verify real rendered content at the public URL - the call returns 200 only "
        "after the URL serves an actual app, otherwise the app is torn down and 400 "
        "carries the cause.\n\n"
        "**On health or public-verify failure the unit is auto-disabled** and the 400 "
        "carries the cause. No partial registration survives.\n\n"
        "### curl - LOCAL base (http://127.0.0.1:8510)\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X POST http://127.0.0.1:8510/apps \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        "  -d '{\"name\":\"myapp\",\"source\":\"/home/me/src/myapp\",\"domain\":\"marylandinsights.com\"}'\n"
        "```\n"
        "### curl - PUBLIC tunnel base (self-host URL)\n"
        "```bash\n"
        "TOKEN=$(grep STREAMCTL_API_TOKEN conf-path | cut -d= -f2)\n"
        "curl -s -X POST https://<selfhost-tunnel-url>/apps \\\n"
        "  -H \"Authorization: Bearer $TOKEN\" \\\n"
        "  -H \"Content-Type: application/json\" \\\n"
        "  -d '{\"name\":\"myapp\",\"source\":\"https://github.com/org/my-app.git\",\"domain\":\"marylandinsights.com\"}'\n"
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
def create_app(body: CreateAppBody, request: Request, _: None = Depends(auth_check)) -> dict:
    _route_audit(request, "create", body.name, "accepted", domain=body.domain)
    try:
        result = core.create(CONF, str(body.name), str(body.source), domain=str(body.domain))
    except core.StreamctlError as e:
        _route_audit(request, "create", body.name, "rejected", error=str(e))
        raise
    _route_audit(request, "create", body.name, "done", public_url=result.get("public_url"))
    return result


@app.post(
    "/apps/{name}/deploy",
    summary="Deploy (redeploy/restart) an existing app",
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
def deploy_app(name: str, request: Request, _: None = Depends(auth_check)) -> dict:
    _route_audit(request, "deploy", name, "accepted")
    try:
        result = core.deploy(CONF, name)
    except core.StreamctlError as e:
        _route_audit(request, "deploy", name, "rejected", error=str(e))
        raise
    except HTTPException as e:
        _route_audit(request, "deploy", name, "rejected", error=str(e.detail))
        raise
    _route_audit(request, "deploy", name, "done")
    return result


@app.delete(
    "/apps/{name}",
    summary="Destroy an app",
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
def destroy_app(name: str, request: Request, _: None = Depends(auth_check)) -> dict:
    _route_audit(request, "destroy", name, "accepted")
    try:
        result = core.destroy(CONF, name)
    except core.StreamctlError as e:
        _route_audit(request, "destroy", name, "rejected", error=str(e))
        raise
    except HTTPException as e:
        _route_audit(request, "destroy", name, "rejected", error=str(e.detail))
        raise
    _route_audit(request, "destroy", name, "done")
    return result


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(CONF["STREAMCTL_API_PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
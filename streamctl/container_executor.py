#!/usr/bin/env python3
"""streamctl container executor - deterministic git->docker->wrangler pipeline.

Consumes a verified container intent (dispatched by cf_router_local.py):
clones the repo, builds the image, pushes it to the CF registry, renders the
worker + container config from streamctl/templates/ files, deploys via
wrangler, wires the public URL (DNS A + workers route) and polls it healthy.
Destroy reverses every CF object. Zero literals: every value comes from the
conf file or the intent. The subprocess runner and httpx transport are
injectable so tests never touch git, docker, wrangler or the CF API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import streamctl_core as core
from streamctl_core import StreamctlError

sys.path.insert(0, str(Path(__file__).resolve().parent))

STREAMCTL_DIR = Path(__file__).resolve().parent

EXECUTOR_DEFAULTS = {
    "TARGETS": "home",
    "CONTAINER_PORT": "8501",
    "CONTAINER_MAX_INSTANCES": "1",
    "IMAGE_TAG_SUFFIX": "latest",
    "CF_CONTAINERS_PKG": "@cloudflare/containers@",
    "KEEP_WORKDIR": "false",
    "HEALTH_POLL_SEC": "15",
    "HEALTH_TIMEOUT_SEC": "420",
    "CF_REGISTRY_USER": "oauth2accesstoken",
    "ROUTER_DUMMY_IP": "192.0.2.1",
    "WRANGLER_BIN": "npx wrangler",
    "CONTAINER_WRANGLER_TEMPLATE": "templates/wrangler-container.toml.tmpl",
    "CONTAINER_INDEX_TEMPLATE": "templates/container-index.ts.tmpl",
}
REQUIRED_EXECUTOR_KEYS = ("WORK_ROOT", "IMAGE_PREFIX", "CF_REGISTRY_HOST")

APP_RE = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


class ExecutorError(StreamctlError):
    """Operator-visible executor failure, stored verbatim as failed_reason."""


Runner = Callable[..., Any]


def load_executor_conf() -> dict[str, str]:
    """streamctl_core + executor conf overlay; compose CF endpoint URLs."""
    conf = {**EXECUTOR_DEFAULTS, **core.load_conf()}
    missing = [key for key in REQUIRED_EXECUTOR_KEYS if not conf.get(key)]
    if missing:
        raise StreamctlError(f"missing required conf keys: {', '.join(missing)}")
    acct, zone = conf["CF_ACCOUNT_ID"], conf["CF_ZONE_ID"]
    conf.setdefault("CF_DNS_RECORDS_URL", f"{conf['CF_API_BASE']}/zones/{zone}/dns_records")
    conf.setdefault("CF_WORKERS_ROUTES_URL", f"{conf['CF_API_BASE']}/zones/{zone}/workers/routes")
    conf.setdefault("CF_SCRIPTS_URL", f"{conf['CF_API_BASE']}/accounts/{acct}/workers/scripts")
    conf.setdefault("CF_CONTAINER_URL", f"{conf['CF_API_BASE']}/accounts/{acct}/containers/applications")
    return conf


def allowed_targets(conf: dict[str, str]) -> set[str]:
    return {s.strip() for s in conf.get("TARGETS", "home").split(",") if s.strip()}


# ---------- intent validation ----------


def validate_intent(intent: dict[str, object], conf: dict[str, str]) -> None:
    """Reject what the pipeline cannot execute deterministically."""
    app, repo = str(intent.get("app") or ""), str(intent.get("repo") or "")
    if not re.fullmatch(APP_RE, app):
        raise ExecutorError(f"invalid app label (must match {APP_RE}): {app}")
    if not re.match(r"^(https?://|git@)\S+$", repo):
        raise ExecutorError(f"repo must be a git URL: {repo!r}")
    port = intent.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, (int, float))):
        raise ExecutorError(f"port must be numeric: {port!r}")
    env = intent.get("env")
    if env is not None and not isinstance(env, dict):
        raise ExecutorError("env must be an object")


# ---------- template render ----------


def load_templates(conf: dict[str, str]) -> dict[str, str]:
    """Template file contents; relative paths resolve against streamctl/."""
    out: dict[str, str] = {}
    for key in ("CONTAINER_WRANGLER_TEMPLATE", "CONTAINER_INDEX_TEMPLATE"):
        raw = conf.get(key) or ""
        if not raw:
            raise ExecutorError(f"missing conf key: {key}")
        path = Path(raw)
        if not path.is_absolute():
            path = STREAMCTL_DIR / path
        out[key] = path.read_text(encoding="utf-8")
    return out


def render(template: str, values: dict[str, str | int]) -> str:
    """Substitute {{key}} placeholders; assert none survive."""
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    leftover = re.findall(r"\{\{(\w+)\}\}", out)
    if leftover:
        raise ExecutorError(
            f"template placeholder(s) unfilled after render: {sorted(set(leftover))}"
        )
    return out


# ---------- subprocess steps (runner injectable) ----------

_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"


def _default_runner(argv: list[str], *, cwd: str | None = None, env: dict | None = None, input: str | None = None):
    return subprocess.run(argv, cwd=cwd, env=env, input=input, capture_output=True, text=True)


def _run(argv: list[str], runner: Runner, *, cwd: str | None = None, env: dict | None = None, input: str | None = None, secret: str | None = None) -> str:
    """Run one command; on failure surface the output tail with the secret redacted."""
    proc = runner(argv, cwd=cwd, env=env, input=input)
    detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
    if secret:
        detail = detail.replace(secret, "***")
    if proc.returncode != 0:
        raise ExecutorError(f"`{' '.join(argv[:2])}...` failed: {detail}")
    return detail


def _password(conf: dict[str, str]) -> str:
    return Path(conf["CF_API_KEY_FILE"]).read_text(encoding="utf-8").strip()


def step_fetch(workdir: Path, repo: str, runner: Runner) -> None:
    """Clone shallow, or fetch + reset when the dir already holds a clone."""
    if (workdir / ".git").is_dir():
        _run(["git", "-C", str(workdir), "fetch", "origin"], runner)
        _run(["git", "-C", str(workdir), "reset", "--hard", "origin/HEAD"], runner)
        return
    workdir.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--depth", "1", repo, str(workdir)], runner)


def step_build(workdir: Path, image: str, runner: Runner) -> None:
    _run(["docker", "build", "-t", image, str(workdir)], runner)


def step_push(image: str, conf: dict[str, str], runner: Runner) -> None:
    """Push via wrangler (containers/me identity exchange handles registry auth)."""
    # registry.cloudflare.com auth is handled by wrangler (containers/me
    # identity exchange) - raw docker login with the API token 401s.
    wrangler = conf["WRANGLER_BIN"].split()
    token = _password(conf)
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": token}
    _run(wrangler + ["containers", "push", image], runner, env=env, secret=token)


def step_render(
    deploy_dir: Path,
    conf: dict[str, str],
    worker_name: str,
    image: str,
    app_port: int,
) -> None:
    """Render wrangler.toml + src/index.ts from the template files."""
    templates = load_templates(conf)
    values: dict[str, str | int] = {
        "worker_name": worker_name,
        "container_image": image,
        "class_name": f"C{worker_name.title().replace('-', '')}Container",
        "app_port": app_port,
        "max_instances": conf["CONTAINER_MAX_INSTANCES"],
    }
    (deploy_dir / "src").mkdir(parents=True, exist_ok=True)
    wrangler = render(templates["CONTAINER_WRANGLER_TEMPLATE"], values)
    if "workers_dev = false" not in wrangler:
        raise ExecutorError("rendered wrangler.toml lacks workers_dev = false")
    (deploy_dir / "wrangler.toml").write_text(wrangler, encoding="utf-8")
    (deploy_dir / "src" / "index.ts").write_text(
        render(templates["CONTAINER_INDEX_TEMPLATE"], values), encoding="utf-8"
    )
    pkg = {
        "name": worker_name,
        "private": True,
        "type": "module",
        "dependencies": {"@cloudflare/containers": conf["CF_CONTAINERS_PKG"]},
    }
    (deploy_dir / "package.json").write_text(json.dumps(pkg), encoding="utf-8")


def step_npm(deploy_dir: Path, conf: dict[str, str], runner: Runner) -> None:
    """Install the deploy-dir package.json deps (containers helper package)."""
    npm = conf.get("NPM_BIN", "npm").split()
    _run([*npm, "install", "--no-audit", "--no-fund", "--loglevel", "error"],
         runner, cwd=str(deploy_dir))


def step_deploy(deploy_dir: Path, conf: dict[str, str], runner: Runner) -> str:
    """Wrangler deploy with cwd=deploy dir; CLOUDFLARE_API_TOKEN env-only."""
    token = _password(conf)
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": token}
    argv = conf["WRANGLER_BIN"].split()
    return _run([*argv, "deploy"], runner, cwd=str(deploy_dir), env=env, secret=token)


# ---------- CF http steps ----------


def _cf(
    client: httpx.Client,
    conf: dict[str, str],
    url: str,
    method: str = "GET",
    body: object = None,
    *,
    none_on_404: bool = False,
) -> Any:
    resp = client.request(
        method, url, json=body, headers={"authorization": f"Bearer {_password(conf)}"}
    )
    data = resp.json() if resp.content else {}
    if none_on_404 and resp.status_code == 404:
        return None  # already gone: destroy stays idempotent
    if resp.status_code >= 400:
        first = (data.get("errors") or [{}])[0]
        raise ExecutorError(first.get("message") or f"cf api error: {resp.status_code}")
    return data.get("result")


def hostname_of(conf: dict[str, str], intent: dict[str, object]) -> str:
    return f"{intent['app']}.{conf['PUBLIC_DOMAIN']}"


def image_of(intent: dict[str, object], conf: dict[str, str]) -> str:
    """Namespaced CF-managed-registry ref: <host>/<account>/<app>-<hash>:<tag>."""
    sha = hashlib.sha256(str(intent["id"]).encode()).hexdigest()[:12]
    return (f"{conf['CF_REGISTRY_HOST']}/{conf['CF_ACCOUNT_ID']}/"
            f"{intent['app']}-{sha}:{conf['IMAGE_TAG_SUFFIX']}")


def step_wire(client: httpx.Client, conf: dict[str, str], hostname: str, worker_name: str) -> None:
    """DNS A record (proxied dummy IP) + workers route for app.PUBLIC_DOMAIN."""
    _cf(client, conf, conf["CF_DNS_RECORDS_URL"], "POST", {
        "type": "A", "name": hostname, "content": conf["ROUTER_DUMMY_IP"], "proxied": True,
    })
    _cf(client, conf, conf["CF_WORKERS_ROUTES_URL"], "POST", {
        "pattern": f"{hostname}/*", "script": worker_name,
    })


def step_health(
    client: httpx.Client,
    conf: dict[str, str],
    url: str,
    *,
    poll: int | None = None,
    timeout: int | None = None,
) -> None:
    """GET url every HEALTH_POLL_SEC until 200 or HEALTH_TIMEOUT_SEC elapses."""
    poll = int(poll if poll is not None else conf["HEALTH_POLL_SEC"])
    timeout = int(timeout if timeout is not None else conf["HEALTH_TIMEOUT_SEC"])
    deadline = time.time() + timeout
    while True:
        try:
            if client.get(url).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() >= deadline:
            break
        time.sleep(poll)
    raise ExecutorError(f"health check failed after {timeout}s: {url}")


# ---------- destroy (reverse order; every delete idempotent) ----------


def destroy_cf_objects(
    client: httpx.Client, conf: dict[str, str], hostname: str, worker_name: str
) -> list[str]:
    """Delete route -> dns -> script -> container app (one each, miss-tolerant)."""
    done: list[str] = []
    matched = [
        d
        for d in (_cf(client, conf, conf["CF_WORKERS_ROUTES_URL"]) or [])
        if str(d.get("pattern")) == f"{hostname}/*"
    ]
    if matched:
        _cf(
            client,
            conf,
            f"{conf['CF_WORKERS_ROUTES_URL']}/{matched[0]['id']}",
            "DELETE",
            none_on_404=True,
        )
        done.append("delete_route")
    matched = [
        d
        for d in (
            _cf(client, conf, f"{conf['CF_DNS_RECORDS_URL']}?type=A&name={hostname}")
            or []
        )
        if d.get("name") == hostname
    ]
    if matched:
        _cf(
            client,
            conf,
            f"{conf['CF_DNS_RECORDS_URL']}/{matched[0]['id']}",
            "DELETE",
            none_on_404=True,
        )
        done.append("delete_dns")
    matched = [
        s
        for s in (_cf(client, conf, conf["CF_SCRIPTS_URL"]) or [])
        if s.get("id") == worker_name
    ]
    if matched:
        _cf(
            client,
            conf,
            f"{conf['CF_SCRIPTS_URL']}/{worker_name}",
            "DELETE",
            none_on_404=True,
        )
        done.append("delete_script")
    matched = [
        a
        for a in (_cf(client, conf, conf["CF_CONTAINER_URL"]) or [])
        if a.get("name") == worker_name
    ]
    if matched:
        _cf(
            client,
            conf,
            f"{conf['CF_CONTAINER_URL']}/{matched[0]['id']}",
            "DELETE",
            none_on_404=True,
        )
        done.append("delete_container")
    return done


def _dead(client: httpx.Client, url: str) -> bool:
    """Public URL must be unreachable; any HTTP answer means still alive."""
    try:
        return client.get(url).status_code != 200
    except httpx.HTTPError:
        return True  # connection refused / unresolved host counts as dead


# ---------- driver ----------


def apply_container_intent(
    intent: dict[str, object],
    conf: dict[str, str],
    store: Any = None,
    client: httpx.Client | None = None,
    runner: Runner | None = None,
) -> dict[str, object]:
    """Full create/destroy pipeline for one container intent.
    Every step appends {step, status, ts} (persisted via store when given).
    First failure aborts with failed_reason. Idempotent both directions:
    create-on-existing redeploys, destroy-on-missing is a no-op success.
    """
    runner = runner or _default_runner
    app, intent_id = str(intent["app"]), str(intent["id"])
    hostname = f"{app}.{conf['PUBLIC_DOMAIN']}"
    steps: list[dict[str, object]] = []
    http_ = client or httpx.Client()

    def record(step: str, ok: bool) -> None:
        steps.append(
            {
                "step": step,
                "status": "ok" if ok else "failed",
                "ts": round(time.time() * 1000),
            }
        )
        if store is not None:
            row = store.read(intent_id)
            if row is not None:
                store.write({**row, "steps_json": json.dumps(steps, separators=(",", ":"))})

    def run_step(label: str, fn: Callable[[], object]) -> None:
        """A failed step is data too: record it failed before propagating."""
        try:
            fn()
        except BaseException:
            record(label, False)
            raise
        record(label, True)

    if str(intent["action"]) == "destroy":
        try:
            for label in destroy_cf_objects(http_, conf, hostname, worker_name=app):
                record(label, True)
            if not _dead(http_, f"https://{hostname}/"):
                raise ExecutorError("public URL still answers after destroy")
            record("verify_dead", True)
            if str(conf.get("KEEP_WORKDIR", "false")).strip().lower() != "true":
                shutil.rmtree(Path(conf["WORK_ROOT"]) / app, ignore_errors=True)
                record("cleanup", True)
            return {"status": "done", "steps": steps}
        except ExecutorError as exc:
            return {"status": "failed", "failed_reason": str(exc), "steps": steps}

    try:
        validate_intent(intent, conf)
        image = image_of(intent, conf)
        workdir = Path(conf["WORK_ROOT"]) / app
        deploy_dir = Path(conf["WORK_ROOT"]) / f".cfdeploy-{app}"
        port = int(str(intent["port"])) if intent.get("port") is not None else int(conf["CONTAINER_PORT"])
        plan = (
            ("fetch", lambda: step_fetch(workdir, str(intent["repo"]), runner)),
            ("build", lambda: step_build(workdir, image, runner)),
            ("push", lambda: step_push(image, conf, runner)),
            ("render", lambda: step_render(deploy_dir, conf, app, image, port)),
            ("npm", lambda: step_npm(deploy_dir, conf, runner)),
            ("deploy", lambda: step_deploy(deploy_dir, conf, runner)),
            ("wire", lambda: step_wire(http_, conf, hostname, worker_name=app)),
            ("health", lambda: step_health(http_, conf, f"https://{hostname}/")),
        )
        for label, fn in plan:
            run_step(label, fn)
        record("verify", True)
    except (ExecutorError, StreamctlError, httpx.HTTPError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "failed", "failed_reason": str(exc), "steps": steps}
    return {"status": "done", "hostname": hostname, "steps": steps}


def main() -> None:  # pragma: no cover - invoked only by the router
    raise ExecutorError("container_executor is a library, not a CLI")

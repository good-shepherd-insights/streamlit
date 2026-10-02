#!/usr/bin/env python3
"""cf-router Backend B pytest suite (parity with worker.test.ts).

HTTP surface driven through httpx.ASGITransport against the FastAPI app;
CF API v4 traffic answered from the helpers.RecordingCf stub, with every
URL composed from the conf fixture (zero literals). create_app(apply_now
=True) applies intents inline, so the suite observes the same
pending -> terminal row flow the production daemon thread performs.
"""

from __future__ import annotations

import json
from pathlib import Path

import cf_router_local as router
import container_executor as executor
import helpers
import httpx
from helpers import BASE_CONF, STREAMCTL_DIR, TEST_APP


def conf_at(tmp_path: Path) -> dict[str, str]:
    """Executor conf overlay on the fixture conf, with per-test paths."""
    conf: dict[str, str] = {**BASE_CONF, "STREAMCTL_ROUTER_DB": str(tmp_path / "intents.sqlite3")}
    conf["CF_API_KEY_FILE"] = str(STREAMCTL_DIR / "cf-router/test/fixtures/cf_api_key")
    conf["WORK_ROOT"] = str(tmp_path / "workroot")
    # Fast, deterministic health polling: the fixture's exec defaults carry
    # the real-world pacing (15s poll / 420s timeout); tests never sleep.
    conf["HEALTH_POLL_SEC"] = "0"
    conf["HEALTH_TIMEOUT_SEC"] = "2"
    for key in ("CF_DNS_RECORDS_URL", "CF_WORKERS_ROUTES_URL", "CF_SCRIPTS_URL", "CF_CONTAINER_URL"):
        conf.setdefault(key, router.load_conf()[key])
    return conf


def make_container_intent(**overrides) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "ci-1",
        "action": "create",
        "target": "container",
        "app": TEST_APP,
        "repo": "https://git.example.test/app.git",
        "issued_at": helpers.now_ms(),
    }
    base.update(overrides)
    return base


class FakeRunner:
    """Recording subprocess runner; every argv passes, output is canned."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, cwd=None, env=None, input=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": env, "input": input})

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return R()

    def argvs(self) -> list[list[str]]:
        out: list[list[str]] = []
        for call in self.calls:
            argv = call["argv"]
            assert isinstance(argv, list)
            out.append([str(arg) for arg in argv])
        return out


class FakeCf:
    """Stub httpx transport: records CF API calls, scripted GET list responders,
    and 404-raising client.get for the health/dead polls (public URL).
    """

    def __init__(self, existing: dict[str, list] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._existing = existing or {"routes": [], "dns": [], "script": [], "containers": []}
        self.public_status: list[int | Exception] = []
        self.public_marker = f".{BASE_CONF['PUBLIC_DOMAIN']}/"
        self.client_obj = httpx.Client(transport=httpx.MockTransport(self._handle))

    def respond_list(self, kind: str):
        url_piece = {"routes": "/workers/routes", "dns": "/dns_records", "containers": "/containers/applications"}.get(kind)

        def handler(prior, call):
            return {"result": self._existing.get(kind, [])}
        return url_piece, handler

    def _list_result(self, url: str):
        if "/workers/routes" in url:
            return self._existing["routes"]
        if "/dns_records" in url:
            return self._existing["dns"]
        if "/workers/scripts" in url:
            return self._existing["script"]
        if "/containers/applications" in url:
            return self._existing["containers"]
        return []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method.upper()
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        self.calls.append({"url": url, "method": method, "body": body})
        if self.public_marker in url:
            # public-URL poll: configured script decides alive vs dead
            if not self.public_status:
                raise httpx.ConnectError("connection refused")
            outcome = self.public_status.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome, json={})
        if method == "GET":
            return httpx.Response(200, json={"result": self._list_result(url) or []})
        return httpx.Response(200, json={"result": {}})

    def calls_by(self, method: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c["method"] == method]


# ---------- template render ----------


def test_rendered_wrangler_toml_has_no_placeholders_and_workers_dev_false():
    templates = executor.load_templates(dict(BASE_CONF))
    values = {
        "worker_name": TEST_APP,
        "container_image": "registry.test/apps/ledger-abc:def",
        "class_name": "CLedgerContainer",
        "app_port": 8501,
        "max_instances": 2,
    }
    wrangler = executor.render(templates["CONTAINER_WRANGLER_TEMPLATE"], values)
    index = executor.render(templates["CONTAINER_INDEX_TEMPLATE"], values)
    assert "{{" not in wrangler
    assert "{{" not in index
    assert "workers_dev = false" in wrangler


def test_render_rejects_unfilled_placeholder():
    try:
        executor.render("name = {{worker_name}} port = {{app_port}}", {"worker_name": "x"})
        raise AssertionError("render should have raised")
    except executor.ExecutorError as exc:
        assert "app_port" in str(exc)


# ---------- argv / subprocess steps ----------


def test_build_and_push_exact_argv(tmp_path):
    runner = FakeRunner()
    conf = conf_at(tmp_path)
    workdir = Path(conf["WORK_ROOT"]) / TEST_APP
    workdir.mkdir(parents=True)
def test_push_exact_argv_wrangler_authtool():
    """registry.cloudflare.com auth is wrangler's containers/me exchange, not docker login (raw token login 401s - verified live)."""
    runner = FakeRunner()
    conf = dict(BASE_CONF)
    image = executor.image_of(make_container_intent(), conf)
    executor.step_push(image, conf, runner)
    push = runner.argvs()[-1]
    wrangler = conf["WRANGLER_BIN"].split()
    assert push == wrangler + ["containers", "push", image]
    # no docker login / no secret on argv
    assert "login" not in push


def test_deploy_exact_argv_token_env_only(tmp_path):
    runner = FakeRunner()
    conf = conf_at(tmp_path)
    deploy_dir = Path(conf["WORK_ROOT"]) / (".cfdeploy-" + TEST_APP)
    executor.step_deploy(deploy_dir, conf, runner)
    call = runner.calls[0]
    assert call["argv"] == [*conf["WRANGLER_BIN"].split(), "deploy"]
    assert call["cwd"] == str(deploy_dir)
    assert "CLOUDFLARE_API_TOKEN" in str(call["env"])
    # the token never rides argv
    argv = call["argv"]
    assert isinstance(argv, list)
    assert not any("CLOUDFLARE_API_TOKEN" in str(arg) for arg in argv)


# ---------- create / destroy flows ----------


def test_create_flow_posts_dns_and_route_then_verifies(tmp_path):
    conf = conf_at(tmp_path)
    runner = FakeRunner()
    cf = FakeCf()
    cf.public_status = [200]
    intent = make_container_intent()
    result = executor.apply_container_intent(intent, conf, client=cf.client_obj, runner=runner)
    assert result["status"] == "done", result
    posts = [c for c in cf.calls if c["method"] == "POST"]
    dns_posts = [c for c in posts if "/dns_records" in str(c["url"])]
    route_posts = [c for c in posts if "/workers/routes" in str(c["url"])]
    assert len(dns_posts) == 1
    assert len(route_posts) == 1
    assert dns_posts[0]["body"] == {
        "type": "A",
        "name": f"{TEST_APP}.{conf['PUBLIC_DOMAIN']}",
        "content": conf["ROUTER_DUMMY_IP"],
        "proxied": True,
    }
    assert route_posts[0]["body"] == {
        "pattern": f"{TEST_APP}.{conf['PUBLIC_DOMAIN']}/*",
        "script": TEST_APP,
    }
    public_gets = [
        c
        for c in cf.calls
        if c["method"] == "GET" and cf.public_marker in str(c["url"])
    ]
    assert public_gets, "health poll must have hit the public URL"
    assert public_gets[-1]["url"] == f"https://{TEST_APP}.{conf['PUBLIC_DOMAIN']}/"


def test_destroy_deletes_four_objects_in_order_and_verifies_dead(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf(existing={
        "routes": [{"id": "r1", "pattern": f"{TEST_APP}.{conf['PUBLIC_DOMAIN']}/*"}],
        "dns": [{"id": "d1", "type": "A", "name": f"{TEST_APP}.{conf['PUBLIC_DOMAIN']}"}],
        # scripts list shape: script entries carry the worker name under "id"
        "script": [{"id": TEST_APP}],
        "containers": [{"id": "c1", "name": TEST_APP}],
    })
    cf.public_status = [httpx.ConnectError("refused")]
    intent = make_container_intent(action="destroy")
    result = executor.apply_container_intent(intent, conf, client=cf.client_obj)
    assert result["status"] == "done"
    dels = [c for c in cf.calls if c["method"] == "DELETE"]
    urls = [str(c["url"]) for c in dels]
    assert len(dels) == 4
    assert "/workers/routes/r1" in urls[0]
    assert "/dns_records/d1" in urls[1]
    assert urls[2].endswith("/workers/scripts/ledger")
    assert "/containers/applications/c1" in urls[3]
    # verify the public URL was probed dead afterwards
    assert any(
        c["method"] == "GET" and cf.public_marker in str(c["url"]) for c in cf.calls
    )


def test_destroy_connection_refused_counts_as_dead(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    cf.public_status = [httpx.ConnectError("connection refused")]
    result = executor.apply_container_intent(make_container_intent(action="destroy"), conf, client=cf.client_obj)
    assert result["status"] == "done"


def test_destroy_on_missing_objects_is_noop_success(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()  # all lists empty: nothing to delete
    cf.public_status = [httpx.ConnectError("refused")]
    result = executor.apply_container_intent(make_container_intent(action="destroy"), conf, client=cf.client_obj)
    assert result["status"] == "done"
    assert not [c for c in cf.calls if c["method"] == "DELETE"]


# ---------- idempotency ----------


def test_create_idempotent_redeploy(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    cf.public_status = [200, 200]
    result1 = executor.apply_container_intent(make_container_intent(), conf, client=cf.client_obj, runner=FakeRunner())
    result2 = executor.apply_container_intent(make_container_intent(), conf, client=cf.client_obj, runner=FakeRunner())
    assert result1["status"] == "done", result1
    assert result2["status"] == "done", result2
    # Second create went through the same pipeline: both have the wire POSTs.
    assert result2["steps"][-1]["status"] == "ok"


def test_destroy_idempotent_twice(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    cf.public_status = [httpx.ConnectError("refused"), httpx.ConnectError("refused")]
    r1 = executor.apply_container_intent(make_container_intent(action="destroy"), conf, client=cf.client_obj)
    r2 = executor.apply_container_intent(make_container_intent(action="destroy"), conf, client=cf.client_obj)
    assert r1["status"] == "done"
    assert r2["status"] == "done"


# ---------- validation ----------


def test_invalid_app_label_rejected(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    result = executor.apply_container_intent(make_container_intent(app="Bad_App"), conf, client=cf.client_obj)
    assert result["status"] == "failed"
    assert "invalid app label" in result["failed_reason"]


def test_missing_repo_rejected(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    result = executor.apply_container_intent(make_container_intent(repo=""), conf, client=cf.client_obj)
    assert result["status"] == "failed"
    assert "repo" in result["failed_reason"]


def test_unknown_target_rejected(tmp_path):
    conf = conf_at(tmp_path)
    cf = FakeCf()
    result = executor.apply_container_intent(make_container_intent(target="vm"), conf, client=cf.client_obj)
    assert result["status"] == "failed"


# ---------- failure path ----------


def test_create_failure_reports_failed_reason_and_steps(tmp_path):
    conf = conf_at(tmp_path)

    class BoomRunner(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[:2] == ["docker", "build"]:
                class R:
                    returncode = 1
                    stdout = ""
                    stderr = "dockerfile missing"
                return R()
            return super().__call__(argv, **kw)

    cf = FakeCf()
    result = executor.apply_container_intent(make_container_intent(), conf, client=cf.client_obj, runner=BoomRunner())
    assert result["status"] == "failed"
    assert "dockerfile missing" in result["failed_reason"] or "build" in result["failed_reason"]
    assert [s["status"] for s in result["steps"]] == ["ok", "failed"]
    assert result["steps"][-1]["step"] == "build"


# ---------- FastAPI wiring (HMAC-signed POST -> pending -> building -> done) ----------


def post_signed(app_api, conf, intent):
    mac = router.sign_intent(conf["HMAC_SECRET"], router.canonical_body(intent), intent["issued_at"])
    return app_api.post(router.ROUTER_DEFAULTS and "/v1/intents", json={**intent, "mac": mac})


def test_full_signed_container_intent_lifecycle(tmp_path, monkeypatch):
    conf = dict(conf_at(tmp_path))
    conf["TARGETS"] = "home,container"
    conf["INTENTS_PATH"] = "/v1/intents"
    cf = FakeCf()
    cf.public_status = [200]
    runner = FakeRunner()
    monkeypatch.setattr(router.apply_intent, "_executor_client", cf.client_obj, raising=False)
    monkeypatch.setattr(router.apply_intent, "_executor_runner", runner, raising=False)
    app = router.create_app(conf, client=httpx.Client(), apply_now=True)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=f"https://{conf['API_HOSTNAME']}") as client:
        intent = make_container_intent()
        mac = router.sign_intent(conf["HMAC_SECRET"], router.canonical_body(intent), intent["issued_at"])
        resp = client.post("/v1/intents", json={**intent, "mac": mac})
        assert resp.status_code == 202, resp.json()
        assert resp.json()["status"] == "pending"
        envelope = client.get(f"/v1/intents/{intent['id']}").json()
    assert envelope["id"] == intent["id"]
    assert envelope["status"] == "done", envelope
    assert envelope["app"] == intent["app"]
    steps = envelope.get("steps") or []
    assert [s["step"] for s in steps][-1] == "verify"
    # container pipeline ran: build + push + deploy argvs recorded
    argvs = runner.argvs()
    assert ["docker", "build", "-t", executor.image_of(intent, conf), str(Path(str(conf["WORK_ROOT"])) / str(intent["app"]))] in argvs
    assert ["wrangler-fake", "containers", "push", executor.image_of(intent, conf)] in argvs
    assert [*conf["WRANGLER_BIN"].split(), "deploy"] in argvs


def test_container_failure_persists_failed_reason(tmp_path, monkeypatch):
    conf = dict(conf_at(tmp_path))
    conf["TARGETS"] = "home,container"

    class BoomRunner(FakeRunner):
        def __call__(self, argv, **kw):
            if argv[:2] == ["docker", "build"]:
                class R:
                    returncode = 1
                    stdout = ""
                    stderr = "dockerfile missing"
                return R()
            return FakeRunner.__call__(self, argv, **kw)

    cf = FakeCf()
    runner = BoomRunner()
    monkeypatch.setattr(router.apply_intent, "_executor_client", cf.client_obj, raising=False)
    monkeypatch.setattr(router.apply_intent, "_executor_runner", runner, raising=False)
    app = router.create_app(conf, client=httpx.Client(), apply_now=True)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=f"https://{conf['API_HOSTNAME']}") as client:
        intent = make_container_intent(id="ci-fail")
        mac = router.sign_intent(conf["HMAC_SECRET"], router.canonical_body(intent), intent["issued_at"])
        resp = client.post("/v1/intents", json={**intent, "mac": mac})
        assert resp.status_code == 202
        envelope = client.get(f"/v1/intents/{intent['id']}").json()
    assert envelope["status"] == "failed"
    assert "dockerfile missing" in envelope["failed_reason"] or "build" in envelope["failed_reason"]
    assert envelope["steps"][-1]["status"] == "failed"


def test_unknown_target_rejected_at_POST(tmp_path):
    conf = dict(conf_at(tmp_path))
    conf["TARGETS"] = "home,container"
    app = router.create_app(conf, client=httpx.Client(), apply_now=True)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=f"https://{conf['API_HOSTNAME']}") as client:
        intent = make_container_intent(target="vm")
        mac = router.sign_intent(conf["HMAC_SECRET"], router.canonical_body(intent), intent["issued_at"])
        resp = client.post("/v1/intents", json={**intent, "mac": mac})
        assert resp.status_code == 400

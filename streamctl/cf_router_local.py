#!/usr/bin/env python3
"""streamctl cf-router Backend B - self-hosted FastAPI adapter over uvicorn.

Python twin of streamctl/cf-router/src (the Worker, Backend A): same signed
intent HMAC gate, same diff-based CF apply engine, same HTTP surface. Every
operational value comes from streamctl.conf (or $STREAMCTL_CONF); routes are
composed from INTENTS_PATH and CF endpoints from CF_API_BASE + the CF
zone/account/tunnel ids, so this module spells no endpoint literal itself.
Installed by unit cf-router/systemd/streamctl-routes.service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
import streamctl_core as core
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from streamctl_core import StreamctlError

# ---------- conf ----------

# Same key names as src/conf.ts so switching backends is a conf edit; the last
# four keys exist only on this backend (bind/port/db for the self-hosted unit).
ROUTER_DEFAULTS = {
    "INTENTS_PATH": "/v1/intents",
    "TUNNEL_SERVICE_PREFIX": "http://localhost:",
    "REPLAY_WINDOW_SEC": "300",
    "ROUTE_WAIT_SEC": "30",
    "APPLY_MAX_RETRIES": "3",
    "RECONCILE_INTERVAL_SEC": "3600",
    "STREAMCTL_ROUTER_BIND": "127.0.0.1",
    "STREAMCTL_ROUTER_PORT": "8515",
}
REQUIRED_KEYS = (
    "CF_API_BASE",
    "CF_ZONE_ID",
    "CF_ACCOUNT_ID",
    "CF_TUNNEL_ID",
    "PUBLIC_DOMAIN",
    "API_HOSTNAME",
    "HMAC_SECRET",
    "CF_API_KEY_FILE",
    "STREAMCTL_ROUTER_DB",
)
NUMERIC_KEYS = (
    "REPLAY_WINDOW_SEC",
    "ROUTE_WAIT_SEC",
    "APPLY_MAX_RETRIES",
    "RECONCILE_INTERVAL_SEC",
)


def load_conf() -> dict[str, str]:
    """streamctl_core conf defaults + $STREAMCTL_CONF overlay, then router keys;
    CF endpoint URLs composed unless the conf supplied them directly.
    """
    conf = {**ROUTER_DEFAULTS, **core.load_conf()}
    missing = [key for key in REQUIRED_KEYS if not conf.get(key)]
    if missing:
        raise StreamctlError(f"missing required conf keys: {', '.join(missing)}")
    for key in NUMERIC_KEYS:
        try:
            int(conf[key])
        except ValueError:
            raise StreamctlError(
                f"conf key {key} must be numeric, got: {conf[key]}"
            ) from None
    acct, tun = conf["CF_ACCOUNT_ID"], conf["CF_TUNNEL_ID"]
    conf.setdefault(
        "CF_DNS_RECORDS_URL",
        f"{conf['CF_API_BASE']}/zones/{conf['CF_ZONE_ID']}/dns_records",
    )
    conf.setdefault(
        "CF_TUNNEL_CONFIG_URL",
        f"{conf['CF_API_BASE']}/accounts/{acct}/cfd_tunnel/{tun}/configurations",
    )
    conf.setdefault(
        "CF_CONTAINER_URL", f"{conf['CF_API_BASE']}/accounts/{acct}/containers/apps"
    )
    return conf


# ---------- auth (src/intent.ts twin) ----------


def canonical_body(intent: dict[str, object]) -> str:
    """The intent sans its mac, serialized exactly like TS JSON.stringify."""
    bare = {k: v for k, v in intent.items() if k != "mac"}
    return json.dumps(bare, separators=(",", ":"), ensure_ascii=False)


def sign_intent(secret: str, body: str, issued_at: object) -> str:
    """Hex HMAC-SHA256 over body + issued_at; _ts keeps issued_at TS-String-shaped."""
    issued = (
        str(int(issued_at))
        if isinstance(issued_at, float) and issued_at.is_integer()
        else str(issued_at)
    )
    return hmac.new(
        secret.encode(), (body + issued).encode(), hashlib.sha256
    ).hexdigest()


def verify_intent(
    intent: dict[str, object], conf: dict[str, str], now: float | None = None
) -> str | None:
    """Gate in intent.ts order: unsigned -> stale (boundary inclusive) -> hmac.

    Returns the 401 reason verbatim, or None when the intent is authentic.
    `now` (epoch ms) is injectable to mirror verifyIntent(payload, now, cfg).
    """
    mac = intent.get("mac")
    if not mac:
        return "unsigned intent: mac header is required"
    issued_at = intent["issued_at"]
    window = int(conf["REPLAY_WINDOW_SEC"]) * 1000
    now_ms = time.time() * 1000 if now is None else now
    if abs(now_ms - float(issued_at)) > window:
        return "stale intent: issued_at outside REPLAY_WINDOW_SEC"
    expected = sign_intent(conf["HMAC_SECRET"], canonical_body(intent), issued_at)
    if not hmac.compare_digest(str(mac).lower().encode(), expected.encode()):
        return "bad hmac: wrong secret or tampered body"
    return None


# ---------- store (src/store.ts twin: sqlite instead of KV) ----------


class RouterStore:
    """RouterStore over one sqlite file; rows are the fixed KV envelope shape."""

    _COLUMNS = ("id", "status", "intent", "hostname", "verified_at", "failed_reason")

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, status TEXT,"
                " intent TEXT, hostname TEXT, verified_at INTEGER, failed_reason TEXT)"
            )
            self._db.commit()

    def _select(self, clause: str, values: tuple) -> list[dict[str, object]]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {','.join(self._COLUMNS)} FROM intents WHERE {clause}", values
            ).fetchall()
        return [dict(zip(self._COLUMNS, row, strict=False)) for row in rows]

    @classmethod
    def _record(cls, row: dict[str, object]) -> dict[str, object]:
        row["intent"] = json.loads(str(row["intent"]))
        return row

    def read(self, intent_id: str) -> dict[str, object] | None:
        rows = self._select("id = ?", (intent_id,))
        return self._record(rows[0]) if rows else None

    def where(self, statuses: list[str]) -> list[dict[str, object]]:
        marks = ",".join("?" * len(statuses))
        return [
            self._record(r)
            for r in self._select(f"status IN ({marks})", tuple(statuses))
        ]

    def write(self, row: dict[str, object]) -> None:
        intent = row["intent"]
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,?)",
                (
                    str(row["id"]),
                    str(row["status"]),
                    json.dumps(intent, separators=(",", ":"))
                    if isinstance(intent, dict)
                    else str(intent),
                    row.get("hostname"),
                    row.get("verified_at"),
                    row.get("failed_reason"),
                ),
            )
            self._db.commit()

    def delete(self, intent_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM intents WHERE id = ?", (intent_id,))
            self._db.commit()


# ---------- intent shape (src/worker.ts parseIntent twin) ----------


def parse_intent(body: object) -> dict[str, object] | None:
    """Shape gate; None rejects the POST with 400. The dict passes through
    verbatim: verify_intent re-canonicalizes the same field order it signs.
    """
    if not isinstance(body, dict):
        return None

    def num(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    if (
        not isinstance(body.get("id"), str)
        or body.get("action") not in ("create", "destroy")
        or body.get("target") not in ("home", "container")
        or not isinstance(body.get("app"), str)
        or not num(body.get("issued_at"))
        or (
            body["target"] == "home"
            and not (isinstance(body.get("hostname"), str) and num(body.get("port")))
        )
        or (
            body["target"] == "container"
            and not isinstance(body.get("container_image"), str)
        )
    ):
        return None
    return body


# ---------- CF apply engine (src/core.ts twin) ----------


class CfApiError(Exception):
    """First CF error message, surfaced verbatim like core.ts cfMutation."""


def cf_json(
    client: httpx.Client,
    conf: dict[str, str],
    url: str,
    method: str = "GET",
    body: object = None,
) -> object:
    """One CF API v4 call; key read from conf CF_API_KEY_FILE (0600) per request."""
    key_path = Path(conf["CF_API_KEY_FILE"])
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode != 0o600:
        raise StreamctlError(
            f"CF_API_KEY_FILE must be mode 0600, got {oct(mode)}: {key_path}"
        )
    resp = client.request(
        method,
        url,
        json=body,
        headers={
            "authorization": f"Bearer {key_path.read_text(encoding='utf-8').strip()}"
        },
    )
    data = resp.json()
    if not resp.is_success or data.get("success") is False:
        first = (data.get("errors") or [{}])[0]
        raise CfApiError(first.get("message") or resp.reason_phrase or "cf api error")
    return data.get("result")


def _desired(
    intent: dict[str, object], conf: dict[str, str]
) -> list[dict[str, object]]:
    """Pure function of intent + conf (core.ts desired_for)."""
    if intent["target"] == "container":
        return [
            {
                "kind": "container_instance",
                "ref": intent["app"],
                "props": {
                    "name": intent["app"],
                    "image": intent.get("container_image"),
                },
            }
        ]
    return [
        {
            "kind": "tunnel_ingress",
            "ref": intent["hostname"],
            "props": {
                "hostname": intent["hostname"],
                "service": f"{conf['TUNNEL_SERVICE_PREFIX']}{intent['port']}",
            },
        },
        {
            "kind": "dns_record",
            "ref": intent["hostname"],
            "props": {
                "type": "CNAME",
                "name": intent["hostname"],
                "content": f"{conf['CF_TUNNEL_ID']}.cfargotunnel.com",
                "proxied": True,
            },
        },
    ]


def _current(
    intent: dict[str, object], conf: dict[str, str], client: httpx.Client
) -> list[dict[str, object]]:
    """Live read (core.ts get_desired_state's current half)."""
    if intent["target"] == "container":
        return [
            {
                "kind": "container_instance",
                "ref": i["name"],
                "props": {"name": i["name"], "image": i["image"]},
            }
            for i in (cf_json(client, conf, conf["CF_CONTAINER_URL"]) or [])
            if i["name"] == intent["app"]
        ]
    tunnel = cf_json(client, conf, conf["CF_TUNNEL_CONFIG_URL"]) or {}
    records = (
        cf_json(
            client,
            conf,
            f"{conf['CF_DNS_RECORDS_URL']}?type=CNAME&name={intent['hostname']}",
        )
        or []
    )
    return [
        {"kind": "tunnel_ingress", "ref": rule["hostname"], "props": dict(rule)}
        for rule in _tunnel_ingress(conf, client)
        if rule.get("hostname") == intent["hostname"]
    ] + [
        {
            "kind": "dns_record",
            "ref": rec["name"],
            "props": {k: rec[k] for k in ("id", "type", "name", "content", "proxied")},
        }
        for rec in records
    ]


def _same(a: dict[str, object], b: dict[str, object]) -> bool:
    return a["kind"] == b["kind"] and a["ref"] == b["ref"]


def _existing(
    desired: list[dict[str, object]], current: list[dict[str, object]]
) -> list[dict[str, object]]:
    return [d for d in desired if any(_same(d, c) for c in current)]


def _dns_ids(
    desired: list[dict[str, object]], current: list[dict[str, object]]
) -> list[dict[str, object]]:
    """A dns row that exists carries its provider id, making the diff exact on re-read."""
    return [
        {**d, "props": {**d["props"], "id": c["props"]["id"]}}
        if d["kind"] == "dns_record"
        and (
            c := next(
                (
                    x
                    for x in current
                    if x["kind"] == "dns_record" and x["ref"] == d["ref"]
                ),
                None,
            )
        )
        else d
        for d in desired
    ]


def _tunnel_ingress(
    conf: dict[str, str], client: httpx.Client
) -> list[dict[str, object]]:
    """Live ingress rules - config lives under result.config (CF v4 shape)."""
    result = cf_json(client, conf, conf["CF_TUNNEL_CONFIG_URL"]) or {}
    cfg_obj = result.get("config") if isinstance(result, dict) else None
    return list((cfg_obj or {}).get("ingress") or [])


def _put_ingress(
    conf: dict[str, str],
    client: httpx.Client,
    drop: str | None,
    add: dict | None = None,
) -> None:
    """Single writable object (core.ts putTunnelIngress): replace-with-computed
    rules, merging ALL existing ingress hosts before dropping/adding. App rules
    insert BEFORE the catchall (last rule must remain hostname-less)."""
    rules: list[dict[str, object]] = [
        {"hostname": rule["hostname"], "service": rule["service"]}
        for rule in _tunnel_ingress(conf, client)
        if rule.get("hostname")  # hostful app rules first...
    ]
    catchall = next(
        (r for r in _tunnel_ingress(conf, client) if not r.get("hostname")), None
    )
    rules = [r for r in rules if r.get("hostname") != drop]
    if catchall:
        rules.append(catchall)  # ...catchall last, preserved verbatim
    if add:
        insert = len(rules) - (1 if catchall else 0)
        rules = [*rules[:insert], add, *rules[insert:]]  # type: ignore[list-item]
    cf_json(
        client,
        conf,
        conf["CF_TUNNEL_CONFIG_URL"],
        "PUT",
        {"config": {"ingress": rules}},
    )


def _retryable(action, conf: dict[str, str]) -> str | None:
    """Run one mutation up to APPLY_MAX_RETRIES times; the last CF message wins."""
    reason = None
    for _ in range(int(conf["APPLY_MAX_RETRIES"])):
        try:
            action()
            return None
        except CfApiError as exc:
            reason = str(exc)
    return reason


def _audit(
    intent: dict[str, object], action: str, resources: list[dict[str, object]]
) -> list[dict[str, object]]:
    return [
        {
            "at": round(time.time() * 1000),
            "actor": "router",
            "intent_id": intent["id"],
            "action": action,
            "app": intent["app"],
            "resources": [f"{r['kind']}:{r['ref']}" for r in resources],
        }
    ]


def apply_create(
    intent: dict[str, object],
    current: list[dict[str, object]],
    conf: dict[str, str],
    client: httpx.Client,
) -> dict[str, object]:
    """Diff-based create (core.ts apply_create); re-applying same state is a no-op."""
    desired = _dns_ids(_desired(intent, conf), current)
    existing = _existing(desired, current)
    applied = []
    for resource in [d for d in desired if not any(_same(d, c) for c in current)]:

        def attempt(resource=resource):
            if resource["kind"] == "tunnel_ingress":
                _put_ingress(conf, client, str(intent["hostname"]), resource["props"])
            elif resource["kind"] == "dns_record":
                cf_json(
                    client, conf, conf["CF_DNS_RECORDS_URL"], "POST", resource["props"]
                )
            else:
                cf_json(
                    client,
                    conf,
                    conf["CF_CONTAINER_URL"],
                    "POST",
                    {"name": intent["app"], "image": intent.get("container_image")},
                )

        reason = _retryable(attempt, conf)
        if reason:
            return {
                "status": "failed",
                "applied": applied,
                "removed": [],
                "existing": existing,
                "failed_reason": reason,
                "audit": [],
            }
        applied.append(resource)
    return {
        "status": "done",
        "applied": applied,
        "removed": [],
        "existing": existing,
        "audit": _audit(intent, "create", applied) if applied else [],
    }


def apply_destroy(
    intent: dict[str, object],
    current: list[dict[str, object]],
    conf: dict[str, str],
    client: httpx.Client,
) -> dict[str, object]:
    """Inverse diff (core.ts apply_destroy): only desired ∩ current is deleted."""
    removed = []
    for doomed in _existing(_dns_ids(_desired(intent, conf), current), current):
        try:
            if doomed["kind"] == "tunnel_ingress":
                _put_ingress(conf, client, str(doomed["ref"]))
            elif doomed["kind"] == "dns_record":
                cf_json(
                    client,
                    conf,
                    f"{conf['CF_DNS_RECORDS_URL']}/{doomed['props']['id']}",
                    "DELETE",
                )
            else:
                cf_json(
                    client,
                    conf,
                    f"{conf['CF_CONTAINER_URL']}/{intent['app']}",
                    "DELETE",
                )
        except CfApiError as exc:
            return {
                "status": "failed",
                "applied": [],
                "removed": removed,
                "existing": [],
                "failed_reason": str(exc),
                "audit": [],
            }
        removed.append(doomed)
    return {
        "status": "done",
        "applied": [],
        "removed": removed,
        "existing": [],
        "audit": _audit(intent, "destroy", removed) if removed else [],
    }


def apply_intent(
    intent: dict[str, object],
    conf: dict[str, str],
    store: RouterStore,
    client: httpx.Client,
) -> str:
    """Apply one pending row to its terminal status (queue-consumer.ts twin);
    'skipped' when the row is gone (retracted) or already applied.
    """
    row = store.read(str(intent["id"]))
    if row is None or row["status"] != "pending":
        return "skipped"
    if intent["action"] == "destroy":
        result = apply_destroy(intent, _current(intent, conf, client), conf, client)
    elif intent["target"] == "container":
        result = apply_create(intent, [], conf, client)
        if result["status"] == "done":
            # Containers apply blind, then verify the instance re-reads warm.
            warm = [
                c
                for c in _current(intent, conf, client)
                if c["kind"] == "container_instance" and c["ref"] == intent["app"]
            ]
            result = (
                {**result, "applied": warm, "existing": warm}
                if warm
                else {
                    **result,
                    "status": "failed",
                    "failed_reason": "container instance never re-reads warm after create",
                    "applied": [],
                    "existing": [],
                }
            )
    else:
        result = apply_create(intent, _current(intent, conf, client), conf, client)
    store.write(
        {
            **row,
            "status": result["status"],
            **(
                {"verified_at": round(time.time() * 1000)}
                if result["status"] == "done"
                else {}
            ),
            **(
                {"failed_reason": result["failed_reason"]}
                if result.get("failed_reason")
                else {}
            ),
        }
    )
    return "applied"


# ---------- HTTP adapter (src/worker.ts twin) ----------

TERMINAL_STATUSES = ("done", "failed", "done_stale")


def create_app(
    conf: dict[str, str], *, client: httpx.Client | None = None, apply_now: bool = False
) -> FastAPI:
    """Assemble the FastAPI app from the conf. apply_now applies intents inline
    instead of a daemon thread (tests); production leaves it off.
    """
    app = FastAPI(title="streamctl cf-router (Backend B)", version="1.0")
    store = RouterStore(conf["STREAMCTL_ROUTER_DB"])
    cf_client = client or httpx.Client()
    intents_path = conf["INTENTS_PATH"]

    def run_apply(intent: dict[str, object]) -> None:
        def work() -> None:
            try:
                apply_intent(intent, conf, store, cf_client)
            except (
                Exception
            ) as exc:  # unexpected: terminal failure keeps the row legible
                row = store.read(str(intent["id"]))
                if row and row["status"] == "pending":
                    store.write({**row, "status": "failed", "failed_reason": str(exc)})

        if apply_now:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    @app.post(intents_path, status_code=202)
    async def post_intent(request: Request) -> object:
        try:
            intent = parse_intent(json.loads(await request.body()))
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "request body is not valid JSON"}, status_code=400
            )
        if intent is None:
            return JSONResponse(
                {
                    "error": "malformed intent: id/action/target/app/target facts mismatch"
                },
                status_code=400,
            )
        verdict = verify_intent(intent, conf)
        if verdict:
            return JSONResponse({"error": verdict}, status_code=401)
        store.write(
            {
                "id": intent["id"],
                "status": "pending",
                "intent": intent,
                "hostname": intent.get("hostname"),
            }
        )
        run_apply(
            intent
        )  # row first, then apply: a terminal row is only ever a real row
        return {
            "id": intent["id"],
            "status": "pending",
            "url": f"https://{conf['API_HOSTNAME']}{intents_path}/{intent['id']}",
        }

    @app.get(f"{intents_path}/{{intent_id}}")
    def get_intent(intent_id: str) -> object:
        row = store.read(intent_id)
        if row is None:
            return JSONResponse(
                {"error": f"unknown intent: {intent_id}"}, status_code=404
            )
        envelope = {
            "id": row["id"],
            "status": row["status"],
            "verified": row.get("verified_at") is not None,
            **({"failed_reason": row["failed_reason"]} if row.get("failed_reason") else {}),
        }
        if row.get("hostname"):
            envelope["hostname"] = row["hostname"]
        return envelope

    @app.delete(f"{intents_path}/{{intent_id}}", status_code=204,
                response_class=Response)
    def retract_intent(intent_id: str) -> Response:
        row = store.read(intent_id)
        if row is None:
            return JSONResponse(
                {"error": f"unknown intent: {intent_id}"}, status_code=404
            )
        if row["status"] in TERMINAL_STATUSES:
            return JSONResponse(
                {"error": f"terminal intent cannot be retracted: {row['status']}"},
                status_code=409,
            )
        store.delete(intent_id)
        return Response(status_code=204)

    return app


def main() -> None:
    """Systemd entry: uvicorn with bind/port read from conf, never hardcoded."""
    import uvicorn

    conf = load_conf()
    uvicorn.run(
        create_app(conf),
        host=conf["STREAMCTL_ROUTER_BIND"],
        port=int(conf["STREAMCTL_ROUTER_PORT"]),
        log_level="warning",
    )


if __name__ == "__main__":
    main()

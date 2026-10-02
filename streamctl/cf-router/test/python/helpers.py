"""cf-router Backend B test helpers.

Mirrors test/fixtures/intent.ts + test/helpers.ts: single source for intent
values and a recording CF API stub over httpx MockTransport, signing over the
canonical body exactly as fixtures/intent.ts withMac does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

import cf_router_local as router
import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

# streamctl/ — the conf fixture's relative CF_API_KEY_FILE resolves against it.
STREAMCTL_DIR = Path(__file__).resolve().parents[3]

# Single source for the shared fixture values (same as fixtures/intent.ts).
INTENT_ID = "intent-1"
TEST_APP = "ledger"
TEST_IMAGE = "registry.test/ledger:v7"
WRONG_SECRET = "other-secret"
TEST_DNS_ID = "dns-42"
SECOND_INTENT_ID = "intent-second"

# Tests compose every URL/path from the conf contract, never from literals.
BASE_CONF = router.load_conf()
KEY_FILE = Path(BASE_CONF["CF_API_KEY_FILE"])
if not KEY_FILE.is_absolute():
    KEY_FILE = STREAMCTL_DIR / KEY_FILE
    BASE_CONF["CF_API_KEY_FILE"] = str(KEY_FILE)

TEST_HOSTNAME = "ledger." + BASE_CONF["PUBLIC_DOMAIN"]
TEST_PORT = int(BASE_CONF["STREAMCTL_PORT_BASE"])
INTENTS_PATH = BASE_CONF["INTENTS_PATH"]
CFARGO_TARGET = BASE_CONF["CF_TUNNEL_ID"] + ".cfargotunnel.com"
DNS_RECORDS_URL = BASE_CONF["CF_DNS_RECORDS_URL"]


def service(port: int) -> str:
    return BASE_CONF["TUNNEL_SERVICE_PREFIX"] + str(port)


def conf_for(tmp_db: Path) -> dict[str, str]:
    """Per-test conf: fixture conf + per-test sqlite path."""
    return {**BASE_CONF, "STREAMCTL_ROUTER_DB": str(tmp_db)}


def now_ms() -> int:
    return round(time.time() * 1000)


def make_intent(overrides: dict[str, object] | None = None) -> dict[str, object]:
    base: dict[str, object] = {
        "id": INTENT_ID,
        "action": "client",
    }
    # The base keys mirror fixtures/intent.ts makeIntent; single source here.
    base = {
        "id": INTENT_ID,
        "action": "create",
        "target": "home",
        "app": TEST_APP,
        "hostname": TEST_HOSTNAME,
        "port": TEST_PORT,
        "issued_at": now_ms(),
    }
    base.update(overrides or {})
    return base


def signed(intent: dict[str, object], secret: str) -> dict[str, object]:
    """WithMac twin: mac over the canonical body sans its mac field."""
    bare = {k: v for k, v in intent.items() if k != "mac"}
    return {
        **bare,
        "mac": router.sign_intent(
            secret, router.canonical_body(bare), bare["issued_at"]
        ),
    }


def dns_record_body() -> dict[str, object]:
    """CF-shaped CNAME row the stub answers with; tun id from the conf fixture."""
    return {
        "id": TEST_DNS_ID,
        "type": "CNAME",
        "name": TEST_HOSTNAME,
        "content": CFARGO_TARGET,
        "proxied": True,
    }


class RecordingCf:
    """recordingFetch twin over httpx.MockTransport: records every CF API call
    and answers from scripted routes (first match wins). Unrouted calls answer
    the CF-shaped null success body. Responders are static CF bodies
    {ok, errors, result} or fns of (calls_so_far, call); ok:False surfaces as
    HTTP 400 so core's error path sees the verbatim errors[0].message.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._routes: list[tuple[Callable[[str, str], bool], dict | Callable]] = []

    def respond_to(
        self,
        test: Callable[[str, str], bool],
        respond: dict | Callable,
    ) -> None:
        """Routes match (url, method); a responder fn receives (prior, call)."""
        self._routes.append((test, respond))

    def calls_by(self, method: str) -> list[dict[str, object]]:
        return [call for call in self.calls if call["method"] == method]

    def reset(self) -> None:
        self.calls.clear()
        self._routes.clear()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method.upper()
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        call = {"url": url, "method": method, "body": body}
        self.calls.append(call)
        for test, respond in self._routes:
            if not test(url, method):
                continue
            prior = sum(
                1 for c in self.calls[:-1] if test(str(c["url"]), str(c["method"]))
            )
            resp = respond(prior, call) if callable(respond) else respond
            status = 400 if resp.get("ok") is False else 200
            return httpx.Response(status, json=resp)
        return httpx.Response(200, json={"result": None})


def seed_empty_reads(cf: RecordingCf) -> None:
    """CF-shaped empty reads: every conf-composed GET answers with empties
    (seedEmptyReads twin from worker.test.ts).
    """
    cf.respond_to(
        lambda url, method: method == "GET",
        lambda prior, call: (
            {"result": []}
            if "/dns_records" in str(call["url"])
            else {"result": {"config": {"ingress": []}}}
        ),
    )

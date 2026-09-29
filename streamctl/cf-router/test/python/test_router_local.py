#!/usr/bin/env python3
"""cf-router Backend B pytest suite (parity with worker.test.ts).

HTTP surface driven through httpx.ASGITransport against the FastAPI app;
CF API v4 traffic answered from the helpers.RecordingCf stub, with every
URL composed from the conf fixture (zero literals). create_app(apply_now
=True) applies intents inline, so the suite observes the same
pending -> terminal row flow the production daemon thread performs.
"""

from __future__ import annotations

from pathlib import Path

import cf_router_local as router
import httpx
from helpers import (
    BASE_CONF,
    CFARGO_TARGET,
    DNS_RECORDS_URL,
    INTENT_ID,
    INTENTS_PATH,
    SECOND_INTENT_ID,
    TEST_DNS_ID,
    TEST_HOSTNAME,
    TEST_PORT,
    WRONG_SECRET,
    RecordingCf,
    conf_for,
    dns_record_body,
    make_intent,
    now_ms,
    seed_empty_reads,
    service,
    signed,
)

# Second host on the shared tunnel; composed from the conf, not a literal.
OTHER_HOST = "alpha." + BASE_CONF["PUBLIC_DOMAIN"]


def ledger_rule() -> dict[str, object]:
    return {"hostname": TEST_HOSTNAME, "service": service(TEST_PORT)}


def make_app(cf: RecordingCf, conf: dict[str, str]):
    return router.create_app(conf, client=cf.client(), apply_now=True)


def http(app) -> httpx.Client:
    """Drive the FastAPI app in-process over HTTP (handleFetch twin).

    fastapi.testclient.TestClient satisfies the httpx.Client API; it wires
    the ASGI transport with a working sync context manager in every httpx.
    """
    from fastapi.testclient import TestClient

    return TestClient(app, base_url=f"https://{BASE_CONF['API_HOSTNAME']}")


def ingest(ingress: list, dns: list):
    """CF GET responder: tunnel-config + dns-list reads answer these rows;
    every other conf-composed GET answers the CF null-success body.
    """

    def respond(prior, call):
        if "/dns_records" in str(call["url"]):
            return {"result": dns}
        return {"result": {"ingress": ingress}}

    return respond


def seed_row(conf: dict[str, str], status: str = "pending"):
    """Write one intent row straight into the sqlite store (pendingRow()
    harness twin) and return the store for post-asserts.
    """
    store = router.RouterStore(conf["STREAMCTL_ROUTER_DB"])
    intent = make_intent()
    store.write(
        {
            "id": INTENT_ID,
            "status": status,
            "intent": intent,
            "hostname": intent.get("hostname"),
        }
    )
    return store


def conf_at(tmp_path) -> dict[str, str]:
    return conf_for(Path(tmp_path) / "intents.sqlite3")


def test_post_signed_intent_202_then_done_verified(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_empty_reads(cf)
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(make_intent({"issued_at": now_ms()}), conf["HMAC_SECRET"]),
        )
        assert resp.status_code == 202
        assert resp.json() == {
            "id": INTENT_ID,
            "status": "pending",
            "url": f"https://{BASE_CONF['API_HOSTNAME']}{INTENTS_PATH}/{INTENT_ID}",
        }
        # Applied inline (apply_now): the pending row the POST wrote reaches
        # done through exactly one merged PUT + one CNAME POST.
        assert len(cf.calls_by("PUT")) == 1
        assert len(cf.calls_by("POST")) == 1
        envelope = client.get(f"{INTENTS_PATH}/{INTENT_ID}").json()
    assert envelope == {
        "id": INTENT_ID,
        "status": "done",
        "hostname": TEST_HOSTNAME,
        "verified": True,
    }
    row = router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID)
    assert row["status"] == "done"
    assert row["verified_at"] is not None
    assert row["intent"]["action"] == "create"


def test_post_unsigned_401_no_row_written(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_empty_reads(cf)
    with http(make_app(cf, conf)) as client:
        resp = client.post(INTENTS_PATH, json=make_intent({"issued_at": now_ms()}))
        assert resp.status_code == 401
        assert "unsigned" in resp.json()["error"]
        # The gate rejects before any row write or CF traffic.
        assert cf.calls == []
    assert router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID) is None


def test_post_wrong_mac_401(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_empty_reads(cf)
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(make_intent({"issued_at": now_ms()}), WRONG_SECRET),
        )
        assert resp.status_code == 401
        assert "hmac" in resp.json()["error"]
        assert cf.calls == []
    assert router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID) is None


def test_post_stale_401(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_empty_reads(cf)
    window = int(conf["REPLAY_WINDOW_SEC"]) * 1000
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(
                make_intent({"issued_at": now_ms() - window - 1000}),
                conf["HMAC_SECRET"],
            ),
        )
        assert resp.status_code == 401
        assert "stale" in resp.json()["error"]
        assert cf.calls == []
    assert router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID) is None


def test_get_unknown_404(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    with http(make_app(cf, conf)) as client:
        resp = client.get(f"{INTENTS_PATH}/nope")
        assert resp.status_code == 404
        assert resp.json()["error"]


def test_get_pending_row_envelope(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_row(conf)
    with http(make_app(cf, conf)) as client:
        resp = client.get(f"{INTENTS_PATH}/{INTENT_ID}")
        assert resp.status_code == 200
        assert resp.json() == {
            "id": INTENT_ID,
            "status": "pending",
            "hostname": TEST_HOSTNAME,
            "verified": False,
        }


def test_delete_retracts_pending_row(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_row(conf)
    with http(make_app(cf, conf)) as client:
        resp = client.delete(f"{INTENTS_PATH}/{INTENT_ID}")
        assert resp.status_code == 204
        assert router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID) is None
        assert client.get(f"{INTENTS_PATH}/{INTENT_ID}").status_code == 404


def test_delete_unknown_404(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    with http(make_app(cf, conf)) as client:
        resp = client.delete(f"{INTENTS_PATH}/nope")
        assert resp.status_code == 404


def test_delete_terminal_409(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_row(conf, status="done")
    with http(make_app(cf, conf)) as client:
        resp = client.delete(f"{INTENTS_PATH}/{INTENT_ID}")
        assert resp.status_code == 409
        assert (
            router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID)["status"]
            == "done"
        )


def test_create_merges_existing_ingress_and_posts_cname(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    alpha = {"hostname": OTHER_HOST, "service": service(TEST_PORT + 1)}
    cf.respond_to(
        lambda url, method: method == "GET",
        ingest(ingress=[alpha], dns=[]),
    )
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(make_intent({"issued_at": now_ms()}), conf["HMAC_SECRET"]),
        )
        assert resp.status_code == 202
        puts = cf.calls_by("PUT")
        posts = cf.calls_by("POST")
        # The tunnel PUT keeps the pre-existing ingress host and appends ours.
        assert len(puts) == 1
        assert puts[0]["url"] == conf["CF_TUNNEL_CONFIG_URL"]
        assert puts[0]["body"] == {"config": {"ingress": [alpha, ledger_rule()]}}
        # The CNAME creation uses the conf-composed dns URL and full props.
        assert len(posts) == 1
        assert posts[0]["url"] == DNS_RECORDS_URL
        assert posts[0]["body"] == {
            "type": "CNAME",
            "name": TEST_HOSTNAME,
            "content": CFARGO_TARGET,
            "proxied": True,
        }
        envelope = client.get(f"{INTENTS_PATH}/{INTENT_ID}").json()
    assert envelope["status"] == "done"
    assert envelope["verified"] is True


def test_create_idempotent_zero_mutations(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    cf.respond_to(
        lambda url, method: method == "GET",
        ingest(ingress=[ledger_rule()], dns=[dns_record_body()]),
    )
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(
                make_intent({"id": SECOND_INTENT_ID, "issued_at": now_ms()}),
                conf["HMAC_SECRET"],
            ),
        )
        assert resp.status_code == 202
        # State already holds the exact desired rows: diff is empty.
        assert [call for call in cf.calls if call["method"] != "GET"] == []
        envelope = client.get(f"{INTENTS_PATH}/{SECOND_INTENT_ID}").json()
    assert envelope["status"] == "done"
    assert envelope["verified"] is True


def test_destroy_removes_cname_and_merges_remaining_ingress(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    alpha = {"hostname": OTHER_HOST, "service": service(TEST_PORT + 1)}
    cf.respond_to(
        lambda url, method: method == "GET",
        ingest(ingress=[ledger_rule(), alpha], dns=[dns_record_body()]),
    )
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(
                make_intent({"action": "destroy", "issued_at": now_ms()}),
                conf["HMAC_SECRET"],
            ),
        )
        assert resp.status_code == 202
        puts = cf.calls_by("PUT")
        assert len(puts) == 1
        assert puts[0]["url"] == conf["CF_TUNNEL_CONFIG_URL"]
        # Only the doomed host leaves the tunnel; alpha stays merged.
        assert puts[0]["body"] == {"config": {"ingress": [alpha]}}
        dels = cf.calls_by("DELETE")
        assert len(dels) == 1
        assert dels[0]["url"] == f"{DNS_RECORDS_URL}/{TEST_DNS_ID}"
        assert dels[0]["body"] is None
        assert cf.calls_by("POST") == []
        envelope = client.get(f"{INTENTS_PATH}/{INTENT_ID}").json()
    assert envelope["status"] == "done"
    assert envelope["verified"] is True


def test_destroy_cf_error_verbatim(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    alpha = {"hostname": OTHER_HOST, "service": service(TEST_PORT + 1)}
    cf.respond_to(
        lambda url, method: method == "GET",
        ingest(ingress=[ledger_rule(), alpha], dns=[dns_record_body()]),
    )
    cf.respond_to(
        lambda url, method: method == "PUT",
        {"ok": False, "errors": [{"code": 7502, "message": "ingress rule invalid"}]},
    )
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(
                make_intent({"action": "destroy", "issued_at": now_ms()}),
                conf["HMAC_SECRET"],
            ),
        )
        assert resp.status_code == 202
        # The failed ingress PUT stops the chain before the CNAME delete.
        assert cf.calls_by("DELETE") == []
        row = router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID)
        assert row["status"] == "failed"
        assert row["failed_reason"] == "ingress rule invalid"


def test_create_cf_error_verbatim_retried(tmp_path):
    conf = conf_at(tmp_path)
    cf = RecordingCf()
    seed_empty_reads(cf)
    cf.respond_to(
        lambda url, method: method == "PUT",
        {"ok": False, "errors": [{"code": 7502, "message": "ingress rule invalid"}]},
    )
    with http(make_app(cf, conf)) as client:
        resp = client.post(
            INTENTS_PATH,
            json=signed(make_intent({"issued_at": now_ms()}), conf["HMAC_SECRET"]),
        )
        assert resp.status_code == 202
        # Retried APPLY_MAX_RETRIES times, then the last CF message wins.
        assert len(cf.calls_by("PUT")) == int(conf["APPLY_MAX_RETRIES"])
        assert cf.calls_by("POST") == []
        row = router.RouterStore(conf["STREAMCTL_ROUTER_DB"]).read(INTENT_ID)
        assert row["status"] == "failed"
        assert row["failed_reason"] == "ingress rule invalid"

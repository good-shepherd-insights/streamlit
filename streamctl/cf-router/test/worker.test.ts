/**
 * Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-2026)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// Integration tests for the Backend A I/O adapter (src/worker.ts) and its
// queue consumer + cron entries. HTTP surface is driven via handleFetch; CF
// API traffic is answered from recordingFetch; bindings come from the fake
// KV + queue fixtures. Every route URL is composed from the conf fixture.
import { describe, it, expect } from "vitest";
import { handleFetch, makeWorker, type WorkerDeps } from "../src/worker.js";
import { processIntentMessage, consumeBatch, type ConsumerDeps } from "../src/queue-consumer.js";
import { scheduledReconcile } from "../src/reconcile.js";
import type { Intent, IntentRecord } from "../src/core.js";
import { makeKvStore } from "../src/store.js";
import { makeConf, RAW_CONF, WRONG_SECRET } from "./fixtures/conf.js";
import {
  INTENT_ID,
  makeIntent,
  service,
  TEST_APP,
  TEST_HOSTNAME,
  TEST_IMAGE,
  TEST_PORT,
  withMac,
} from "./fixtures/intent.js";
import { KV_KEY_PREFIX, makeFakeKv, makeFakeQueue } from "./fixtures/kv.js";
import { recordingFetch, type MockResponse } from "./helpers.js";

/** Route URLs composed from the conf fixture only (zero literals). */
const route = (path = "") => `https://${RAW_CONF.API_HOSTNAME}${RAW_CONF.INTENTS_PATH}${path}`;

/** CF-shaped empty reads: every conf-composed GET answers with empties. */
function seedEmptyReads(mockFetch: ReturnType<typeof recordingFetch>) {
  mockFetch.respondTo(
    (_url, method) => method === "GET",
    (_n, call): MockResponse => {
      if (call.url.includes("/dns_records")) return { result: [] };
      if (call.url.includes("/configurations")) return { result: { ingress: [] } };
      if (call.url.includes("/containers")) return { result: [] };
      return { result: null };
    },
  );
}

function harness() {
  const cfg = makeConf();
  const kv = makeFakeKv();
  const store = makeKvStore(kv);
  const queue = makeFakeQueue();
  const doFetch = recordingFetch();
  const deps: WorkerDeps = { cfg, store, queue };
  return { cfg, kv, store, queue, doFetch, deps };
}

async function postIntent(deps: WorkerDeps, intent: Intent | Promise<Intent>): Promise<Response> {
  return await handleFetch(
    new Request(route(), { method: "POST", body: JSON.stringify(await intent) }),
    deps,
  );
}

function pendingRow(overrides: Partial<Intent> = {}): IntentRecord {
  const row: IntentRecord = { id: INTENT_ID, status: "pending", intent: makeIntent(overrides), ...{} };
  if (overrides.hostname !== undefined) {
    return { ...row, hostname: overrides.hostname };
  }
  return { ...row, hostname: TEST_HOSTNAME };
}

describe("POST intents (HMAC gate)", () => {
  it("fresh signed intent -> 202 pending row, intent enqueued, canonical url from conf", async () => {
    const h = harness();
    const intent = await withMac(makeIntent({ issued_at: Date.now() }), h.cfg.HMAC_SECRET as string);

    const resp = await postIntent(h.deps, intent);

    expect(resp.status).toBe(202);
    expect(await resp.json()).toEqual({
      id: INTENT_ID,
      status: "pending",
      url: `${route()}/${INTENT_ID}`,
    });
    // The pending row landed in KV under the fixture prefix before enqueue.
    expect(h.kv.map.has(`${KV_KEY_PREFIX}${INTENT_ID}`)).toBe(true);
    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("pending");
    expect(row?.hostname).toBe(TEST_HOSTNAME);
    expect(h.queue.messages).toEqual([intent]);
  });

  it("unsigned intent -> 401 with verbatim reason", async () => {
    const h = harness();
    const intent = makeIntent({ issued_at: Date.now(), mac: undefined });

    const resp = await postIntent(h.deps, intent);

    expect(resp.status).toBe(401);
    expect(await resp.json()).toMatchObject({ error: expect.stringMatching(/unsigned/i) });
    expect(h.kv.map.size).toBe(0);
    expect(h.queue.messages).toEqual([]);
  });

  it("intent signed with the wrong secret -> 401 /hmac/", async () => {
    const h = harness();
    const intent = await withMac(makeIntent({ issued_at: Date.now() }), WRONG_SECRET);

    const resp = await postIntent(h.deps, intent);

    expect(resp.status).toBe(401);
    expect(await resp.json()).toMatchObject({ error: expect.stringMatching(/hmac/i) });
  });

  it("stale intent (outside REPLAY_WINDOW_SEC) -> 401 /stale/", async () => {
    const h = harness();
    const intent = await withMac(
      makeIntent({ issued_at: Date.now() - (Number(RAW_CONF.REPLAY_WINDOW_SEC) + 1) * 1000 }),
      h.cfg.HMAC_SECRET as string,
    );

    const resp = await postIntent(h.deps, intent);

    expect(resp.status).toBe(401);
    expect(await resp.json()).toMatchObject({ error: expect.stringMatching(/stale/i) });
  });

  for (const [name, body] of Object.entries({
    "non-JSON body": "{not json",
    "missing required field": { action: "create", target: "home", app: null, issued_at: Date.now() },
    "unknown action": { ...makeIntent(), issued_at: Date.now(), action: "explode" },
    "unknown target": { ...makeIntent(), issued_at: Date.now(), target: "bogus" },
    "home intent without hostname": { ...makeIntent(), issued_at: Date.now(), hostname: undefined },
  })) {
    it(`malformed body rejected with 400: ${name}`, async () => {
      const h = harness();
      const resp = await handleFetch(
        new Request(route(), { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body) }),
        h.deps,
      );
      expect(resp.status).toBe(400);
      expect(await resp.json()).toHaveProperty("error");
    });
  }

  it("routes outside the conf-composed intents path -> 404", async () => {
    const h = harness();
    const resp = await handleFetch(
      new Request(`https://${RAW_CONF.API_HOSTNAME}${RAW_CONF.UNROUTED_PATH}`, { method: "POST" }),
      h.deps,
    );
    expect(resp.status).toBe(404);
  });
});

describe("GET intents/:id", () => {
  it("unknown id -> 404", async () => {
    const h = harness();
    const resp = await handleFetch(new Request(route("/nope")), h.deps);
    expect(resp.status).toBe(404);
    expect(await resp.json()).toMatchObject({ error: expect.any(String) });
  });

  it("known pending row -> status envelope with verified=false", async () => {
    const h = harness();
    await h.store.write(pendingRow());

    const resp = await handleFetch(new Request(route(`/${INTENT_ID}`)), h.deps);

    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({
      id: INTENT_ID,
      status: "pending",
      hostname: TEST_HOSTNAME,
      verified: false,
    });
  });

  it("pending->done flow: POST -> consumer applies -> GET reports done+verified", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    const intent = await withMac(makeIntent({ issued_at: Date.now() }), h.cfg.HMAC_SECRET as string);
    await postIntent(h.deps, intent);

    await processIntentMessage(h.queue.messages[0], { cfg: h.cfg, store: h.store, doFetch: h.doFetch });

    const resp = await handleFetch(new Request(route(`/${INTENT_ID}`)), h.deps);
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({
      id: INTENT_ID,
      status: "done",
      hostname: TEST_HOSTNAME,
      verified: true,
    });
  });
});

describe("DELETE intents/:id (retract)", () => {
  it("retracts a pending row: 204 and the row is gone", async () => {
    const h = harness();
    await h.store.write(pendingRow());

    const resp = await handleFetch(new Request(route(`/${INTENT_ID}`), { method: "DELETE" }), h.deps);

    expect(resp.status).toBe(204);
    expect(h.kv.map.has(`${KV_KEY_PREFIX}${INTENT_ID}`)).toBe(false);
    expect(await h.store.read(INTENT_ID)).toBeUndefined();
  });

  it("unknown id -> 404", async () => {
    const h = harness();
    const resp = await handleFetch(new Request(route("/nope"), { method: "DELETE" }), h.deps);
    expect(resp.status).toBe(404);
  });

  it("terminal row -> 409 and the row is left untouched", async () => {
    const h = harness();
    await h.store.write({ id: INTENT_ID, status: "done", intent: makeIntent() });

    const resp = await handleFetch(new Request(route(`/${INTENT_ID}`), { method: "DELETE" }), h.deps);

    expect(resp.status).toBe(409);
    expect((await h.store.read(INTENT_ID))?.status).toBe("done");
  });
});

describe("queue consumer (dispatch by intent.target)", () => {
  function consumerDeps(h: ReturnType<typeof harness>, audit?: unknown[]): ConsumerDeps {
    return {
      cfg: h.cfg,
      store: h.store,
      doFetch: h.doFetch,
      ...(audit ? { onAudit: (entry: unknown) => void audit.push(entry) } : {}),
    };
  }

  it("flips the row pending->done, records the audit trail, and applies via CF", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    await h.store.write(pendingRow());
    const audit: unknown[] = [];

    const outcome = await processIntentMessage(makeIntent(), consumerDeps(h, audit));

    expect(outcome).toBe("applied");
    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("done");
    expect(row?.verified_at).toBeTypeOf("number");
    expect(h.doFetch.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
    expect(h.doFetch.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ action: "create", app: TEST_APP });
  });

  it("failed passthrough: the CF error lands verbatim in failed_reason", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    h.doFetch.respondTo(
      (_url, method) => method === "PUT",
      { ok: false, errors: [{ code: 7502, message: "ingress rule invalid" }] },
    );
    await h.store.write(pendingRow());

    await processIntentMessage(makeIntent(), consumerDeps(h));

    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("failed");
    expect(row?.failed_reason).toBe("ingress rule invalid");
  });

  it("skips messages with a non-pending row (zero CF mutations)", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    await h.store.write({ id: INTENT_ID, status: "done", intent: makeIntent() });

    const outcome = await processIntentMessage(makeIntent(), consumerDeps(h));

    expect(outcome).toBe("skipped");
    expect(h.doFetch.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it("skips retracted rows (store row gone) with zero CF calls", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);

    const outcome = await processIntentMessage(makeIntent(), consumerDeps(h));

    expect(outcome).toBe("skipped");
    expect(h.doFetch.calls).toHaveLength(0);
  });

  it("consumeBatch applies each queued message in order", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    const second = { ...makeIntent(), id: "i-second" };
    await h.store.write(pendingRow());
    await h.store.write({ ...pendingRow(), id: "i-second", intent: second });

    await consumeBatch(
      { messages: [{ body: makeIntent() }, { body: second }] },
      { cfg: h.cfg, store: h.store, doFetch: h.doFetch },
    );

    expect((await h.store.read(INTENT_ID))?.status).toBe("done");
    expect((await h.store.read("i-second"))?.status).toBe("done");
  });

  it("container target: applies through CF_CONTAINER_URL only, after a warm re-read", async () => {
    const h = harness();
    // Containers GETs answer with a warm instance; the executor re-reads
    // CF_CONTAINER_URL after the create to verify (PRD 9).
    h.doFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/containers"),
      { result: [{ name: TEST_APP, image: TEST_IMAGE, status: "healthy" }] },
    );
    h.doFetch.respondTo((url, method) => method === "POST" && url.includes("/containers"), { result: null });
    await h.store.write(
      pendingRow({ target: "container", container_image: TEST_IMAGE, hostname: undefined }),
    );

    await processIntentMessage(
      makeIntent({ target: "container", container_image: TEST_IMAGE, hostname: undefined }),
      consumerDeps(h),
    );

    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("done");
    expect(row?.verified_at).toBeTypeOf("number");
    // POST create + the warm re-read GET: every container call used
    // exactly the conf-composed CF_CONTAINER_URL (POST + GET on it).
    expect(h.doFetch.calls.filter((c) => c.url.includes("/containers"))).toHaveLength(2);
    expect(h.doFetch.calls.filter((c) => c.url.includes("/containers") && c.method === "POST")).toHaveLength(1);
  });

  it("container target: an instance that never re-reads warm fails the row", async () => {
    const h = harness();
    // GET always empty (no instance), POST create "succeeds".
    seedEmptyReads(h.doFetch);
    h.doFetch.respondTo((url, method) => method === "POST" && url.includes("/containers"), { result: null });
    await h.store.write(
      pendingRow({ target: "container", container_image: TEST_IMAGE, hostname: undefined }),
    );

    await processIntentMessage(
      makeIntent({ target: "container", container_image: TEST_IMAGE, hostname: undefined }),
      consumerDeps(h),
    );

    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("failed");
    expect(row?.failed_reason).toMatch(/warm/i);
  });
});

describe("scheduled reconcile over the KV store", () => {
  it("repairs drift from store rows through the same apply path", async () => {
    const h = harness();
    seedEmptyReads(h.doFetch);
    await h.store.write({ id: INTENT_ID, status: "done", intent: makeIntent() });

    const summary = await scheduledReconcile({ cfg: h.cfg, store: h.store, doFetch: h.doFetch });

    expect(summary.checked).toBe(1);
    expect(summary.repaired).toEqual([INTENT_ID]);
    const row = await h.store.read(INTENT_ID);
    expect(row?.status).toBe("done");
    expect(h.doFetch.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
    expect(h.doFetch.calls.filter((c) => c.method === "POST")).toHaveLength(1);
  });

  it("leaves rows without drift untouched (no mutations)", async () => {
    const h = harness();
    h.doFetch.respondTo(
      (_url, method) => method === "GET",
      (_n, call): MockResponse => {
        if (call.url.includes("/dns_records")) return { result: [] };
        if (call.url.includes("/configurations")) {
          return { result: { ingress: [{ hostname: TEST_HOSTNAME, service: service(TEST_PORT) }] } };
        }
        return { result: null };
      },
    );
    await h.store.write({ id: INTENT_ID, status: "done", intent: makeIntent() });

    const summary = await scheduledReconcile({ cfg: h.cfg, store: h.store, doFetch: h.doFetch });

    expect(summary.checked).toBe(1);
    expect(summary.repaired).toEqual([]);
    expect(h.doFetch.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });
});

describe("makeWorker (bindings validated from env)", () => {
  const env = () => ({
    ...RAW_CONF,
    INTENTS_KV: makeFakeKv(),
    QUEUE: makeFakeQueue(),
  });

  it("assembles a fetch handler from the env object (conf vars + bindings)", async () => {
    const worker = makeWorker(env());
    const intent = await withMac(makeIntent({ issued_at: Date.now() }), String(RAW_CONF.HMAC_SECRET));

    const resp = await worker.fetch(new Request(route(), { method: "POST", body: JSON.stringify(intent) }));
    expect(resp.status).toBe(202);
  });

  it("missing QUEUE binding throws", () => {
    const { QUEUE: _queue, ...withoutQueue } = env();
    expect(() => makeWorker(withoutQueue)).toThrow(/QUEUE/);
  });

  it("missing INTENTS_KV binding throws", () => {
    const { INTENTS_KV: _kv, ...withoutKv } = env();
    expect(() => makeWorker(withoutKv)).toThrow(/INTENTS_KV/);
  });
});

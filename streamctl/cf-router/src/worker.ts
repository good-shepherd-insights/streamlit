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

// cf-router Worker adapter (Backend A) — the HTTP surface over the shared
// core. Every route is composed from Config (API_HOSTNAME + INTENTS_PATH), so
// the adapter spells no path literals: a request outside the conf-composed
// intents path answers 404. POST of a signed intent is gated by the core
// auth() (401 stays verbatim), accepted intents are persisted as pending KV
// rows and enqueued for the queue consumer. GET/DELETE serve and retract
// rows. makeWorker() assembles the handlers from the Worker env object,
// validating the KV/queue bindings before the conf is loaded.

import {
  auth,
  type Intent,
  type IntentRecord,
  type IntentStatus,
  type RouterStore,
  type CfFetch,
} from "./core.js";
import { loadConf, type Config } from "./conf.js";
import { makeKvStore, type RouterKV } from "./store.js";
import { consumeBatch, type ConsumerDeps } from "./queue-consumer.js";
import { scheduledReconcile } from "./reconcile.js";

/** Queues producer binding; the signed intent is the message body. */
export interface QueueProducer {
  send(message: unknown): Promise<void>;
}

/** Adapter deps: conf-composed objects; the adapter owns no config of its own. */
export interface WorkerDeps {
  cfg: Config;
  store: RouterStore;
  queue: QueueProducer;
}

/** Statuses past which an intent can no longer be retracted. */
const TERMINAL_STATUSES: IntentStatus[] = ["done", "failed", "done_stale"];

const jsonResponse = (body: unknown, status: number): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const notFound = (pathname: string): Response => jsonResponse({ error: `no route: ${pathname}` }, 404);

/** Parse + shape-check a POST body into an Intent; undefined means reject with 400. */
function parseIntent(raw: unknown): Intent | undefined {
  if (typeof raw !== "object" || raw === null) return undefined;
  const body = raw as Record<string, unknown>;
  const action = body["action"] as "create" | "destroy";
  const target = body["target"] as "home" | "container";
  if (
    typeof body["id"] !== "string" ||
    (action !== "create" && action !== "destroy") ||
    (target !== "home" && target !== "container") ||
    typeof body["app"] !== "string" ||
    typeof body["issued_at"] !== "number"
  ) {
    return undefined;
  }
  // Every target carries its own routing facts: home needs hostname + port,
  // container needs the image. A missing fact is a 400, not a runtime error.
  if (target === "home" && (typeof body["hostname"] !== "string" || typeof body["port"] !== "number")) {
    return undefined;
  }
  const container_image = body["container_image"] as string;
  if (target === "container" && typeof container_image !== "string") {
    return undefined;
  }
  // The parsed object is returned verbatim: auth() re-canonicalizes the body
  // it signs, and a rebuilt object can differ from the signed key order.
  return body as unknown as Intent;
}

/** POST /intents: gate, persist the pending row, then enqueue for the consumer. */
async function postIntent(request: Request, deps: WorkerDeps): Promise<Response> {
  let parsed: unknown;
  try {
    parsed = await request.json();
  } catch {
    return jsonResponse({ error: "request body is not valid JSON" }, 400);
  }
  const intent = parseIntent(parsed);
  if (intent === undefined) {
    return jsonResponse({ error: "malformed intent: id/action/target/app/target facts mismatch" }, 400);
  }
  const verdict = await auth(intent, Date.now(), deps.cfg);
  if (!verdict.ok) {
    return jsonResponse({ error: verdict.reason }, verdict.status);
  }
  // Row first, then queue: an enqueued message must never precede its store row.
  const row: IntentRecord = { id: intent.id, status: "pending", intent, hostname: intent.hostname };
  await deps.store.write(row);
  await deps.queue.send(intent);
  return jsonResponse(
    {
      id: intent.id,
      status: row.status,
      url: `https://${deps.cfg.API_HOSTNAME}${deps.cfg.INTENTS_PATH}/${intent.id}`,
    },
    202,
  );
}

/** GET /intents/:id — the status envelope over the stored row. */
async function getIntent(id: string, deps: WorkerDeps): Promise<Response> {
  const row = await deps.store.read(id);
  if (row === undefined) return jsonResponse({ error: `unknown intent: ${id}` }, 404);
  return jsonResponse(
    {
      id: row.id,
      status: row.status,
      hostname: row.hostname,
      verified: row.verified_at !== undefined,
      ...(row.failed_reason !== undefined ? { failed_reason: row.failed_reason } : {}),
    },
    200,
  );
}

/** DELETE /intents/:id — retract while the intent has not been applied. */
async function retractIntent(id: string, deps: WorkerDeps): Promise<Response> {
  const row = await deps.store.read(id);
  if (row === undefined) return jsonResponse({ error: `unknown intent: ${id}` }, 404);
  if (TERMINAL_STATUSES.includes(row.status)) {
    return jsonResponse({ error: `terminal intent cannot be retracted: ${row.status}` }, 409);
  }
  // The row is removed outright: a vanished row is exactly what the queue
  // consumer reads as "retracted" and skips without CF mutations.
  await deps.store.delete?.(id);
  return new Response(null, { status: 204 });
}

/** PRD 5b.i fetch handler for Backend A. Only the intents path is routed. */
export async function handleFetch(request: Request, deps: WorkerDeps): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname;
  const rest = path.slice(deps.cfg.INTENTS_PATH.length);
  // Host + path must both match the conf-composed intents URL; anything else
  // (foreign host, sibling paths, unknown methods) has no route here.
  const routed =
    url.hostname === deps.cfg.API_HOSTNAME &&
    (rest === "" || rest.startsWith("/")) &&
    (path === deps.cfg.INTENTS_PATH || path.startsWith(`${deps.cfg.INTENTS_PATH}/`));
  if (!routed) return notFound(path);
  if (rest === "" && request.method === "POST") return await postIntent(request, deps);
  if (request.method === "GET") return await getIntent(rest.slice(1), deps);
  if (request.method === "DELETE") return await retractIntent(rest.slice(1), deps);
  return jsonResponse({ error: `method not allowed: ${request.method} ${path}` }, 405);
}

/** CF credentials ride in a Worker SECRET; every outbound API fetch gets it. */
export function authedFetch(token: string): CfFetch {
  return (url, init) => fetch(url, { ...init, headers: { ...init?.headers, authorization: `Bearer ${token}` } });
}

/** Assemble the worker runtime from env: vars + bindings + CF_API_KEY secret. */
export function makeWorker(
  env: Record<string, unknown>,
  extra?: { doFetch?: CfFetch; token?: string },
): {
  fetch: (request: Request) => Promise<Response>;
  scheduled: (controller?: ScheduledEvent, ctx?: ExecutionContext) => Promise<void>;
  queue?: (batch: MessageBatch<unknown>) => Promise<void>;
} {
  const token = extra?.token ?? (env["CF_API_KEY"] as string | undefined);
  if (!token) throw new Error("missing required secret: CF_API_KEY");
  const doFetch = extra?.doFetch ?? authedFetch(token);
  const queue = env["QUEUE"];
  if (queue === undefined) throw new Error("missing required binding: QUEUE");
  const kv = env["INTENTS_KV"];
  if (kv === undefined) throw new Error("missing required binding: INTENTS_KV");
  const cfg = loadConf(env);
  const store = makeKvStore(kv as RouterKV);
  const consumer: ConsumerDeps = { cfg, store, doFetch };
  return {
    fetch: (request) => handleFetch(request, { cfg, store, queue: queue as QueueProducer }),
    scheduled: async () => {
      await scheduledReconcile({ cfg, store, doFetch });
    },
    queue: async (batch) => {
      await consumeBatch(
        { messages: batch.messages.map((m) => ({ body: m.body, attempts: m.attempts ?? 0 })) },
        consumer,
      );
    },
  };
}

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

// cf-router queue consumer (Backend A) — applies each delivered intent
// through the core apply engine and flips its store row to the terminal
// status. The store row is the gate: only a pending row is applied; a row
// retracted before delivery has vanished and arrives here as "skipped" with
// zero CF mutations. CF failures land verbatim in failed_reason; unexpected
// throws bubble back to the queue for redelivery until APPLY_MAX_RETRIES
// attempts are spent.

import {
  apply_create,
  apply_destroy,
  get_desired_state,
  type ApplyResult,
  type AuditEntry,
  type CfFetch,
  type Intent,
  type RouterStore,
} from "./core.js";
import type { Config } from "./conf.js";

export interface ConsumerDeps {
  cfg: Config;
  store: RouterStore;
  doFetch: CfFetch;
  /** Optional audit sink; entries come straight from the core apply result. */
  onAudit?: (entry: AuditEntry) => void;
}

export type MessageOutcome = "applied" | "skipped";

interface QueueMessage {
  body: unknown;
  attempts?: number;
}

interface QueueBatch {
  messages: QueueMessage[];
}

/** One delivered intent: apply it and move the row to its terminal status. */
export async function processIntentMessage(intent: unknown, deps: ConsumerDeps): Promise<MessageOutcome> {
  const body = intent as Intent;
  const row = await deps.store.read(body.id);
  if (row === undefined || row.status !== "pending") {
    // Retracted rows have been removed from the store; terminal rows were
    // already applied. Neither touches CF again.
    return "skipped";
  }
  const result = await applyIntent(body, deps);
  await deps.store.write({
    ...row,
    status: result.status,
    ...(result.status === "done" ? { verified_at: Date.now() } : {}),
    ...(result.failed_reason !== undefined ? { failed_reason: result.failed_reason } : {}),
  });
  for (const entry of result.audit) deps.onAudit?.(entry);
  return "applied";
}

/** Apply by action/target: destroy inverts from the live read, creates diff. */
async function applyIntent(intent: Intent, deps: ConsumerDeps): Promise<ApplyResult> {
  if (intent.action === "destroy") {
    const state = await get_desired_state(intent, deps.cfg, deps.doFetch);
    return await apply_destroy(intent, state.current, deps.cfg, deps.doFetch);
  }
  if (intent.target === "container") {
    // Containers apply blind, then the core re-read verifies the instance is
    // warm; one that never shows up fails the row without leaving drift (PRD 9).
    const result = await apply_create(intent, [], deps.cfg, deps.doFetch);
    if (result.status === "failed") return result;
    const warm = await verifyWarm(intent, deps);
    return { ...warm, audit: [...result.audit, ...warm.audit] };
  }
  const state = await get_desired_state(intent, deps.cfg, deps.doFetch);
  return await apply_create(intent, state.current, deps.cfg, deps.doFetch);
}

/** Re-read the container target; done only when the instance reads back warm. */
async function verifyWarm(intent: Intent, deps: ConsumerDeps): Promise<ApplyResult> {
  const state = await get_desired_state(intent, deps.cfg, deps.doFetch);
  const warm = state.current.some((c) => c.kind === "container_instance" && c.ref === intent.app);
  if (!warm) {
    return {
      status: "failed",
      applied: [],
      removed: [],
      existing: [],
      failed_reason: "container instance never re-reads warm after create",
      audit: [],
    };
  }
  return { status: "done", applied: state.current, removed: [], existing: state.current, audit: [] };
}

/** Apply a whole delivery in order; abort on a throw the queue may retry. */
export async function consumeBatch(batch: QueueBatch, deps: ConsumerDeps): Promise<void> {
  for (const message of batch.messages) {
    try {
      await processIntentMessage(message.body, deps);
    } catch (error) {
      // Transient failure: rethrow inside the retry budget so the queue
      // redelivers; once the attempts are spent the message is dropped.
      if ((message.attempts ?? 1) < deps.cfg.APPLY_MAX_RETRIES) throw error;
    }
  }
}

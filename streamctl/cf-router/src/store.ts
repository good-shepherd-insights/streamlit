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

// cf-router KV store (Backend A) — the RouterStore adapter over Workers KV.
// One KV row per intent, holding the fixed envelope
// {id, status, intent, hostname, verified_at, failed_reason}; the core
// IntentRecord shape is projected onto it for storage and back for reads
// (verified_at -> its boolean projection, detail is envelope-less and stays
// unset on the KV side). Row keys carry the store's own prefix constant so
// where() can list exactly the intent rows.

import type { Intent, IntentRecord, IntentStatus, RouterStore } from "./core.js";

/** Minimal KV binding surface used here (subset of workers KVNamespace). */
export interface RouterKV {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
  delete(key: string): Promise<void>;
  list(options?: { prefix?: string }): Promise<{ keys: { name: string }[] }>;
}

/** The persisted KV row shape (the thin envelope over the core record). */
export interface IntentEnvelope {
  id: string;
  status: IntentStatus;
  intent: Intent;
  hostname?: string;
  verified_at?: number;
  failed_reason?: string;
}

/** Prefix for every stored intent row; ids are appended verbatim. */
export const INTENT_KV_PREFIX = "intent/";

const kvKey = (id: string) => INTENT_KV_PREFIX + id;

function toEnvelope(record: IntentRecord): IntentEnvelope {
  return {
    id: record.id,
    status: record.status,
    intent: record.intent,
    ...(record.hostname !== undefined ? { hostname: record.hostname } : {}),
    ...(record.verified_at !== undefined ? { verified_at: record.verified_at } : {}),
    ...(record.failed_reason !== undefined ? { failed_reason: record.failed_reason } : {}),
  };
}

function toRecord(envelope: IntentEnvelope): IntentRecord {
  // `detail` is not part of the KV envelope; failed_reason carries the
  // terminal diagnosis. `verified` is the boolean projection of verified_at.
  return {
    ...envelope,
    ...(envelope.verified_at !== undefined ? { verified: true } : {}),
  };
}

/** RouterStore over the kv binding: read/where/write (+delete for retract). */
export function makeKvStore(kv: RouterKV): RouterStore {
  return {
    async read(id) {
      const raw = await kv.get(kvKey(id));
      if (raw === null) return undefined;
      return toRecord(JSON.parse(raw) as IntentEnvelope);
    },
    // KV has no per-value indexes: list the intent rows and read them out,
    // then filter in-memory. Volume is tens of intents/day, not thousands.
    async where(statuses) {
      const { keys } = await kv.list({ prefix: INTENT_KV_PREFIX });
      const rows = await Promise.all(
        keys.map((key) => this.read(key.name.replace(INTENT_KV_PREFIX, ""))),
      );
      return rows.filter((row): row is IntentRecord => row !== undefined && statuses.includes(row.status));
    },
    async write(row) {
      await kv.put(kvKey(row.id), JSON.stringify(toEnvelope(row)));
    },
    async delete(id) {
      await kv.delete(kvKey(id));
    },
  };
}

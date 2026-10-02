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

// Tests for the KV-backed RouterStore (src/store.ts). The store keeps one
// envelope row per intent under the fixture KV prefix; tests drive it
// through the core RouterStore contract the adapters consume.
import { describe, it, expect } from "vitest";
import { makeKvStore } from "../src/store.js";
import type { IntentRecord, IntentStatus } from "../src/core.js";
import { INTENT_ID, makeIntent, TEST_HOSTNAME } from "./fixtures/intent.js";
import { KV_KEY_PREFIX, makeFakeKv } from "./fixtures/kv.js";

const ALL_STATUSES: IntentStatus[] = ["pending", "done", "failed", "done_stale"];

function pendingRow(overrides: Partial<IntentRecord> = {}): IntentRecord {
  return { id: INTENT_ID, status: "pending", intent: makeIntent(), ...overrides };
}

const kvKey = (id: string) => `${KV_KEY_PREFIX}${id}`;

describe("makeKvStore.read", () => {
  it("round-trips the envelope: status, intent, hostname, verified_at, failed_reason", async () => {
    const kv = makeFakeKv();
    const store = makeKvStore(kv);
    await store.write({
      id: INTENT_ID,
      status: "done",
      intent: makeIntent(),
      hostname: TEST_HOSTNAME,
      failed_reason: undefined,
      verified_at: 42,
    });

    const row = await store.read(INTENT_ID);
    expect(row).toEqual({
      id: INTENT_ID,
      status: "done",
      intent: makeIntent(),
      hostname: TEST_HOSTNAME,
      verified_at: 42,
      verified: true,
    });
  });

  it("returns undefined for an unknown id (KV null)", async () => {
    const store = makeKvStore(makeFakeKv());
    expect(await store.read("nope")).toBeUndefined();
  });

  for (const status of ALL_STATUSES) {
    it(`reads status "${status}"`, async () => {
      const store = makeKvStore(makeFakeKv());
      await store.write(pendingRow({ status }));
      expect((await store.read(INTENT_ID))?.status).toBe(status);
    });
  }
});

describe("makeKvStore.write", () => {
  it("persists the row under the fixture KV prefix", async () => {
    const kv = makeFakeKv();
    const store = makeKvStore(kv);

    await store.write(pendingRow({ hostname: TEST_HOSTNAME }));

    expect(kv.map.has(kvKey(INTENT_ID))).toBe(true);
    const envelope = JSON.parse(kv.map.get(kvKey(INTENT_ID)) as string);
    expect(envelope).toEqual({
      id: INTENT_ID,
      status: "pending",
      intent: makeIntent(),
      hostname: TEST_HOSTNAME,
    });
  });

  it("overwrites in place: a status flip persists as the single current row", async () => {
    const kv = makeFakeKv();
    const store = makeKvStore(kv);
    await store.write(pendingRow());

    const row = (await store.read(INTENT_ID)) as IntentRecord;
    await store.write({ ...row, status: "done", verified_at: 7 });

    expect(kv.map.size).toBe(1);
    expect((await store.read(INTENT_ID))?.status).toBe("done");
    expect((await store.read(INTENT_ID))?.verified_at).toBe(7);
  });
});

describe("makeKvStore.where", () => {
  it("filters rows by the requested statuses only", async () => {
    const store = makeKvStore(makeFakeKv());
    await store.write(pendingRow());
    await store.write({ id: "i-2", status: "done", intent: makeIntent({ id: "i-2" }) });
    await store.write({ id: "i-3", status: "failed", intent: makeIntent({ id: "i-3" }) });
    await store.write({ id: "i-4", status: "done_stale", intent: makeIntent({ id: "i-4" }) });

    expect((await store.where(["pending"])).map((r) => r.id)).toEqual([INTENT_ID]);
    const doneRows = await store.where(["done", "done_stale"]);
    expect(doneRows.map((r) => r.id).sort()).toEqual(["i-2", "i-4"]);
  });

  it("returns [] on an empty store", async () => {
    expect(await makeKvStore(makeFakeKv()).where(ALL_STATUSES)).toEqual([]);
  });
});

describe("makeKvStore.delete", () => {
  it("removes the row (retract support) so a later read is undefined", async () => {
    const store = makeKvStore(makeFakeKv());
    await store.write(pendingRow());
    await store.delete?.(INTENT_ID);
    expect(await store.read(INTENT_ID)).toBeUndefined();
    expect(await store.where(["pending"])).toEqual([]);
  });
});

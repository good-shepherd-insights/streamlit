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

// Fake KV + queue fixtures for cf-router adapter tests. Plain Map-backed
// implementations of the binding surface worker.ts uses — no worker runtime,
// no mock-cloudflare library. Every key below is carried by the fixtures so
// tests reference fixtures, not literals.
export const KV_KEY_PREFIX = "intent/";

export interface FakeKv {
  map: Map<string, string>;
  get: (key: string) => Promise<string | null>;
  put: (key: string, value: string) => Promise<void>;
  delete: (key: string) => Promise<void>;
  list: (options?: { prefix?: string }) => Promise<{ keys: { name: string }[] }>;
}

/** Map-backed KV binding; `seed` values are stored serialized as put() would. */
export function makeFakeKv(seed: Record<string, unknown> = {}): FakeKv {
  const map = new Map<string, string>();
  for (const [key, value] of Object.entries(seed)) map.set(key, JSON.stringify(value));
  return {
    map,
    async get(key) {
      return map.get(key) ?? null;
    },
    async put(key, value) {
      map.set(key, value);
    },
    async delete(key) {
      map.delete(key);
    },
    async list(options) {
      const prefix = options?.prefix ?? "";
      return {
        keys: [...map.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })),
      };
    },
  };
}

/** Queues producer binding stand-in, recording enqueued messages for asserts. */
export function makeFakeQueue() {
  const messages: unknown[] = [];
  return {
    messages,
    send: (message: unknown) => {
      messages.push(message);
      return Promise.resolve();
    },
  };
}

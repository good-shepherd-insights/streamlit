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

// Fixture intent values for cf-router tests. One source of truth: tests
// reference APP/HOSTNAME/PORT/INTENT_ID instead of repeating literals.
import type { Intent } from "../../src/core.js";
import { signIntent } from "../../src/intent.js";

export const INTENT_ID = "intent-1";
export const TEST_APP = "ledger";
export const TEST_HOSTNAME = "ledger.apps.example.test";
export const TEST_PORT = 8502;
export const NOW = 1_750_000_000_000;
export const TEST_IMAGE = "registry.test/ledger:v7";

/** CF-shaped bodies shared by tests; tun id derived from the conf fixture. */
export const TEST_TUNNEL_ID = "tun_test_789";
export const CFARGO_TARGET = `${TEST_TUNNEL_ID}.cfargotunnel.com`;
export const TEST_DNS_ID = "dns-42";
export const service = (port = TEST_PORT) => (`http://localhost:${port}` as const);
export const dnsRecordBody = () => ({
  id: TEST_DNS_ID,
  type: "CNAME" as const,
  name: TEST_HOSTNAME,
  content: `${TEST_TUNNEL_ID}.cfargotunnel.com`,
  proxied: true as const,
});

export function makeIntent(overrides: Partial<Intent> = {}): Intent {
  return {
    id: INTENT_ID,
    action: "create",
    target: "home",
    app: TEST_APP,
    hostname: TEST_HOSTNAME,
    port: TEST_PORT,
    issued_at: NOW,
    ...overrides,
  };
}

/** Intent signed under `secret`; the mac covers the canonical body sans mac. */
export async function withMac(intent: Intent, secret: string): Promise<Intent> {
  const { mac: _unset, ...bare } = intent;
  return { ...bare, mac: await signIntent(secret, JSON.stringify(bare), bare.issued_at) };
}

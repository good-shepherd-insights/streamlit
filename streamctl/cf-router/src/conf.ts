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

// cf-router shared config — PRD 5b.i.
// The ONLY place conf key names are defined. Both backends (Worker wrangler
// vars, homeserver streamctl.conf) reuse the same key names so switching
// backends is a conf edit, never code. CF endpoint URLs are composed here
// from CF_API_BASE + the CF zone/account/tunnel ids, keeping every other
// module free of endpoint literals.

export interface Config {
  // Cloudflare API wiring
  CF_API_BASE: string;
  CF_ZONE_ID: string;
  CF_ACCOUNT_ID: string;
  CF_TUNNEL_ID: string;
  // CF endpoints composed from the fields above
  CF_DNS_RECORDS_URL: string;
  CF_TUNNEL_CONFIG_URL: string;
  CF_CONTAINER_URL: string;
  // routing/auth/timing (same shape on both backends)
  TUNNEL_SERVICE_PREFIX: string;
  PUBLIC_DOMAIN: string;
  API_HOSTNAME: string;
  // Router API route root; composed onto API_HOSTNAME by the I/O adapters so
  // no adapter ever spells the route literal itself.
  INTENTS_PATH: string;
  HMAC_SECRET: string;
  REPLAY_WINDOW_SEC: number;
  ROUTE_WAIT_SEC: number;
  APPLY_MAX_RETRIES: number;
  RECONCILE_INTERVAL_SEC: number;
}

/** Keys parsed as numbers; every other key stays a string. */
const NUMERIC_KEYS = [
  "REPLAY_WINDOW_SEC",
  "ROUTE_WAIT_SEC",
  "APPLY_MAX_RETRIES",
  "RECONCILE_INTERVAL_SEC",
] as const;

const REQUIRED_KEYS = [
  "CF_API_BASE",
  "CF_ZONE_ID",
  "CF_ACCOUNT_ID",
  "CF_TUNNEL_ID",
  "PUBLIC_DOMAIN",
  "API_HOSTNAME",
  "HMAC_SECRET",
] as const;

const NUMERIC_DEFAULTS: Record<(typeof NUMERIC_KEYS)[number], number> = {
  REPLAY_WINDOW_SEC: 300,
  ROUTE_WAIT_SEC: 30,
  APPLY_MAX_RETRIES: 3,
  RECONCILE_INTERVAL_SEC: 3600,
};

/** Parse a key=value conf body: `#` comments, blank lines, `key = value` spacing. */
function parseConfFile(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of body.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    out[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
  }
  return out;
}

/**
 * Load the shared Config from an env-style record (wrangler vars / env
 * object, possibly carrying non-string bindings, which are ignored) or
 * from a key=value file body. Unknown keys are ignored. A supplied
 * CF_*_URL / INTENTS_PATH wins; otherwise the value is composed from the
 * identity fields above.
 */
export function loadConf(input: string | Record<string, unknown>): Config {
  const raw: Record<string, string> =
    typeof input === "string"
      ? parseConfFile(input)
      : // Bindings (KV namespaces, queues) ride on the same env object as the
        // vars; only the string-valued entries are conf-relevant.
        (Object.fromEntries(
          Object.entries(input).filter(([, v]) => typeof v === "string"),
        ) as Record<string, string>);
  for (const key of REQUIRED_KEYS as readonly string[]) {
    if (!raw[key]) throw new Error(`missing required conf key: ${key}`);
  }
  const numbers: Record<string, number> = {};
  for (const key of NUMERIC_KEYS as readonly string[]) {
    const supplied = raw[key];
    numbers[key] = supplied === undefined ? NUMERIC_DEFAULTS[key as keyof typeof NUMERIC_DEFAULTS] : Number(supplied);
    // Guard against non-numeric conf values; defaults are also validated here.
    if (!Number.isFinite(numbers[key])) throw new Error(`conf key ${key} must be numeric, got: ${supplied}`);
  }
  // REQUIRED_KEYS membership above guarantees these exist at runtime; the
  // Record<string,string> index signature hides that from tsc, so assert here.
  const cf = raw as Record<"CF_API_BASE" | "CF_ZONE_ID" | "CF_ACCOUNT_ID" | "CF_TUNNEL_ID", string>;
  const nums = numbers as Record<"REPLAY_WINDOW_SEC" | "ROUTE_WAIT_SEC" | "APPLY_MAX_RETRIES" | "RECONCILE_INTERVAL_SEC", number>;
  const cf2 = raw as Record<"PUBLIC_DOMAIN" | "API_HOSTNAME" | "HMAC_SECRET", string>;
  const { CF_API_BASE, CF_ZONE_ID, CF_ACCOUNT_ID, CF_TUNNEL_ID } = cf;
  return {
    CF_API_BASE,
    CF_ZONE_ID,
    CF_ACCOUNT_ID,
    CF_TUNNEL_ID,
    CF_DNS_RECORDS_URL: raw.CF_DNS_RECORDS_URL ?? `${CF_API_BASE}/zones/${CF_ZONE_ID}/dns_records`,
    CF_TUNNEL_CONFIG_URL:
      raw.CF_TUNNEL_CONFIG_URL ??
      `${CF_API_BASE}/accounts/${CF_ACCOUNT_ID}/cfd_tunnel/${CF_TUNNEL_ID}/configurations`,
    CF_CONTAINER_URL: raw.CF_CONTAINER_URL ?? `${CF_API_BASE}/accounts/${CF_ACCOUNT_ID}/containers/apps`,
    TUNNEL_SERVICE_PREFIX: raw.TUNNEL_SERVICE_PREFIX ?? `http://localhost:`,
    PUBLIC_DOMAIN: cf2.PUBLIC_DOMAIN,
    API_HOSTNAME: cf2.API_HOSTNAME,
    INTENTS_PATH: raw.INTENTS_PATH ?? "/v1/intents",
    HMAC_SECRET: cf2.HMAC_SECRET,
    REPLAY_WINDOW_SEC: nums.REPLAY_WINDOW_SEC,
    ROUTE_WAIT_SEC: nums.ROUTE_WAIT_SEC,
    APPLY_MAX_RETRIES: nums.APPLY_MAX_RETRIES,
    RECONCILE_INTERVAL_SEC: nums.RECONCILE_INTERVAL_SEC,
  };
}

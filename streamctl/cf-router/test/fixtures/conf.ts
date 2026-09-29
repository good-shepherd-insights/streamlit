// Fixture conf definitions for cf-router tests.
// RAW_CONF mirrors an env-style source (wrangler vars / conf object);
// the *.conf file fixture exercises the key=value file mode of loadConf.
import { loadConf, type Config } from "../../src/conf.js";

export const RAW_CONF: Record<string, string> = {
  CF_API_BASE: "https://api.cloudflare.test/client/v4",
  CF_ZONE_ID: "zone_test_123",
  CF_ACCOUNT_ID: "acct_test_456",
  CF_TUNNEL_ID: "tun_test_789",
  PUBLIC_DOMAIN: "apps.example.test",
  API_HOSTNAME: "router.example.test",
  HMAC_SECRET: "test-hmac-secret",
  REPLAY_WINDOW_SEC: "300",
  ROUTE_WAIT_SEC: "30",
  APPLY_MAX_RETRIES: "3",
  RECONCILE_INTERVAL_SEC: "3600",
};

export const WRONG_SECRET = "other-secret";

export function makeConf(overrides: Partial<Config> = {}): Config {
  const base = loadConf(RAW_CONF);
  return { ...base, ...overrides };
}

// Env-style record with one required key missing, for loadConf validation tests.
export const RAW_CONF_MISSING_ZONE: Record<string, string> = Object.fromEntries(
  Object.entries(RAW_CONF).filter(([key]) => key !== "CF_ZONE_ID"),
);
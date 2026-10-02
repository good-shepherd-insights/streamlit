// cf-router intent auth — PRD 5b.ii auth().
// HMAC-SHA256 (WebCrypto; identical in Workers and Node) over
// body + issued_at. The replay window comes from Config so both backends
// reject stale replays through the same gate. Failures return 401-shaped
// results; adapters translate them straight onto the HTTP response.

import type { Config } from "./conf.js";

export type AuthConfig = Pick<Config, "HMAC_SECRET" | "REPLAY_WINDOW_SEC">;

/** Wire payload: the exact request body, its issued_at, and the hex mac. */
export interface SignedPayload {
  body: string;
  issued_at: number;
  mac?: string | null;
}

export type VerifyResult = { ok: true } | { ok: false; status: 401; reason: string };

function fail(reason: string): VerifyResult {
  return { ok: false, status: 401, reason };
}

function toHex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

/** Hex HMAC-SHA256 of `body + issued_at` under the caller's secret. */
export async function signIntent(secret: string, body: string, issued_at: number): Promise<string> {
  const mac = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    new TextEncoder().encode(body + String(issued_at)),
  );
  return toHex(mac);
}

/** Constant-time hex comparison so mac checks do not leak prefix length. */
function timingSafeHexEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Gate a signed payload. Order: unsigned -> stale (|now - issued_at| must
 * be within REPLAY_WINDOW_SEC, boundary inclusive) -> mac mismatch.
 */
export async function verifyIntent(
  payload: SignedPayload,
  now: number,
  cfg: AuthConfig,
): Promise<VerifyResult> {
  if (!payload.mac) {
    return fail("unsigned intent: mac header is required");
  }
  if (Math.abs(now - payload.issued_at) > cfg.REPLAY_WINDOW_SEC * 1000) {
    return fail("stale intent: issued_at outside REPLAY_WINDOW_SEC");
  }
  const expected = await signIntent(cfg.HMAC_SECRET, payload.body, payload.issued_at);
  if (!timingSafeHexEqual(payload.mac.toLowerCase(), expected)) {
    return fail("bad hmac: wrong secret or tampered body");
  }
  return { ok: true };
}
// Table-driven tests for HMAC intent auth (src/intent.ts).
// Gate per PRD 5b.ii: unsigned / stale / wrong-secret -> 401; valid -> ok.
import { describe, it, expect } from "vitest";
import { signIntent, verifyIntent, type SignedPayload } from "../src/intent.js";
import { makeConf, WRONG_SECRET } from "./fixtures/conf.js";

const NOW = 1_750_000_000_000; // fixed epoch ms for deterministic replay-window tests

interface VerifyCase {
  name: string;
  build: () => Promise<SignedPayload>;
  verifyAt?: number;
  expectOk: boolean;
  reasonMatch?: RegExp;
}

async function signed(secret: string, issuedAt: number, windowOffsetMs = 0): Promise<SignedPayload> {
  const body = JSON.stringify({ intent: "test-payload" });
  return {
    body,
    issued_at: issuedAt,
    mac: await signIntent(secret, body, issuedAt),
  };
}

const CASES: VerifyCase[] = [
  {
    name: "fresh signed body -> ok",
    build: () => signed(makeConf().HMAC_SECRET as string, NOW),
    expectOk: true,
  },
  {
    name: "unsigned body (no mac) -> 401",
    build: async () => ({ body: JSON.stringify({ intent: "test-payload" }), issued_at: NOW }),
    expectOk: false,
    reasonMatch: /unsigned|missing/i,
  },
  {
    name: "stale issued_at (beyond REPLAY_WINDOW_SEC) -> 401",
    build: () => signed(makeConf().HMAC_SECRET as string, NOW - 5 * 60 * 1000 - 1),
    expectOk: false,
    reasonMatch: /stale|replay|window/i,
  },
  {
    name: "issued_at in the future beyond window -> 401",
    build: () => signed(makeConf().HMAC_SECRET as string, NOW + 6 * 60 * 1000),
    expectOk: false,
    reasonMatch: /stale|replay|window/i,
  },
  {
    name: "signed with wrong secret -> 401",
    build: () => signed(WRONG_SECRET, NOW),
    expectOk: false,
    reasonMatch: /hmac|secret|signature/i,
  },
  {
    name: "mac mangled after signing -> 401",
    build: async () => {
      const payload = await signed(makeConf().HMAC_SECRET as string, NOW);
      return { ...payload, mac: (payload.mac as string).replace(/^.{2}/, "ff") };
    },
    expectOk: false,
    reasonMatch: /hmac|secret|signature/i,
  },
];

describe("verifyIntent", () => {
  for (const testCase of CASES) {
    it(testCase.name, async () => {
      const result = await verifyIntent(await testCase.build(), testCase.verifyAt ?? NOW, makeConf());
      if (testCase.expectOk) {
        expect(result.ok).toBe(true);
        if (!result.ok) throw new Error("unreachable: expected ok result");
      } else {
        expect(result.ok).toBe(false);
        if (result.ok) throw new Error("unreachable: expected failure result");
        expect(result.status).toBe(401);
        expect(result.reason).toMatch(testCase.reasonMatch as RegExp);
      }
    });
  }

  it("accepts a body issued exactly at the replay window boundary", async () => {
    const cfg = makeConf();
    const payload = await signed(cfg.HMAC_SECRET as string, NOW - (cfg.REPLAY_WINDOW_SEC as number) * 1000);
    const result = await verifyIntent(payload, NOW, cfg);
    expect(result.ok).toBe(true);
  });
});
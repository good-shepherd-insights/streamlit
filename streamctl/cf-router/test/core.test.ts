// Table-driven tests for the shared core (src/core.ts), six PRD 5b.ii functions.
// The mock fetch answers CF-shaped JSON: {success, errors, result}.
import { describe, it, expect } from "vitest";
import {
  UnsupportedTargetError,
  apply_create,
  apply_destroy,
  auth,
  get_desired_state,
  intent_status,
  reconcile,
  type Intent,
  type IntentRecord,
  type RouterStore,
} from "../src/core.js";
import { makeConf } from "./fixtures/conf.js";
import { CFARGO_TARGET, dnsRecordBody, INTENT_ID, makeIntent, NOW, service, TEST_APP, TEST_DNS_ID, TEST_HOSTNAME, TEST_IMAGE, TEST_PORT, TEST_TUNNEL_ID } from "./fixtures/intent.js";
import { recordingFetch, type MockResponse } from "./helpers.js";

// Route helper: answers every GET the core composes from conf URLs with
// CF-shaped success empties (no ingress rules, no records, no instances).
function seedEmptyReads(url: string, method: string): MockResponse {
  if (method !== "GET") return { result: null };
  if (url.includes("/dns_records")) return { result: [] };
  if (url.includes("/configurations")) return { result: { ingress: [] } };
  if (url.includes("/containers")) return { result: [] };
  return { result: null };
}

describe("get_desired_state", () => {
  it("home target: composes tunnel-config + dns-record GETs from Config fields and returns current+desired", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);

    expect(state.desired).toHaveLength(2);
    const kinds = state.desired.map((r) => r.kind);
    expect(kinds).toEqual(["tunnel_ingress", "dns_record"]);
    expect(state.desired[0]).toEqual({
      kind: "tunnel_ingress",
      ref: TEST_HOSTNAME,
      props: { hostname: TEST_HOSTNAME, service: service() },
    });
    expect(state.desired[1]?.props).toEqual({
      type: "CNAME",
      name: TEST_HOSTNAME,
      content: CFARGO_TARGET,
      proxied: true,
    });
    expect(state.current).toHaveLength(0);
    // No endpoint literals: URLs are composed from conf fields.
    expect(mockFetch.calls[0]?.url).toBe(
      `${cfg.CF_TUNNEL_CONFIG_URL as string}`,
    );
    expect(mockFetch.calls[1]?.url).toBe(
      `${cfg.CF_DNS_RECORDS_URL as string}?type=CNAME&name=${TEST_HOSTNAME}`,
    );
  });

  it("home target: existing resources land in current (read-first)", async () => {
    const mockFetch = recordingFetch();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/dns_records"),
      {
        result: [
          {
            id: TEST_DNS_ID,
            type: "CNAME",
            name: TEST_HOSTNAME,
            content: CFARGO_TARGET,
            proxied: true,
          },
        ],
      },
    );
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/configurations"),
      {
        result: { ingress: [{ hostname: TEST_HOSTNAME, service: service() }] },
      },
    );
    mockFetch.respondTo(() => true, { result: null });

    const state = await get_desired_state(makeIntent(), makeConf(), mockFetch);
    expect(state.current).toHaveLength(2);
    const dns = state.current.find((r) => r.kind === "dns_record");
    expect(dns?.props).toMatchObject({ id: TEST_DNS_ID, type: "CNAME" });
  });

  it("container target: desired is the instance row, current read by app", async () => {
    const mockFetch = recordingFetch();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/containers"),
      { result: [{ name: TEST_APP, image: TEST_IMAGE, status: "healthy" }] },
    );
    mockFetch.respondTo(() => true, { result: null });

    const state = await get_desired_state(
      makeIntent({ target: "container", container_image: TEST_IMAGE, hostname: undefined }),
      makeConf(),
      mockFetch,
    );
    expect(state.desired).toEqual([
      {
        kind: "container_instance",
        ref: TEST_APP,
        props: { name: TEST_APP, image: TEST_IMAGE },
      },
    ]);
    expect(state.current).toHaveLength(1);
  });

  const unsupported: { target: string }[] = [{ target: "bogus" }, { target: "" }];
  for (const { target } of unsupported) {
    it(`unsupported target "${target}" raises UnsupportedTargetError`, async () => {
      const mockFetch = recordingFetch();
      await expect(
        get_desired_state(makeIntent({ target: target as Intent["target"] }), makeConf(), mockFetch),
      ).rejects.toThrow(UnsupportedTargetError);
    });
  }
});

describe("apply_create (diff-based, idempotent)", () => {
  it("applies the missing set only and audits", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    // GETs return empty; create routes echo success.
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));
    const state = await get_desired_state(makeIntent(), cfg, mockFetch);

    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);

    expect(result.status).toBe("done");
    expect(result.applied).toHaveLength(2);
    expect(result.existing).toHaveLength(0);
    expect(result.failed_reason).toBeUndefined();
    expect(result.audit).toHaveLength(1);
    expect(result.audit[0]).toMatchObject({ action: "create", app: TEST_APP });

    // One PUT for tunnel config, one POST for the CNAME.
    expect(mockFetch.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
    expect(mockFetch.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    const put = mockFetch.calls.find((c) => c.method === "PUT");
    expect(put?.url).toBe(cfg.CF_TUNNEL_CONFIG_URL);

    // DNS POST body carries the composed CNAME content and intent hostname.
    const post = mockFetch.calls.find((c) => c.method === "POST");
    expect(post?.url).toBe(cfg.CF_DNS_RECORDS_URL);
    expect(JSON.parse(post?.body as string).type).toBe("CNAME");
  });

  it("MANDATORY idempotency: second apply_create with same desired state performs ZERO new API calls and returns done(existing)", async () => {
    const cfg = makeConf();

    // First pass: empty server -> applies both resources.
    const mockFetch = recordingFetch();
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));
    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const first = await apply_create(makeIntent(), state.current, cfg, mockFetch);
    expect(first.status).toBe("done");

    // Second pass: server now holds what the first pass created.
    mockFetch.reset();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/dns_records"),
      {
        result: [
          {
            id: TEST_DNS_ID,
            type: "CNAME",
            name: TEST_HOSTNAME,
            content: CFARGO_TARGET,
            proxied: true,
          },
        ],
      },
    );
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/configurations"),
      {
        result: { ingress: [{ hostname: TEST_HOSTNAME, service: service() }] },
      },
    );
    mockFetch.respondTo(() => true, { result: null });
    const state2 = await get_desired_state(makeIntent(), cfg, mockFetch);
    const current2 = state2.current;
    expect(current2).toHaveLength(2);
    expect(state2.desired).toEqual(current2);

    // The re-apply itself must make ZERO API calls.
    mockFetch.reset();
    const second = await apply_create(makeIntent(), current2, cfg, mockFetch);

    expect(mockFetch.calls).toHaveLength(0);
    expect(second.status).toBe("done");
    expect(second.applied).toHaveLength(0);
    expect(second.existing).toEqual(current2);
    expect(second.audit).toHaveLength(0);
  });

  it("preserves existing resources: PUT of tunnel config merges current rules with the new rule", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/configurations"),
      { result: { ingress: [{ hostname: "other.apps.example.test", service: "http://other:8500" }] } },
    );
    mockFetch.respondTo((url, method) => method === "GET" && url.includes("/dns_records"), { result: [] });
    mockFetch.respondTo(() => true, { result: null });

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);

    const put = mockFetch.calls.find((c) => c.method === "PUT");
    const ingress = JSON.parse(put?.body as string).config.ingress;
    expect(ingress).toHaveLength(2); // current rule kept + the new rule appended
    expect(ingress).toEqual(
      expect.arrayContaining([
        { hostname: "other.apps.example.test", service: "http://other:8500" },
        { hostname: TEST_HOSTNAME, service: service() },
      ]),
    );
    expect(result.status).toBe("done");
  });

  it("applies only the missing resource when one side already exists", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/dns_records"),
      {
        result: [
          {
            id: TEST_DNS_ID,
            type: "CNAME",
            name: TEST_HOSTNAME,
            content: CFARGO_TARGET,
            proxied: true,
          },
        ],
      },
    );
    mockFetch.respondTo((url, method) => method === "GET" && url.includes("/configurations"), {
      result: { ingress: [] },
    });
    mockFetch.respondTo(() => true, { result: null });

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);

    // DNS already present and matching: diff keeps it as existing; only ingress applied.
    expect(result.applied.map((r) => r.kind)).toEqual(["tunnel_ingress"]);
    expect(result.existing.map((r) => r.kind)).toEqual(["dns_record"]);
    expect(mockFetch.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("retries a flaky create within APPLY_MAX_RETRIES", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo((url, method) => method === "GET", seedEmptyReads("x", "GET"));
    // Failing DNS POSTs surface the CF error verbatim; the failing attempt
    // counts against APPLY_MAX_RETRIES, so "transient" wins only if retries exist.
    mockFetch.respondTo((url, method) => method === "POST" && url.includes("/dns_records"), {
      ok: false,
      errors: [{ code: 9103, message: "transient" }],
    });

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);

    expect(result.status).toBe("failed");
    expect(result.failed_reason).toBe("transient");
    const dnsPosts = mockFetch.calls.filter((c) => c.method === "POST");
    expect(dnsPosts).toHaveLength(cfg.APPLY_MAX_RETRIES);
  });

  it("retries a flaky create until it succeeds within APPLY_MAX_RETRIES", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));
    // First two POSTs to the DNS endpoint fail ("transient"), then one succeeds.
    mockFetch.respondTo(
      (url, method) => method === "POST",
      (callsSoFar) =>
        callsSoFar < 2
          ? { ok: false, errors: [{ code: 9103, message: "transient" }] }
          : { result: { id: TEST_DNS_ID } },
    );

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);
    expect(result.status).toBe("done");
  });

  it("surfaces the CF error verbatim when the API rejects", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo((url, method) => method === "GET", seedEmptyReads("x", "GET"));
    mockFetch.respondTo(
      (url, method) => method === "PUT",
      { ok: false, errors: [{ code: 7502, message: "ingress rule invalid" }] },
    );

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_create(makeIntent(), state.current, cfg, mockFetch);
    expect(result.status).toBe("failed");
    expect(result.failed_reason).toBe("ingress rule invalid");
    expect(result.audit).toHaveLength(0);
  });
});

describe("apply_destroy (inverse diff)", () => {
  it("removes exactly the desired resources that exist, via composed DELETE/PUT", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    // Current has both resources (as if create ran earlier).
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/dns_records"),
      {
        result: [
          {
            id: TEST_DNS_ID,
            type: "CNAME",
            name: TEST_HOSTNAME,
            content: CFARGO_TARGET,
            proxied: true,
          },
        ],
      },
    );
    mockFetch.respondTo((url, method) => method === "GET" && url.includes("/configurations"), {
      result: {
        ingress: [
          { hostname: TEST_HOSTNAME, service: service() },
          { hostname: "other.apps.example.test", service: "http://other:8500" },
        ],
      },
    });
    mockFetch.respondTo(() => true, { result: null });

    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_destroy(makeIntent({ action: "destroy" }), state.current, cfg, mockFetch);

    expect(result.status).toBe("done");
    expect(result.removed).toHaveLength(2);
    // DNS delete URL composed from conf list endpoint + record id.
    expect(mockFetch.calls.filter((c) => c.method === "DELETE")).toEqual([
      { url: `${cfg.CF_DNS_RECORDS_URL as string}/dns-42`, method: "DELETE", body: undefined },
    ]);
    // Ingress rule removed by re-PUT of the remaining rules.
    const put = mockFetch.calls.find((c) => c.method === "PUT");
    expect(JSON.parse(put?.body as string).config.ingress).toEqual([
      { hostname: "other.apps.example.test", service: "http://other:8500" },
    ]);
    expect(result.audit[0]).toMatchObject({ action: "destroy", app: TEST_APP });
  });

  it("is idempotent: destroying when nothing exists does zero mutations", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));
    const state = await get_desired_state(makeIntent(), cfg, mockFetch);
    const result = await apply_destroy(makeIntent({ action: "destroy" }), state.current, cfg, mockFetch);
    expect(result.status).toBe("done");
    expect(result.removed).toHaveLength(0);
    expect(mockFetch.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });
});

describe("intent_status", () => {
  const rows: IntentRecord[] = [
    { id: INTENT_ID, status: "done", intent: makeIntent(), hostname: TEST_HOSTNAME },
  ];
  const store: RouterStore = {
    read: async (id) => rows.find((r) => r.id === id),
    where: async () => rows,
    write: async () => undefined,
  };

  it("returns the store row by id", async () => {
    const row = await intent_status(INTENT_ID, store);
    expect(row?.status).toBe("done");
  });

  it("returns undefined for an unknown id", async () => {
    expect(await intent_status("nope", store)).toBeUndefined();
  });
});

describe("reconcile", () => {
  it("re-applies drift through the same apply path and flips status", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(() => true, seedEmptyReads("x", "GET"));
    const writes: IntentRecord[] = [];
    const store: RouterStore = {
      read: async () => undefined,
      where: async () => [{ id: INTENT_ID, status: "done", intent: makeIntent() }],
      write: async (row) => {
        writes.push(row);
      },
    };

    const summary = await reconcile(cfg, store, mockFetch);
    expect(summary.checked).toBe(1);
    expect(summary.repaired).toEqual([INTENT_ID]);
    expect(mockFetch.calls.filter((c) => c.method === "POST")).toHaveLength(1);
    expect(mockFetch.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
    expect(writes[0]?.status).toBe("done");
  });

  it("leaves intent untouched when there is no drift", async () => {
    const cfg = makeConf();
    const mockFetch = recordingFetch();
    mockFetch.respondTo(
      (url, method) => method === "GET" && url.includes("/dns_records"),
      {
        result: [
          {
            id: TEST_DNS_ID,
            type: "CNAME",
            name: TEST_HOSTNAME,
            content: CFARGO_TARGET,
            proxied: true,
          },
        ],
      },
    );
    mockFetch.respondTo((url, method) => method === "GET" && url.includes("/configurations"), {
      result: { ingress: [{ hostname: TEST_HOSTNAME, service: service() }] },
    });
    mockFetch.respondTo(() => true, { result: null });
    const store: RouterStore = {
      read: async () => undefined,
      where: async () => [{ id: INTENT_ID, status: "done", intent: makeIntent() }],
      write: async () => undefined,
    };

    const summary = await reconcile(cfg, store, mockFetch);
    expect(summary.repaired).toEqual([]);
    expect(mockFetch.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });
});

describe("auth", () => {
  it("passes a properly signed intent and rejects an unsigned one with 401", async () => {
    const { signIntent } = await import("../src/intent.js");
    const cfg = makeConf();
    const bare = makeIntent();
    const mac = await signIntent(cfg.HMAC_SECRET, JSON.stringify(bare), bare.issued_at);
    expect((await auth({ ...bare, mac }, bare.issued_at, cfg)).ok).toBe(true);

    const unsigned = await auth({ ...bare }, bare.issued_at, cfg);
    expect(unsigned.ok).toBe(false);
    if (!unsigned.ok) {
      expect(unsigned.status).toBe(401);
    }
  });
});
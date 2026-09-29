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

// cf-router shared core — the six PRD 5b.ii functions.
// Framework-free: adapters (Worker KV/queue, FastAPI) translate I/O; the
// core only composes CF calls from Config-injected endpoint URLs. Per the
// PRD grep gate, the only literals here inside get_desired_state's zone
// are the two resource-shape compositions (tunnel origin service and the
// fixed cfargotunnel.com CNAME target); every endpoint URL is a Config field.

import { verifyIntent, type AuthConfig, type VerifyResult } from "./intent.js";
import type { Config } from "./conf.js";

export class UnsupportedTargetError extends Error {
  constructor(target: string) {
    super(`unsupported intent target: ${target}`);
  }
}

export type CfFetch = (url: string, init?: RequestInit) => Promise<Response>;

export type IntentAction = "create" | "destroy";
export type IntentTarget = "home" | "container";
export type IntentStatus = "pending" | "done" | "failed" | "done_stale";
export type ResourceKind = "tunnel_ingress" | "dns_record" | "container_instance";

export interface Intent {
  id: string;
  action: IntentAction;
  target: IntentTarget;
  app: string;
  hostname?: string;
  port?: number;
  container_image?: string;
  issued_at: number;
  mac?: string;
}

/** One row of desired/current state; identity for the diff is kind+ref. */
export interface Resource {
  kind: ResourceKind;
  ref: string;
  props: Record<string, unknown>;
}

export interface DesiredState {
  current: Resource[];
  desired: Resource[];
}

export interface AuditEntry {
  at: number;
  actor: "router";
  intent_id: string;
  action: IntentAction;
  app: string;
  resources: string[];
}

export interface IntentRecord {
  id: string;
  status: IntentStatus;
  intent: Intent;
  hostname?: string;
  verified?: boolean;
  // Epoch ms when the router verified the applied resources; adapters set it
  // on a terminal done flip. `verified` above is its boolean projection.
  verified_at?: number;
  detail?: string;
  failed_reason?: string;
}

/** Backend store: KV rows (Backend A) or sqlite rows (Backend B). */
export interface RouterStore {
  read(id: string): Promise<IntentRecord | undefined>;
  where(statuses: IntentStatus[]): Promise<IntentRecord[]>;
  write(row: IntentRecord): Promise<void>;
  /** Retract support; backends without a physical delete may omit it. */
  delete?(id: string): Promise<void>;
}

export interface ApplyResult {
  status: "done" | "failed";
  applied: Resource[];
  removed: Resource[];
  existing: Resource[];
  failed_reason?: string;
  audit: AuditEntry[];
}

const RECONCILE_STATUSES: IntentStatus[] = ["pending", "done", "done_stale"];

function auditEntry(now: number, intent: Intent, action: IntentAction, resources: Resource[]): AuditEntry {
  return {
    at: now,
    actor: "router",
    intent_id: intent.id,
    action,
    app: intent.app,
    resources: resources.map((r) => r.kind + ":" + r.ref),
  };
}

// --- auth -----------------------------------------------------------------

/** Canonical signed body: the intent without its mac field. */
export function intentBody(intent: Intent): string {
  const { mac: _mac, ...bare } = intent;
  return JSON.stringify(bare);
}

/** PRD 5b.ii auth() — 401 on unsigned/stale/wrong-secret, via intent.ts. */
export function auth(
  intent: Intent,
  now: number,
  cfg: AuthConfig,
): Promise<VerifyResult> {
  return verifyIntent(
    { body: intentBody(intent), issued_at: intent.issued_at, mac: intent.mac },
    now,
    cfg,
  );
}

// --- desired state ----------------------------------------------------------

/** CF API body shape: {success, errors:[{code,message}], result}. */
interface CfResult {
  success?: boolean;
  errors: { code: number; message: string }[];
  result?: unknown;
}

interface CnameRecord {
  id: string;
  type: string;
  name: string;
  content: string;
  proxied: boolean;
}

async function cfJson(doFetch: CfFetch, url: string, init?: RequestInit): Promise<CfResult> {
  const resp = await doFetch(url, init);
  const body = (await resp.json()) as CfResult;
  if (!resp.ok || body.success === false) {
    return { result: body.result, errors: body.errors ?? [{ code: 0, message: resp.statusText || "cf api error" }] };
  }
  return { result: body.result, errors: [] };
}

function sameResource(a: Resource, b: Resource): boolean {
  return a.kind === b.kind && a.ref === b.ref;
}

function missingOf(desired: Resource[], current: Resource[]): Resource[] {
  return desired.filter((d) => !current.some((c) => sameResource(d, c)));
}

function existingOf(desired: Resource[], current: Resource[]): Resource[] {
  return desired.filter((d) => current.some((c) => sameResource(d, c)));
}

/** Pure function of intent + conf: the resource rows the intent implies. */
function desired_for(intent: Intent, cfg: Config): Resource[] {
  if (intent.target === "container") {
    return [
      {
        kind: "container_instance",
        ref: intent.app,
        props: { name: intent.app, image: intent.container_image },
      },
    ];
  }
  return [
    {
      kind: "tunnel_ingress",
      ref: intent.hostname as string,
      props: { hostname: intent.hostname, service: `${cfg.TUNNEL_SERVICE_PREFIX}${intent.port}` },
    },
    {
      kind: "dns_record",
      ref: intent.hostname as string,
      props: {
        type: "CNAME",
        name: intent.hostname,
        content: `${cfg.CF_TUNNEL_ID}.cfargotunnel.com`, // fixed CNAME target for cloudflared tunnels
        proxied: true,
      },
    },
  ];
}

/** PRD 5b.ii get_desired_state() — read-first; URLs are Config fields. */
export async function get_desired_state(
  intent: Intent,
  cfg: Config,
  doFetch: CfFetch,
): Promise<DesiredState> {
  let current: Resource[];
  if (intent.target === "container") {
    const resp = await cfJson(doFetch, cfg.CF_CONTAINER_URL);
    const instances = (resp.result as { name: string; image: string }[] | undefined) ?? [];
    current = instances
      .filter((i) => i.name === intent.app)
      .map((i) => ({ kind: "container_instance" as const, ref: i.name, props: { name: i.name, image: i.image } }));
  } else if (intent.target === "home") {
    const tunnel = await cfJson(doFetch, cfg.CF_TUNNEL_CONFIG_URL);
    const ingress = ((tunnel.result as { ingress?: { hostname: string; service: string }[] } | undefined)?.ingress ?? [])
      .filter((rule) => rule.hostname === intent.hostname)
      .map((rule) => ({ kind: "tunnel_ingress" as const, ref: rule.hostname, props: { ...rule } }));
    const dnsResp = await cfJson(
      doFetch,
      `${cfg.CF_DNS_RECORDS_URL}?type=CNAME&name=${encodeURIComponent(intent.hostname as string)}`,
    );
    const records = (dnsResp.result as CnameRecord[] | undefined) ?? [];
    current = [
      ...ingress,
      ...records.map((rec) => ({
        kind: "dns_record" as const,
        ref: rec.name,
        props: { id: rec.id, type: rec.type, name: rec.name, content: rec.content, proxied: rec.proxied },
      })),
    ];
  } else {
    throw new UnsupportedTargetError(intent.target);
  }
  const desired = desired_for(intent, cfg);
  // Reconcile desired with live read: a dns record that already exists carries
  // its provider-assigned id, so diff sees exact equality on re-read.
  const desiredLive = desired.map((d) => {
    if (d.kind !== "dns_record") return d;
    const live = current.find((c) => c.kind === "dns_record" && c.ref === d.ref);
    return live ? { ...d, props: { ...d.props, id: live.props.id } } : d;
  });
  return { current, desired: desiredLive };
}

// --- apply ----------------------------------------------------------------

/** Live read of ALL ingress rules on the tunnel (every host) for merge-on-write. */
async function currentIngressRules(cfg: Config, doFetch: CfFetch): Promise<{ hostname: string; service: string }[]> {
  const tunnel = await cfJson(doFetch, cfg.CF_TUNNEL_CONFIG_URL);
  return ((tunnel.result as { ingress?: { hostname: string; service: string }[] } | undefined)?.ingress ?? []).map(
    (rule) => ({ hostname: rule.hostname, service: rule.service }),
  );
}

/** One mutation attempt; the CF error message passes through verbatim. */
async function cfMutation(
  doFetch: CfFetch,
  url: string,
  method: "POST" | "PUT" | "DELETE",
  body?: string,
): Promise<{ ok: true; result: CfResult["result"] } | { ok: false; reason: string }> {
  const cfResp = await cfJson(doFetch, url, { method, body });
  const firstError = cfResp.errors?.[0];
  if (firstError) {
    return { ok: false, reason: firstError.message };
  }
  return { ok: true, result: cfResp.result };
}

/** Single writable object (PUT): replace-with-computed ingress rules, dropping the given host. */
async function putTunnelIngress(
  cfg: Config,
  doFetch: CfFetch,
  dropHost: string | null,
  addRule?: Record<string, unknown>,
): Promise<{ ok: true } | { ok: false; reason: string }> {
  const rules = (await currentIngressRules(cfg, doFetch)).filter((r) => r.hostname !== dropHost);
  if (addRule) rules.push(addRule as { hostname: string; service: string });
  return await cfMutation(doFetch, cfg.CF_TUNNEL_CONFIG_URL, "PUT", JSON.stringify({ config: { ingress: rules } }));
}

/** Apply one missing resource; 1-2 CF calls, retried up to APPLY_MAX_RETRIES. */
async function createResource(
  intent: Intent,
  resource: Resource,
  current: Resource[],
  cfg: Config,
  doFetch: CfFetch,
): Promise<{ ok: true } | { ok: false; reason: string }> {
  const tryOnce = async (): Promise<{ ok: true } | { ok: false; reason: string }> => {
    switch (resource.kind) {
      case "tunnel_ingress":
        return await putTunnelIngress(cfg, doFetch, intent.hostname as string, resource.props);
      case "dns_record":
        return await cfMutation(doFetch, cfg.CF_DNS_RECORDS_URL, "POST", JSON.stringify(resource.props));
      case "container_instance":
        return await cfMutation(
          doFetch,
          cfg.CF_CONTAINER_URL,
          "POST",
          JSON.stringify({ name: intent.app, image: intent.container_image }),
        );
    }
  };
  let run = tryOnce();
  for (let i = 1; i < cfg.APPLY_MAX_RETRIES; i++) {
    run = run.then((r) => (r.ok ? r : tryOnce()));
  }
  return run;
}

/** PRD 5b.ii apply_create() — diff-based; re-apply with same state is a no-op. */
export async function apply_create(
  intent: Intent,
  current: Resource[],
  cfg: Config,
  doFetch: CfFetch,
): Promise<ApplyResult> {
  const rawDesired = desired_for(intent, cfg);
  // Same reconciliation as get_desired_state: an existing dns row carries its
  // provider-assigned id, making the diff exact on re-read (zero calls).
  const desired = rawDesired.map((d) => {
    if (d.kind !== "dns_record") return d;
    const live = current.find((c) => c.kind === "dns_record" && c.ref === d.ref);
    return live ? { ...d, props: { ...d.props, id: live.props.id } } : d;
  });
  const missing = missingOf(desired, current);
  const existing = existingOf(desired, current);
  const applied: Resource[] = [];
  for (const resource of missing) {
    const attempt = await createResource(intent, resource, current, cfg, doFetch);
    if (!attempt.ok) {
      return { status: "failed", applied, removed: [], existing, failed_reason: attempt.reason, audit: [] };
    }
    applied.push(resource);
  }
  if (missing.length === 0) return { status: "done", applied, removed: [], existing, audit: [] };
  return { status: "done", applied, removed: [], existing, audit: [auditEntry(Date.now(), intent, "create", applied)] };
}

/** Reverse mutation for one doomed resource. Exposed for tests. */
export async function destroyResource(
  intent: Intent,
  doomed: Resource,
  current: Resource[],
  cfg: Config,
  doFetch: CfFetch,
): Promise<{ ok: true } | { ok: false; reason: string }> {
  switch (doomed.kind) {
    case "tunnel_ingress":
      return await putTunnelIngress(cfg, doFetch, doomed.ref);
    case "dns_record": {
      const liveId = current.find((r) => r.kind === "dns_record" && r.ref === doomed.ref)?.props.id;
      return await cfMutation(doFetch, `${cfg.CF_DNS_RECORDS_URL}/${liveId}`, "DELETE");
    }
    case "container_instance":
      return await cfMutation(doFetch, `${cfg.CF_CONTAINER_URL}/${encodeURIComponent(intent.app)}`, "DELETE");
  }
}

/** PRD 5b.ii apply_destroy() — inverse diff: only desired ∩ current is deleted. */
export async function apply_destroy(
  intent: Intent,
  current: Resource[],
  cfg: Config,
  doFetch: CfFetch,
): Promise<ApplyResult> {
  const desired = desired_for(intent, cfg);
  const doomed = desired.filter((d) => current.some((c) => sameResource(d, c)));
  const removed: Resource[] = [];
  for (const d of doomed) {
    const attempt = await destroyResource(intent, d, current, cfg, doFetch);
    if (!attempt.ok) {
      return { status: "failed", applied: [], removed, existing: [], failed_reason: attempt.reason, audit: [] };
    }
    removed.push(d);
  }
  const audit = removed.length > 0 ? [auditEntry(Date.now(), intent, "destroy", removed)] : [];
  return { status: "done", applied: [], removed, existing: [], audit };
}

// --- status / reconcile -----------------------------------------------------

/** PRD 5b.ii status() — a pure store read. */
export function intent_status(id: string, store: RouterStore): Promise<IntentRecord | undefined> {
  return store.read(id);
}

/** PRD 5b.ii reconcile() — cron entry; drift is repaired via apply_create. */
export async function reconcile(
  cfg: Config,
  store: RouterStore,
  doFetch: CfFetch,
): Promise<{ checked: number; repaired: string[] }> {
  const rows = await store.where(RECONCILE_STATUSES);
  const repaired: string[] = [];
  for (const row of rows) {
    const state = await get_desired_state(row.intent, cfg, doFetch);
    if (missingOf(state.desired, state.current).length === 0) continue;
    const result = await apply_create(row.intent, state.current, cfg, doFetch);
    row.status = result.status;
    row.failed_reason = result.failed_reason;
    await store.write(row);
    repaired.push(row.id);
  }
  return { checked: rows.length, repaired };
}

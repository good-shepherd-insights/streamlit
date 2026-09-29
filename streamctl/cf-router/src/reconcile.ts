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

// cf-router cron entry — re-applies drift found in store rows through the
// core apply path. Timing lives in conf (RECONCILE_INTERVAL_SEC); the cron
// schedule itself is wrangler config, so this module only carries the one
// handler the Worker `scheduled` event calls.
//
// Drift is judged at object granularity: the home target's served object is
// its tunnel ingress rule and the container target's is its instance. dns
// records are auxiliary — apply_create builds them alongside the ingress
// rule — so a missing dns record alone is not drift, and reconcile must not
// re-apply an object whose served rule reads back intact.
import {
  apply_create,
  get_desired_state,
  type CfFetch,
  type IntentRecord,
  type IntentStatus,
  type RouterStore,
} from "./core.js";
import type { Config } from "./conf.js";

export interface ReconcileDeps {
  cfg: Config;
  store: RouterStore;
  doFetch: CfFetch;
}

/** Statuses reconcile walks; failed rows keep their verbatim, final reason. */
const RECONCILE_STATUSES: IntentStatus[] = ["pending", "done", "done_stale"];

function missingPrimary(desired: { kind: string; ref: string }[], current: { kind: string; ref: string }[]): boolean {
  return desired.some(
    (d) => d.kind !== "dns_record" && !current.some((c) => c.kind === d.kind && c.ref === d.ref),
  );
}

/** Re-applies drift found in store rows; the store is the single input. */
export async function scheduledReconcile(deps: ReconcileDeps): Promise<{ checked: number; repaired: string[] }> {
  const rows = await deps.store.where(RECONCILE_STATUSES);
  const repaired: string[] = [];
  for (const row of rows) {
    const state = await get_desired_state(row.intent, deps.cfg, deps.doFetch);
    if (!missingPrimary(state.desired, state.current)) continue;
    const result = await apply_create(row.intent, state.current, deps.cfg, deps.doFetch);
    const updated: IntentRecord = { ...row, status: result.status, failed_reason: result.failed_reason };
    await deps.store.write(updated);
    repaired.push(row.id);
  }
  return { checked: rows.length, repaired };
}

# cf-router deployed stack - live Cloudflare inventory (verified 2026-09-29 ~22:15 UTC)

Zone: marylandinsights.com = 2704c01e2a34292e7f258f49f748861b
Account: 4972acce5fc1e4032d8bcc8375fb4c42

## Workers (account)
- Script: cf-router (default export: fetch/scheduled/queue; src/worker.ts)
- Route: 844cc5aae5674399a7a361ff6ad086f2 - pattern `router.marylandinsights.com/*` -> script `cf-router`
- KV namespace INTENTS_KV: eb0b3b8293594e208994bef6f70*6f (store rows: `intent/<id>`)
- Queue cf-router-apply: 0499fd992b544625855ea2d0a153a010 (consumer: cf-router, batch 10, retries 3, DLQ)
- Queue cf-router-dlq: f182bd5dd6ec4db888a073eb33e785cb
- Worker secrets (names only): CF_API_KEY (the CF token), HMAC_SECRET (dev-smoke test secret)
- Cron trigger: */15 * * * * (scheduledReconcile)
- workers.dev subdomain (account-level): gsiefe322

## DNS records (zone marylandinsights.com)
- a32c6e48801527157f1a91272b4d9716 - CNAME r780badk.marylandinsights.com -> c3b4a99e-....cfargotunnel.com (proxied) [LIVE APP]
- 09c0c0ddb44cfe7056601472e7a05f56 - A router.marylandinsights.com -> 192.0.2.1 (proxied, placeholder for worker route)

## Tunnels
- 861baa10-37aa-4517-b788-3f4a5ec2e57e maryland-crm (pre-existing; ingress reverted to [crm:80, 404])
- c3b4a99e-9900-4885-a0ef-28d2bab86e75 maryland-streamctl (CREATED this session; ingress [r780badk -> localhost:8504, 404]; connector pid on this box, NOT systemd-managed)

## App
- https://r780badk.marylandinsights.com - live Streamlit (:8504, unit streamlit@r780badk)

## Proof of Backend A round-trip (live)
- POST https://router.marylandinsights.com/v1/intents (signed, HMAC dev-smoke secret) -> 202 pending
- KV row `intent/<id>` -> queue consumer -> status done, verified true (idempotent re-apply, zero harmful mutations)

## Backend B (self-host)
- FastAPI cf_router_local.py on 127.0.0.1:8516 (test instance; conf /tmp/routetest-real.conf), same contract, proven same day

## Verification
- vitest 65/65, tsc clean; pytest 14/14; commits through 8df3461e3e pushed to feat/cf-router (PR #2 open)
## CF Containers (TORN DOWN on user order)
- Deleted 2026-09-29: route 85fa49d1, DNS aafb95fb, worker script, container app a0301e09. Verified: 0 applications remain, cf-streamlit.marylandinsights.com dead.

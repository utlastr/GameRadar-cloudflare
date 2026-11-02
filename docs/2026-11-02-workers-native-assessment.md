# Native Workers Migration Assessment

## Decision

The target is JavaScript/TypeScript on Cloudflare Workers, without Python or
user-managed Containers. This is a backend rewrite, not a language switch.
Core product behavior is portable, but complete parity has not been established.
Do not advertise the previous container proxy as a completed migration.

Application baseline: local stable snapshot `86a9380`. The original GitHub main
now points to `b9a0467`, whose tree is exactly the same as that snapshot.
Its different commit ID preserves the previous GitHub main history.

## Capability Review

| Capability | Finding | Required work |
| --- | --- | --- |
| Dashboard, detail pages, charts, map rendering | Frontend assets can be reused | Preserve paths, assets, response contracts and error states; verify screenshots and interactions |
| HTTP APIs in `radar/server.py` | Portable, not implemented in native Workers | Rewrite GET/POST handlers, validation, Access authorization and data access |
| Heat/rank/growth calculations, matching, summaries | Algorithms are portable | Reimplement Python semantics; compare fixed-input golden results, thresholds, IDs and time zones |
| Steam and YouTube HTTP API collection | Technically portable | Use fetch and secrets; preserve quotas, pagination, checkpoints, timestamps and error reporting |
| Other HTTP data sources | Conditional | Verify each endpoint and account requirement from Cloudflare; local success is not cloud acceptance evidence |
| Sources using `radar/cdp.py` and `radar/social/page.py` | Not portable to plain Workers as-is | A remote Worker cannot reach the workstation's localhost or inherit its authenticated browser; validate an authorized API or a separate managed-browser design |
| Continuous Python threads and sleeps | Execution model is incompatible | Replace loops with bounded durable tasks, checkpoints, alarms and scheduled reconciliation |
| Local SQLite and filesystem writes | Storage model is incompatible | Use D1 or SQLite-backed Durable Objects plus R2, with an explicit routing/query design |
| Cross-domain joins and full-dataset analysis | Not automatic after sharding | Keep related data together, use incremental aggregates and bound the data loaded per task |
| Windows tray, startup integration and desktop notifications | Not equivalent website features | Explicitly replace with website status and web notification behavior, or leave as desktop-only |
| Existing cloud code | Prototype, not full port | `RadarApp` currently forwards to a Python Container; `RadarShard` is storage/import scaffolding, not the complete application |

Keeping the same frontend files does not prove equivalent behavior. Every
backend route, collection job, setting, calculation and failure state needs an
explicit acceptance check.

## Proposed Native Architecture

- Worker: serve existing static assets and implement the API contract in
  TypeScript.
- Scheduled trigger: enqueue/check due work rather than run the entire radar
  inside one invocation.
- Durable Object scheduler: persist cursors, leases and retry state. Use small
  alarm-driven batches with idempotent writes and an independent recovery check.
- D1 or SQLite-backed Durable Objects: persist entities, samples, source health,
  archive membership and precomputed rankings. Choose the final partitioning
  only after measuring actual table sizes and query/read/write demand.
- R2: private exports, raw archives and backups, not a local writable filesystem.
- Browser-dependent sources: separate feasibility gate. Do not mark a source
  working if only its old cached data is visible.

This architecture uses managed Workers services, not a Python container.
If "Workers only" excludes even a managed browser service, browser-dependent
sources require usable HTTP APIs or cannot retain their current behavior.

## Limits That Change the Design

Official documentation checked on 2026-11-02:

- Ordinary Workers Free HTTP requests have 10 ms of CPU time, not 30 seconds.
  Paid HTTP requests default to 30 seconds and can be configured up to five
  minutes. Waiting for network/storage I/O is not active CPU time.
- Workers have a 128 MB per-isolate memory limit. Loading the approximately
  gigabyte-sized database into JavaScript memory is not a viable design.
- D1 Free has up to ten databases and a 500 MB per-database limit. Splitting
  databases does not automatically distribute existing joins or prevent
  uneven growth; indexes and storage headroom must be measured too.
- Alarms are at-least-once and have bounded automatic retries. A new invocation
  is not a way to turn one task into an unlimited-CPU or guaranteed-24-hour
  process. Persist progress, make writes idempotent and reconcile stalled jobs.
- Hourly flushing from volatile memory alone cannot guarantee sample survival
  after eviction or failure. Any such tradeoff must be explicit.

Account quotas, billing and actual workloads are separate from language
portability. No claim is made here that the full radar fits the free tier.

## Acceptance Gates

1. Inventory the frontend/API/settings/jobs and preserve the stable source.
2. Validate one real HTTP collector from Workers using authorized access.
3. Run ranking, matching and daily-increment golden comparisons on fixed data.
4. Migrate a bounded dataset and verify counts, keys, timestamps and joins.
5. Exercise interruption, duplicate jobs, checkpoints and stale-source alerts.
6. Compare desktop/mobile UI and interactions against the local stable version.
7. Verify every enabled source from the deployed environment and measure
   actual CPU, memory and storage usage before cutover.

Full implementation has not started as part of this assessment. The old
container code remains only as historical prototype material, and its default
npm deployment is blocked.

## Repository Workflow

Stable updates are made locally on main, tested and committed, then pushed to
main. Do not create another branch unless the user requests one. Fetch and
compare before pushing; never silently discard concurrent remote work.

SSH authorizes Git reads/writes to existing repositories. Creating a GitHub
repository uses `gh repo create` and requires GitHub CLI account authorization;
the Git receive protocol does not create a missing GitHub repository.

## Official References

- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/durable-objects/api/alarms/
- https://developers.cloudflare.com/d1/platform/limits/
- https://developers.cloudflare.com/workers/ci-cd/builds/configuration/
- https://cli.github.com/manual/gh_repo_create

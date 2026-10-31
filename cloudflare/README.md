# Game Radar Cloudflare migration

This directory is the Cloudflare runtime boundary. The local Python application
remains the source of truth until its APIs, collectors, and existing data have
all been migrated and verified.

## Current foundation

- Serves the existing `radar/web` files as Worker static assets.
- Exposes `/api/health` and `/api/cloudflare/status`.
- Defines ten deterministic SQLite-backed Durable Object shards. A `(source,
  entity)` pair always routes to the same shard; metric upserts are idempotent
  and capped at 90 records per request.
- Returns an explicit `503 api_not_migrated` for local API routes that do not
  exist in the Worker yet. This prevents a static-only deployment from looking
  like a functioning radar.

## Not migrated

The Python collectors, their scheduling, the existing dashboard API surface,
and historical-data import are not implemented here. The status endpoint lists
these migration groups. Production deployment must wait until those groups are
ported and the imported data has been verified against the local application.
The dashboard also has mutating APIs; protect the site with Cloudflare Access
before exposing it, and never put settings or provider credentials in static
assets. The shard diagnostic/import endpoint requires the `IMPORT_TOKEN` Worker
secret.

## Storage and scheduling constraints

Shard IDs use a stable hash of `(source, entity)`. Queries for one entity can
route to one shard, while global boards and trends will need bounded fan-out
across all shards. Do not add per-shard aggregation without checking all-shard
results against the local SQLite implementation.

Collection work must be checkpointed and idempotent. A Durable Object Alarm
is a new invocation, not a way to extend one invocation; each step must commit
its cursor before scheduling the next step. Alarm retries are at-least-once, so
collector writes must tolerate replay.

The free Durable Objects allowance is 5 GB across the account, with a 1 GB
limit per SQLite-backed object. Ten shards therefore do not provide 10 GB of
free capacity. The active local `radar.db` measured 945,127,424 bytes at 21:55
Asia/Shanghai on 2026-10-31; importing it as one
object would leave inadequate headroom for indexes and new writes, so the
existing database must be partitioned during import. Its size changed during
the work session, so take a SQLite-consistent backup/WAL snapshot for import;
do not copy the live database file directly.

## Local checks

From this directory:

```powershell
npm install
npm run types
npm run dev
```

Do not run `wrangler deploy` until the pending API groups and data-import
verification are complete.

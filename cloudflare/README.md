# Game Radar on Cloudflare

The Worker forwards the existing website and API routes to the Python
application in one Cloudflare Container. The application and its data remain
the local GameRadar v101 code and SQLite schema; no dashboard API is silently
replaced with a snapshot-only response.

## Runtime and storage

- `Dockerfile` builds the Python app and Litestream runtime.
- The container binds the app to `0.0.0.0:8765`.
- SQLite switches to WAL only when `RADAR_SQLITE_JOURNAL_MODE=WAL` is set.
- Litestream replicates WAL changes to the private R2 bucket. On a new instance,
  it restores the newest replica, then falls back to `seed/radar.db`.
- The entrypoint also restores `seed/settings.json`. This file contains runtime
  configuration and secrets and must never be committed or served as an asset.
- A one-minute Worker cron requests `/api/health` to renew container activity.
  The collector continues to use its existing schedules.

## Access protection

Create a Cloudflare Access self-hosted application for the exact `workers.dev`
hostname and allow only the intended account identities. Set the Worker's
`CF_ACCESS_TEAM_DOMAIN` (the `team.cloudflareaccess.com` hostname, without a
scheme) and `CF_ACCESS_AUD` (the Access application audience tag). The Worker
validates the `CF-Access-Jwt-Assertion` signature, issuer, audience, and time
claims on every public request. It rejects requests when either value is
missing or a token is invalid; do not remove this check to make the site load.

## Required Cloudflare setup

Containers require Workers Paid. R2 must be enabled, with a private bucket and
an R2 S3 key. Configure the following Worker secrets before starting the
container: `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, and
`R2_SECRET_ACCESS_KEY`. `R2_SEED_KEY` and `R2_SETTINGS_KEY` default to
`seed/radar.db` and `seed/settings.json`.

Upload a SQLite-consistent database snapshot and its matching settings file to
those seed keys before the first request. Take the final snapshot immediately
before cutover; the local database may continue changing while the cloud copy
is prepared. Keep both seed objects in a private bucket and out of Git and the
Docker build context.

## Collector compatibility

The container has no connection to the workstation's logged-in CDP browser on
`127.0.0.1:9222`. Browser-driven social searches/hot lists and historical B站
discovery therefore cannot be assumed to work in Cloudflare as they do locally.
They require a separately designed cloud browser/session integration or
source-specific API implementations. Current local settings also have no AI
key, B站 cookie, proxy, or Twitch API credentials configured. Verify each
source's live health after deployment; a reachable dashboard alone is not
evidence that collection behavior is complete.

## Validation

`wrangler types` validates bindings and configuration. A Worker-only dry run
can use `wrangler deploy --dry-run --containers-rollout=none`; a full image
build requires a Docker-compatible engine and an account with Containers
access. Do not deploy the snapshot-only Worker prototype as the production
site.

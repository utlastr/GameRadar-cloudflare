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
- The initial seed manifest references the matching `settings.json` object.
  Settings contain runtime configuration and secrets and must never be
  committed or served as an asset.
- The initial seed is published as checksummed R2 parts plus the
  `seed/current.json` manifest. The manifest is uploaded last so a partial
  upload is never selected for restore. Existing `seed/radar.db` seeds remain
  a fallback for older deployments.
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
`R2_SECRET_ACCESS_KEY`. `R2_SEED_KEY` defaults to `seed/current.json`.

The current one-minute Cron and two-minute `sleepAfter` keep the container
close to continuously active. With `standard-1` (4 GiB memory, 8 GB disk), the
Workers Paid base charge plus provisioned memory/disk is approximately
US$32.10 per 30-day month before active CPU usage, R2 storage/operations, and
other usage. This is not a Workers Free plan deployment; review the account's
current pricing and usage before enabling billing.

Publish the initial snapshot from the repository root with
`cloudflare/scripts/upload_snapshot.py`. It uses SQLite's online backup API,
checks `quick_check`, divides the snapshot into 250 MiB objects, and uploads
the manifest last. Pass the current database, matching settings file, bucket,
and a Wrangler command using a Node.js 22+ runtime, for example:

```powershell
python cloudflare/scripts/upload_snapshot.py `
  --database "D:\path\to\radar.db" `
  --settings "D:\path\to\settings.json" `
  --bucket "your-private-bucket" `
  --wrangler "D:\path\to\node.exe" "cloudflare\node_modules\wrangler\bin\wrangler.js"
```

Take the final snapshot immediately before cutover; the local database may
continue changing while the cloud copy is prepared. Keep seed objects in a
private bucket and out of Git and the Docker build context.

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

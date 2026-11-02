# Game Radar on Cloudflare

**Superseded prototype:** the Python/Containers design described below is no
longer the requested deployment target. Native JavaScript/TypeScript Workers
must replace it. The npm deploy command is blocked while the configuration
still declares a container. See the
[migration assessment](../docs/2026-11-02-workers-native-assessment.md).

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/utlastr/GameRadar-cloudflare)

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
  --wrangler "D:\path\to\node.exe" "node_modules\wrangler\bin\wrangler.js"
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

## Git-connected deployment

The deploy button uses the repository root, not the `cloudflare/` subdirectory.
The Python source and Dockerfile must remain in the build context. Root
`wrangler.jsonc` is the deployment configuration; the npm workspace commands
in `cloudflare/package.json` explicitly reference it.

For the initial connection:

1. Publish this variant to its own GitHub repository. The deploy button requires
   a public source; for a private repository, connect it directly in Workers
   Builds instead.
2. Authorize Cloudflare's GitHub integration and confirm the repository it will
   actually build. The button can create a copy: building that copy will not
   deploy later changes pushed to a different repository.
3. Select production branch `main`, repository root `/`, Node.js 22 or newer,
   and deploy command `npm run deploy`. The build system must install the root
   lockfile with `npm ci`. No separate frontend build is required.
4. Supply the six Worker secrets listed in root `.dev.vars.example` through the
   Cloudflare setup. The example contains placeholders only. Enable Workers Paid
   and R2, configure Access, and publish a consistent private database seed as
   described above before declaring the radar operational.
5. Verify that Workers Builds is connected to that same repository and branch.
   Push a local code commit to it and confirm the resulting build, deployment,
   public Access login and backend health. A README badge alone is not proof
   that this connection exists or that deployment succeeds.

After connection, edits are made locally, committed and pushed to the selected
production branch through the repository's PR process. Workers Builds builds
that commit in the background. Do not configure another automatic deployer for
the same Worker unless duplicate deployments are intentionally desired.

Current repository preparation does not enable billing or configure the
account-side integration. Those actions require the authorized account session.

Official references:

- https://developers.cloudflare.com/workers/platform/deploy-buttons/
- https://developers.cloudflare.com/workers/ci-cd/builds/

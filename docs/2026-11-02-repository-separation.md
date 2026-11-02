# Repository Separation Audit

Verified on 2026-11-02. This record describes source control, not deployment.

## Source Selection

| Source | Commit | Commit time (Asia/Shanghai) |
| --- | --- | --- |
| GitHub GameRadar main | `7e8ba9413ecbb650ccd431f9636f8c42825e92ed` | 2026-10-29 08:32:23 |
| Local application before migration | `86a938044099c6688cf827b764194e7c893695c4` | 2026-10-31 21:04:15 |
| Independent Cloudflare variant before this audit | `6f5b408d39707eb69bf2f819abf6f44548703397` | 2026-11-02 18:49:09 |

GitHub main and the local application are different histories: `git merge-base`
finds no common ancestor. Their `radar/` trees differ in 39 paths. The local tree
contains YouTube, real-time Bilibili and Steam cycle modules absent from main.
This does not establish that the local tree contains every fix from main.

Use the local pre-migration snapshot as the Cloudflare variant's application
baseline, preserving the current local interface and collection implementation.
Do not replace it with GitHub main or merge the histories automatically.
The fetched main snapshot is retained as `refs/remotes/source/main` in this
independent repository for future, explicitly scoped comparisons.

## Isolation

- Installed original: `D:\Users\KLEI\AppData\Local\Programs\GameRadar`.
- Independent variant:
  `D:\Users\KLEI\Projects\Tools\GameRadar-cloudflare-standalone`.
- The independent variant has its own `.git` directory and no object alternates.
- The original application's tracked working tree was clean before this audit.
- The original snapshot contains no tracked `cloudflare/`, `Dockerfile` or
  `.dockerignore` files.
- Comparing the local original snapshot with the variant changes only
  `radar/config.py` and `radar/db.py` under `radar/`: environment-selectable
  runtime paths/listen address and opt-in SQLite WAL.
- Other variant additions are deployment files, tests and documentation.
- This audit does not certify that the original is byte-identical to a version
  before the earlier UI recovery; it verifies against the named snapshot.

The original GitHub repository already contains migration work on the separate
`feat/cloudflare-workers-20261031-work` branch at `cbf36b1`. The main tree does not
contain the Cloudflare packaging. No existing branch was rewritten or removed.

## Access Checks

- SSH `ls-remote` and fetch from `utlastr/GameRadar` succeeded.
- SSH push dry runs for an unchanged main and a new test branch succeeded.
  These were previews, not actual writes; no test branch was created.
- An actual push to `utlastr/GameRadar-cloudflare` returned
  `Repository not found`. This is not a successful publication.
- GitHub repository creation requires an authenticated website/API operation,
  separate from configuring a Git remote.
- The Git credential helper returned no nonempty HTTPS secret. The managed
  CDP browser connection timed out, so remote repository creation remains
  uncompleted.

The variant's only configured remote is
`git@github.com:utlastr/GameRadar-cloudflare.git`. Do not redirect its pushes to
the original repository as a workaround.

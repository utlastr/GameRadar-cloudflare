# Parallel Development Rules

These instructions apply to every agent working on this repository, whether
locally or in a web/cloud environment.

## Cloudflare Variant Workflow

- Make source changes in this independent local checkout, test and commit them,
  then push to `utlastr/GameRadar-cloudflare`.
- Do not edit the original installed application or push Cloudflare changes to
  `utlastr/GameRadar`.
- Preserve the original application baseline `86a9380`; cloud-specific changes
  belong only in this variant.
- Per the user's instruction on 2026-11-02, stable iterations are developed
  locally on `main`, then tested, committed and pushed to `main`.
  Create another branch only when the user explicitly requests one.
- Verify the remote commit before reporting synchronization complete.
- The deployment target is native JavaScript/TypeScript Workers. Do not deploy
  the earlier Python/Containers prototype.
- Workers Builds must watch the same GitHub repository and production branch.
  A deploy badge or local commit does not establish that connection.

## Authoritative Base

- The current integration branch is `main`, as explicitly directed by the user
  on 2026-10-28. It is also the GitHub default branch.
- The legacy `master` branch has different history. Do not merge that history
  into `main` as an incidental part of a task.
- Unless the user explicitly changes the integration branch, start new work
  from the latest fetched `origin/main`.
- Never replace `main` with `master`, or merge their histories, as an incidental
  part of another task.

## Before Editing

1. Read this file, then inspect `git status`, the current branch, and the remotes.
2. Fetch the intended base when network access is available. Fetching does not
   authorize merging, rebasing, resetting, or discarding local changes.
3. Use `main` for stable iteration. Create a separate task branch only when
   explicitly requested by the user.
4. Each simultaneously active agent must have an independent checkout or Git
   worktree. Different branches in one shared working directory do not isolate
   uncommitted edits. Never switch branches in another agent's active directory.
5. Record the task's base commit and keep changes within the requested scope.
   If the base cannot be fetched, explicitly identify the local base used.

## Preserve Other Work

- Treat pre-existing changes as user or other-agent work. Do not delete, revert,
  overwrite, or include unrelated changes in your commits.
- If another agent changes a file during your task, inspect the new content and
  preserve it. Ask the user only when the changes make progress impossible.
- Never use force push, including `--force-with-lease`, or destructive recovery
  commands such as `git reset --hard`, `git clean -fd`, or file-restoring
  checkout/restore commands without explicit user authorization for that exact
  operation.
- Never amend, rebase, or otherwise rewrite another agent's published commits
  without explicit user authorization.
- A rejected push is a signal to inspect remote changes, not permission to
  overwrite them. Preserve both histories and reconcile on the task branch.
- Commit meaningful completed work. When publishing is requested, push `main`
  (or the branch explicitly requested by the user).

## Stable Main Updates

- Test stable changes locally, commit to `main`, and push without force.
  Pull requests and additional branches are optional only when requested.
- Before pushing, fetch the latest base and compare the full diff. Reconcile
  concurrent changes without discarding either side.
- Resolve conflicts by understanding and preserving both intended behaviors.
  Never resolve an entire conflicted file using "ours" or "theirs" merely to
  make the merge succeed. Ask when the intended behaviors are incompatible.
- A clean Git merge does not prove functional compatibility. Check shared API
  contracts and run tests appropriate to the changed behavior.
- Report the tests actually run and their results. Distinguish existing failures,
  new failures, and checks that were not run. Never claim the full suite passed
  when only a targeted subset passed.
- If branch protection, review, or checks prevent a direct update, report the
  requirement. Do not bypass protections.

## Publication Safety

- Keep credentials, API tokens, cookies, browser profiles, personal settings,
  and runtime databases out of commits and uploaded packages.
- Honor `.gitignore`, especially `radar_data/`. Do not use `git add -f` to
  bypass exclusions without explicit authorization and a sensitive-data check.
- Inspect the staged diff before committing, and verify the remote commit after
  pushing.
- In the final report, identify the branch, commit, pull request when applicable,
  and anything still waiting to be merged or synchronized.

## Limits

This file guides cooperating agents; it is not GitHub access control. Repository
branch protection and required checks must be configured separately. Do not
claim that writing these instructions enabled those protections.

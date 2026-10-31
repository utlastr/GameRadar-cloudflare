# Parallel Development Rules

These instructions apply to every agent working on this repository, whether
locally or in a web/cloud environment.

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
3. Use a separate task branch, such as `fix/<task>`, `feat/<task>`, or
   `docs/<task>`. Do not develop directly on `main` or `master`.
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
- Commit meaningful completed work. When publishing is requested, push the task
  branch so work is not held only in an uncommitted local directory.

## Integrate Through Pull Requests

- Changes to `master` or `main` must go through a pull request. Do not push
  changes directly to either integration branch.
- Target `main` unless the user explicitly requests another base. When the user
  asks to finish and merge the task, complete the pull request merge if checks
  and repository permissions allow; do not describe an open PR as integrated.
- Before merging, fetch the latest base and compare the full diff. Reconcile
  concurrent changes on the task branch using a normal merge when needed.
- Resolve conflicts by understanding and preserving both intended behaviors.
  Never resolve an entire conflicted file using "ours" or "theirs" merely to
  make the merge succeed. Ask when the intended behaviors are incompatible.
- A clean Git merge does not prove functional compatibility. Check shared API
  contracts and run tests appropriate to the changed behavior.
- Report the tests actually run and their results. Distinguish existing failures,
  new failures, and checks that were not run. Never claim the full suite passed
  when only a targeted subset passed.
- If branch protection, review, or checks prevent a merge, leave the pull
  request available and report the requirement. Do not bypass protections.

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

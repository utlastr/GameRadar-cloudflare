# Unified Tag Queue Verification

Checked October 28, 2026, local time (Asia/Shanghai).
Running revision: `2026-10-28-unified-tags-v5`, PID `42408`.

## Implementation

- One durable queue covers realtime and historical incomplete videos.
- Four shared Tag workers prioritize recent work while reserving historical capacity.
- Historical directory expansion pauses above 20,000 pending jobs and resumes at 10,000 or fewer.
- Detail persistence and queue completion are atomic; local analysis uses an independent retryable outbox.
- Indexed queue selection, bounded word refresh and bounded maintenance reduce database contention.
- Dashboard totals now cover all saved videos, with separate recent, history, retry and analysis debt.

## Live Verification

Source: `bili-unified-tags-final-20261028.json`, ending at 08:45:52.

- Observation window: 178.84 seconds.
- Newly saved videos: 111 (37.24 per minute).
- Successfully completed Tags: 277 (92.93 per minute).
- Confirmed unavailable videos: 11.
- Pending queue change: -177.
- Final pending: 426,591, including 3 recent jobs.
- Oldest actionable recent job: 2.2 seconds.
- Analysis pending/errors: 0/0.
- No observed risk cooldown; same process throughout.
- Video-state totals and queue counts consistent in every sample.

A subsequent read-only production snapshot found zero missing pending jobs,
zero terminal queued jobs and zero orphan jobs. Its queue count was 426,579
(426,178 pending plus 401 error records).

This short observation demonstrates draining under the observed workload,
not all-day peak capacity or guaranteed completion of the historical backlog.

## Tests and Rehearsal

- All 174 Bilibili tests passed in 95.053 seconds.
- Dashboard route/JavaScript regression checks passed.
- CDP desktop/mobile inspection found no overflowing panel text.
- Full-suite attempt: 336 tests, 319 passing and 17 errors from existing
  missing `config.UPDATE_SCOPE` and removed social modules, outside this change.
- Migration rehearsal: 545,083 videos, 427,167 pending jobs; migration
  approximately 1.14 seconds; SQLite quick check returned `ok`.
- Historical queue selection improved from approximately 0.688 seconds to
  0.002657 seconds per selection on the rehearsal copy.
- Dirty word batch of 128 took approximately 0.091 seconds; bounded catalogue
  sweep of 256 took approximately 0.104 seconds.

The rehearsal database at
`D:/Users/KLEI/tmp/gameradar-before-unified-tags-20261028.db` preserves the
original raw-video snapshot but has been migrated and used for derived-data
benchmarks. It is not an untouched pre-migration schema backup.

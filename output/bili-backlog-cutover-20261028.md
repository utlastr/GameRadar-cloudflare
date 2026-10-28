# Historical Backlog Cutover

Cutover: October 28, 2026, 11:16:25, Asia/Shanghai.
Revision: `2026-10-28-fresh-baseline-v6`.

## Preservation Check

| Measure | Before | After |
| --- | ---: | ---: |
| Raw video records | 552,818 | 552,818 |
| Completed Tags | 130,639 | 130,639 |
| Real playback snapshots | 128,811 | 128,811 |
| Incomplete active videos | 418,481 | 0 |
| Tag queue | 418,481 | 0 |

The 418,481 incomplete records were archived, not completed or classified
as unavailable. Their original pending/error state is recorded in `bq_archived`;
raw records and retry/error fields remain in SQLite. Three old growth caches
were preserved in `bq_archive_cache`.

Pre-cutover backup (770,420,736 bytes):
`radar_data/backup/before-bili-reset-20261028-111624.db`.

## Collection Policy

- Realtime discovery and four shared Tag workers continue.
- Dated historical expansion and automatic gap backfill are disabled.
- All five previous daily tasks and their partition checkpoints are archived.
- Previous playback refresh jobs are inactive.
- New growth calculations use only new-stage discoveries and observations.
- Retained historical data is not presented as a complete new trend baseline.

Read-only checks after restart found no archived jobs in the queue, no missing
active jobs and no old active playback-refresh jobs.

The 90-second post-restart monitor (`bili-fresh-baseline-20261028.json`) saw
112 new videos, 110 Tag completions and a final queue of five, all recent;
the oldest actionable job waited 0.7 seconds. Every sample had consistent
state/queue totals, no historical queue and no observed risk pause.
This verifies the cutover and current operation, not sustained peak capacity.

At 11:27:08 the new stage contained 584 videos, all 584 Tag-complete, with
zero pending jobs and zero historical queue. Four Tag workers remained live.
The pre-cutover backup passed SQLite `quick_check`.
Final Bilibili regression run: all 181 tests passed in 97.452 seconds.
The dashboard JavaScript/route regression also passed.

The final manual-refresh guard in `radar/collect.py` routes refreshes to the
latest-post collector, never the legacy search sampler. Its targeted regression
tests passed. A second background restart to load that last handler change was
rejected by the execution policy; the user must exit and reopen GameRadar once.
The archive, new baseline, automatic collection and historical-disable policy
are already loaded and running.

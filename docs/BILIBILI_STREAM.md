# Legacy Game Latest-Post Collection

## Active Source

The production entry point is `bili_stream.start`, which starts `bili_latest`.
One anonymous shared-client request reads the rolling game-parent head:

`https://api.bilibili.com/x/web-interface/newlist?rid=4&pn=1&ps=100&type=0`

The server currently returns 50 records even when 100 are requested. Actual
response length is recorded, not converted into a claimed page capacity.
Each video carries its legacy child ID. One storage worker drains the eight
durable child inboxes; these are not independent network readers.

The former app-host `/x/v2/region/dynamic/list` source returned ten cards per
child, including old posts in nonchronological order. Production no longer
calls it. The legacy manual `run_once` remains a diagnostic helper only.

## Continuity

The latest endpoint has no verified historical timestamp cursor. Each poll
reads the current head, compares BV IDs with the saved head, and deduplicates.

- First poll is `bootstrap`, not recovery of earlier history.
- Overlap links the two observed snapshots.
- No overlap records an unresolved `bs_gaps` entry; missing counts stay
  unknown. A later overlapping batch does not erase an earlier gap.
- Backward or stale publication watermarks, malformed/wrong-category cards,
  duplicates and nonchronological results fail without replacing the previous
  successful checkpoint.

The default target is ten seconds, reduced to 40 percent of the returned
publication window if needed, with a three-second floor. Source cooldowns
take precedence. Request contention or outages can still cause gaps; actual
elapsed time and overlap expose them.

Coverage is observed legacy-game posts, not every new-category game video.
New-game posts assigned to legacy film/life/animation remain outside this
parent. The delayed daily worker remains separate; it does not certify
historical or daily completeness.

## Durable Queues

The head and new `bs_inbox` jobs commit atomically. `bs_latest_seen`
deduplicates discovery. `bili-stream-inbox` drains each child's FIFO inbox
into existing video, keyword and pending-Tag stores.

Four Tag workers continue saved pending jobs across minute boundaries and
restarts. Selection reserves distinct pending BVs under a short local lock;
network requests run outside it. Complete Tags, both classification IDs and six counters reuse the
existing detail adapter. Failed details back off and remain pending.
Unavailable videos are explicit. Fresh-head placeholder counter zeros are
not treated as final statistics. Business code 62012 is a retryable unknown
failure, not evidence that a video has been deleted.

A per-BV lease prevents duplicate concurrent daily/latest enrichment. Both
workers honor the saved per-video retry deadline. Detail requests use a
12-second timeout and enter the shared dispatcher even when a head is due;
they are no longer skipped for lack of time before that deadline. A failed
video does not block the following videos. No Tag blacklist drops an entire
video. Daily-worker completions also count in realtime Tag throughput.

## Capacity and Risk

Eight workers: one head reader, one local inbox worker, four Tag workers,
one keyword/health maintenance worker and one local analysis worker.
Maintenance and local analysis do not occupy a Tag worker.
All Bilibili callers enter a reentrant, weighted-fair dispatcher before the
shared request-start gate. Waiting head requests take precedence, but cannot
run twice in a row while other classes are waiting. Details, dated directories
and other clients receive rotating turns; detail slots have greater weight.
The application policy defaults to 180 starts/minute (one-third-second spacing) and
four admitted requests. Spacing is measured between request starts; network
latency overlaps instead of being added to every inter-request delay.
`BILI_REQUESTS_PER_MINUTE`, `BILI_MAX_INFLIGHT` and `BVIDEO_TAG_WORKERS` can
lower these defaults; configuration caps remain 240, four and four respectively.
This is an application budget, not a measured official Bilibili allowance.
The previous 1.5-second completion-based serial gate caused the old
40/minute latency-free ceiling and approximately 22 actual requests/minute.
Anonymous sessions are thread-local, retain keep-alive, discard response
cookies before every request and close when a worker exits.
Queued callers recheck saved cooldowns inside the shared start gate.
A risk response stops subsequent affected starts, including callers already
admitted but still waiting at that gate. Requests already sent may finish;
their successes cannot clear a newly recorded risk pause or hit count.

The October 27 Tag hot-path repair deletes a pending job in the same
transaction that saves its complete Tags/statistics (or confirmed unavailability).
It no longer sweeps every completed video before every individual request.
Legacy queue repair runs once at initialization. Repeated `62012` errors remain
errors, not successful/deleted videos; their retry delay is five minutes, then
six hours, then one day after repeated failures. Other pending videos continue.

Queue preservation is not proof that the worker can catch up. Actual discovery
and completed-detail rates over up to ten minutes expose growing, draining
and stalled queues. A restart starts a fresh throughput window, excluding old
process events. Growth uses actual Tag-queue entries minus successful and
unavailable exits, rather than assuming every rediscovered video needs another
detail request. Equal input/output with active processing is reported as
balanced, not stalled. An observed-rate projection is only provided after a
minute of observations and when exits exceed entries; it is not a catch-up promise.

Closed days touched by unresolved realtime gaps are registered with the
existing daily backfill queue. A completed dated directory does not silently
resolve a realtime gap or establish whole-platform coverage.

## Verification

`GET /api/bili-stream` reports:

- `source=legacy_game_latest_web`, `mode=latest_window`, actual runtime PID
  and list/storage/Tag thread liveness. The October 26 scheduler reports
  `collector_revision=2026-10-26-bounded-capacity-v2`,
  `independent_partition_fetching=false` and `full_coverage=false`.
- Successful request time, latest publication and its age.
- Actual returned count, deduplicated new count, previous-head overlap and
  observed successful-request interval.
- Unresolved gaps, persistent inbox work and pending full-Tag work.
- New discoveries and completed Tags during the last 60 seconds.
- A rolling throughput comparison, earliest pending day, actual request
  counts/queued callers by request class, admitted request count, mean wire
  and queue times, response outcomes and actual Tag worker count.

Partition rows do not reuse stale old sampler polling counters as evidence of
network activity. They show per-batch discoveries, total saved
records, pending Tags and the latest publication observed for that partition.
A quiet partition need not have a video in the current minute; parent-head
freshness is checked separately.

An old process continues to use its loaded modules. The dashboard detects a
missing or previous scheduler revision and requests a restart. Code edits do not hot-reload
the app: exit from the tray and reopen `GameRadar.pyw`, then verify revision,
request telemetry, head overlap and increasing completed-Tag counts.

`tests/test_bili_pipeline.py` runs the production workers and anonymous HTTP
transport against an explicitly local fixture. It covers overlapping heads,
all six counters/Tags, code-62012 isolation, simultaneous daily work and saved
retry progress. This is protocol-level regression evidence, not a live
Bilibili load or completeness test.

Its local-only `capacity_fixture(0.5, 1.2)` exercises the actual shared client
with the production 0.5-second spacing and a fixture adding 1.2 seconds of
wire latency per response. On October 26 it completed 50 full details, 40
head requests and ten dated-directory requests in 53.922 seconds, with all
Tags/six counters present, no cookies and no errors. Observed HTTP concurrency
peaked at three, below the configured cap of four. This verifies the client
capacity for that workload; upstream Bilibili throughput still requires
running the new revision against the real service.

## Unified Tag Queue (October 28)

`2026-10-28-unified-tags-v5` uses `bs_pending` for both realtime and dated
directory videos. Startup imports existing incomplete records once, retaining
retry deadlines and terminal states. A directory page commits its jobs with
the videos and page checkpoint. Production daily slices only list/enqueue;
the shared Tag workers own enrichment. Standalone bounded daily runs retain
their synchronous diagnostic path and the same per-BV leases.

Normal contention reserves one in five jobs for history; when actionable
recent jobs wait over 120 seconds, one in ten remains reserved for history.
Either lane borrows idle capacity. Recent jobs age into history after one hour.
Queue selection starts from its ordered lane index rather than sorting the
entire incomplete video catalogue for each request.

Historical directory expansion pauses above 20,000 outstanding jobs and resumes
at 10,000 or fewer. This does not stop realtime discovery, existing enrichment,
or local analysis. Both watermarks are editable settings. Playback refresh
yields while actionable recent Tag jobs have waited over 120 seconds. Existing
request spacing, bounded concurrency and upstream risk cooldowns remain active.

The raw detail, actual playback observation, queue exit event and local analysis
outbox commit atomically. A separate worker performs word extraction and Tag
indexing in short transactions. Analysis errors have their own durable backoff
and do not refetch successful details. Growth backfill uses small batches;
legacy title rescans run less frequently with a smaller bounded title budget.
Changed word evidence has its own coalesced dirty queue, processed in bounded
batches. AI judgments refresh only their affected words; a bounded rolling
catalogue sweep still revisits old words after rule or catalogue changes.
Unchanged word metadata is not rewritten.

Top-level counts and throughput now cover **all saved videos**. `realtime`
retains source-specific counts; `queue` exposes recent/history/deferred counts,
analysis debt and directory backpressure. A short-window drain projection is
only an observed-rate diagnostic, not a catch-up promise; `catch_up_seconds`
is null. Network telemetry also reports database-lock wait and the slowest
transaction thread to distinguish local contention from upstream latency.

## Fresh Baseline Cutover (October 28)

`2026-10-28-fresh-baseline-v6` supports an offline, backed-up cutover using
`python tools/reset_bili_backlog.py --apply`. The tool refuses to run while
the local application server is listening and backs up SQLite before changing data.

Existing incomplete records become `detail_state='archived'`, retaining their
raw titles, error history and other fields. `bq_archived` records their previous
state and cutover time. They are removed from the active Tag queue, not counted
as successful or unavailable, and cannot be requeued by migration or rediscovery.
Completed videos, raw Tags, playback samples and the local analysis outbox remain.

The persistent `bili_collection_reset` policy disables dated-directory work and
automatic gap-day backfill independently of watermarks. The installed settings
also disable `BVIDEO_DAILY_ENABLED`. Old playback tracking is deactivated; old
growth caches are retained in `bq_archive_cache` instead of served as new results.

Only posts published on or after the cutover join new collection. Growth uses
only videos discovered and published in this new stage, with observations on
or after the cutoff. Old completed data remains available for discovery but
does not become a complete trend baseline. The dashboard shows both retained
active totals and separate new-stage totals, plus the archive count and start.

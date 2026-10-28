# Bilibili Daily Collection

`radar.bili_daily` replaces periodic latest/ranking/keyword sampling when
`BVIDEO_DAILY_ENABLED` is true. The existing Steam release worker is unchanged.

## Queue

- At 00:05 Beijing time, enqueue the previous calendar day.
- Request dated old-category directories with `pagesize=100`. No keyword search.
- Commit each page and its next-page checkpoint in one transaction.
- Deduplicate BV IDs across directories. Alternate directory pages with
  `view/detail` enrichment for complete Tags and six counters, so large
  directories cannot starve Tags. Shared risk controls apply.
- Resume unfinished work in bounded slices. A database lease prevents two
  processes from collecting the same queue concurrently.
- Retry stale/unstable directories after one hour, independently per partition.
  A changed directory does not abort listing of the other partitions.
- Detail errors save `detail_attempts`, `detail_retry_after`, `detail_updated`
  and the exact error per BV. Retry starts at 60 seconds and backs off to an
  hour; the next ready video proceeds immediately. Both realtime and daily
  enrichment use these same retry deadlines.
- `waiting_details` means videos remain pending, not a complete day. Code
  62012 stays retryable; its business meaning has not been established.
- Risk pauses respect the client's recorded cooldown without clearing it.

The selected old categories are configured in `BVIDEO_DAILY_TIDS`; they are not
a whole-site game census. Both `tid` and `tid_v2` are retained from video detail.
The dated adapter accepts a `tid_version` argument, but this worker uses version
1. New-category availability monitoring is separate and is not performed by
this queue.

## Completeness

Pages must have valid BV IDs, the requested day/category, consistent totals and
page sizes, no overlaps, no short intermediate page, and the exact reported
unique count at the end. Aggregate earliest/latest publication times across
every page, since pages are not strictly chronological. Only after reaching
the reported end do empty or early-watermark high-volume game directories
enter `waiting_index`; returned videos and Tags are retained and other
directories continue. An expired stale-directory retry rebuilds only its
pagination snapshot, retaining videos and completed Tags. This late-watermark
check is a stale-index guard, not proof of whole-platform completeness.
Changed totals or overlapping pages reset only that directory's page
checkpoint for the next retry, preserving collected videos and Tags.
Deleted/unavailable videos produce `partial`; missing Tags/counters remain
pending errors, not fabricated zeros. Ready days rotate by their last update
time so an older unfinished day cannot monopolize every slice.

## Local Discovery

Candidate evidence comes from paired title punctuation and complete Tags.
Dates, numeric episode markers, generic labels, configured person names and
the current uploader's name are filtered. Unknown repeated names stay
candidate topics rather than being assigned invented game IDs.

By default, three seed videos from two uploader IDs promote a candidate.
Exact names from existing game entities can be matched to actual platform IDs.
Normalization and `BVIDEO_NAME_ALIASES` merge spellings; fuzzy merging is not
automatic.

New candidates rescan saved titles from the last 90 days using compiled
multi-pattern batches. Existing candidates scan only newly changed titles.
Each topic counts a BV once even when both title and Tags match it.
The 90-day option limits retrospective matching, not raw-data retention.

`KW_BIG_GAMES` and `BVIDEO_WORD_BLACKLIST` block words, not videos. Mixed
big-game/new-game Tags retain the new-game evidence and the original Tags.

## Stored Data

BV ID, publication day, title, uploader UID, old/new category IDs, all Tags,
and cumulative views/likes/coins/favorites/shares/replies. No video URL,
precise publication time, description, or danmaku is stored in these tables.
Queue timestamps are operational retry/checkpoint metadata.
On migration, only a legacy day-wide error backoff explicitly caused by a
saved video-detail failure is released. Global/endpoint risk cooldowns and
saved completed details remain untouched.

## Operations

```powershell
python -X utf8 -m radar.bili_daily --day 2026-10-23 --requests 5 --seconds 60
python -X utf8 -m pytest tests/test_bili_daily.py tests/test_bili_client.py tests/test_discovery.py -q
```

The dashboard's Bilibili view displays daily status and candidate metrics.
`GET /api/bili-daily?day=2026-10-23` returns a saved day's status and topics.
Counters are cumulative at detail retrieval, not daily increases.
Progress logs distinguish saved videos, videos with complete Tags/statistics,
and pending videos, using Chinese status names. Repeated identical statuses
are summarized at most once per five minutes.

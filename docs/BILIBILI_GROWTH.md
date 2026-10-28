# Bilibili Tag Growth And Candidate Groups

## Data And Scope

- Raw Tags are indexed separately in `bg_tags` / `bg_hits`; existing word
  judgments and blacklists do not remove Tags from this growth index.
- A normalized Tag occurs at most once per BV. Normalization is NFKC,
  case folding, whitespace removal and leading `#` removal, not alias guessing.
- Only saved Bilibili videos are analyzed. Pending Tags and collection gaps
  remain missing coverage; this is not a whole-site census.
- `bg_samples` stores real observation timestamps and cumulative plays.
  Existing details are imported at `detail_updated`, never at publication time.
  Imported Tag relationships describe the fetched Tags, not a historical
  reconstruction of when a Tag was first attached.

## Ranking

The default date is the last closed Beijing calendar day, D.

```
H(D) = sqrt(remaining_active_authors(D) * remaining_plays(D))
I(D) = (max(H(D), mean(H(D), H(D-1), H(D-2))) + c)
       / (median(H(D-3), ..., H(D-9)) + c)
```

For each window, first aggregate all related videos by UP. Remove the UP with
the largest play increment, including all of that UP's videos. Both factors
use the remaining population. An active UP has a positive observed increment
in this window; posting UPs and new video counts are separately available.
This is not a unique-audience or game-player estimate.

`c` defaults to 100 and is an explicit configurable initial value, not a fitted
optimum. The UI can change it; the override persists in SQLite.

Playback at a day boundary is exact if a real snapshot exists at that boundary.
Otherwise it is linearly estimated only between bracketing observations at
most 30 hours apart. There is no extrapolation. The UI marks estimates.
Negative/reset counters and missing endpoints are missing data, never zero.
For a video first published during the day, the pre-publication count is zero.
All known related videos published by a given day's end must have usable
endpoints and an owner for a complete group metric. Ten comparable heat days
and an observation history starting before the window are required for I.

During warm-up, a separate provisional rank uses saved publication counts:
`(max(D count, last-three-day mean) + 1) / (preceding-seven-day median + 1)`.
It is labeled as a posting-growth preselection, not measured play growth.
Tags with measured play growth are also considered even without new uploads.
The expensive provisional pool is bounded; the complete keyword catalogue
remains separately accessible.

One-UP signals retain their own tab. A supplementary untrimmed play-growth
indicator can retain these signals even when their trimmed heat is zero.
They are not interpreted as multi-UP diffusion.

## Grouping And Corrections

- Select up to 300 rising Tags; provisional posting-growth candidates are used
  while playback baselines are missing.
- For videos published in D-9 through D, or with observed playback increases
  in the observation range, count each Tag pair's distinct BVs and distinct
  nonempty UP IDs.
- Each pair needs at least three different UPs. Repeated videos by one UP
  cannot satisfy this threshold.
- During playback warm-up, pair evidence remains visible but is deferred:
  provisional posting-growth candidates are not automatically merged until
  both endpoints have comparable playback growth baselines. This prevents
  unequal historical collection coverage from turning generic Tags into
  apparently rising bridges.
- Accepted edges form connected candidate groups. A video matching any group
  Tag counts once when recomputing the group's history and growth.
- Display the Tag used by the most distinct UPs as the name, with other
  group Tags in parentheses. Ties use video count, then normalized spelling.
  No English original title is invented.
- A rising generic term can still bridge different games. Groups remain
  candidates rather than permanent game IDs or confirmed aliases.
- Each group exposes its edge counts and BV examples. A rejected pair is
  persisted in `bg_edges` and acts as a cannot-link constraint even through
  alternate paths. Restoring it rebuilds groups from unchanged raw evidence.
- Group history is recomputed using the current member set, not by adding
  yesterday's possibly overlapping Tag totals.

## Collection And Operation

- Full-detail enrichment records all Tags and a counter snapshot.
- The background worker imports existing records with a durable cursor,
  rebuilds locally every five minutes, and refreshes selected recent videos.
- Playback-only refreshes use the existing anonymous Bilibili transport,
  shared request-start limit, risk pauses, and per-BV lease.
- Default refresh target: six hours/video; one background request opportunity
  every ten seconds. Actual capacity depends on selected video volume and
  the shared collector. Overdue queue counts and failures are visible.
- Smaller complete Tag populations have refresh priority. A selected video
  remains tracked for eleven days after its last selection even if its Tag
  temporarily leaves the top list, so a brief early spike can accumulate a
  baseline. The UI exposes the latency-free minimum sweep time when it
  exceeds the target; no six-hour completion is promised.
- Existing latest-list and Tag collectors are unchanged. No cookies are read
  by the new worker; pauses are not cleared.

Settings:
`BVIDEO_GROWTH_ENABLED`, `BVIDEO_GROWTH_CANDIDATES`,
`BVIDEO_GROWTH_SMOOTHING`, `BVIDEO_GROWTH_SAMPLE_HOURS`,
`BVIDEO_GROWTH_REQUEST_SECONDS`, `BVIDEO_GROWTH_MAX_GAP_HOURS`,
`BVIDEO_GROWTH_TRACK_DAYS`.

API:
`GET /api/bili-growth`, `POST /api/bili-growth/rebuild`,
`POST /api/bili-growth/edge`.

Dashboard route: `http://127.0.0.1:8765/#bili-growth`.
The complete existing keyword catalogue remains at `#kw`.

Offline bootstrap:

```
python tools/build_bili_growth.py
```

This command makes no network requests and does not restart the application.

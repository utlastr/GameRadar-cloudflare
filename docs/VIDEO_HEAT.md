# Video Heat Homepage

The homepage preserves the original single annotated game table. Video signals,
not storefront counters, determine its ordering. There are no source categories
or platform round-robin slots.

## Current Connection

- `/api/bili-growth` supplies existing Bilibili Tag groups and `index`.
- `/api/state.cards` remains the game catalog, including notes, developers,
  aliases, launch information and reference flags such as platform percentiles.
- `/api/bili-terms?matched=1` supplies all saved unambiguous exact archive
  matches, including candidate terms, independent of growth-window eligibility.
  Multiple matched terms join one archive row; they do not sum discovery counts.
- The candidate set is the union of these feeds, not just growth groups.
  Blocked/dismissed cards stay hidden. A collection reset or empty growth feed
  cannot remove existing games or identity matches.
- `archive.gid` associates a group with an existing game. Unmatched groups stay
  visible under their Tag names; a store listing is not required.
- Across video sources, one game gets one row and one index:
  `1 + sum(weight[source] * max(source_index - 1, 0))`.
- A single video source contributes at most once per game. If separate groups
  are already linked to the same archive, use their largest index, not their
  sum. This is conservative deduplication, not a recomputation of their pooled
  author/playback statistics.
- Sort descending by this index. Equal indexes use name and identity only.
  Filtering preserves the original global index and rank.
- Missing video baselines have no index. These games stay in the same table,
  below valid indexes, ordered provisionally by existing observed video rates,
  then name and identity. The provisional value is not a fabricated growth ratio.
  Row numbers denote table positions. Historical discovery video/author counts
  are evidence only, never daily increments or ranking inputs.
  Old statistical days and results preceding the active collection reset cannot
  rank as live.
- Store APIs load only references for linked games. Comments, downloads,
  visits, players, prices, store ranks and revenue never enter ranking.
- Bilibili currently represents China. Douyin and YouTube growth feeds are not
  connected; their absence is not a zero observation.
- The current source adapter uses completed Beijing calendar days, not intraday
  live forecasting.

## Reference And History Columns

Linked game archives can open from the game name. Native store metrics retain
their observation times and missing/stale states. A linked TapTap entry is not
proof that a different developer has made a gameplay clone. Without evidence,
the homepage says clone availability is unverified, never "no competitors".

The last 14 cached Bilibili statistical days are requested with at most three
concurrent requests. The recurrence helper counts distinct observed days with
an index strictly above 2. Missing days are reported as missing observations,
not as cold days. This is a rolling observation column, not a durable all-time
alert ledger or an additional leaderboard.

Homepage configuration is centralized in `VIDEO_HEAT_CONFIG`; optional
`S.config.video_heat` overrides it. Initial video-source weights are all 1;
these are neutral provisional weights, not measured market revenue ratios.

## Scope And Remaining Data Work

This change connects the existing first three discovery stages rather than
rewriting them. The current upstream `bili_growth.py` implementation is not
identical to every clause in the proposed final model:

- Its heat uses the remaining author count after removing the largest author.
- Playback is daily observed growth, not each upload's first 24 lifetime hours.
- Its smoothing constant is configurable but currently fixed; it is not yet an
  automatically calibrated 30-day ordinary-game median.
- The homepage does not add permanent discovery/alert storage or recover Tag
  groups that upstream never selected.

As observed on October 28, 2026, the active collection baseline began at
11:16:25 Beijing time. The current cached October 27 result contained zero
groups. Historical raw samples and archive matches do not supply the missing
active-baseline playback history, but still provide identities and video
evidence immediately. Do not present synthetic ratios or the old store
leaderboard to conceal missing history.

The ForgeTax/铸剑大师 regression fixtures check ranking, names and source
deduplication only. They do not prove discovery from the routine October 6-9
stream. That historical non-keyword-search validation still needs its original
capture evidence.

# Publication Monitor

## Implemented

- `pub_records` stores one row per platform/content ID. Overlapping requests and retries do not inflate counts.
- Publication time and first observation time are distinct. Missing/future publication times are not counted as today's publications.
- `pub_topics` stores attribution evidence. A search term is a candidate, not a verified game identity.
- The social dashboard exposes 1/3/6/24-hour observed publication counts, distinct authors, source coverage, and unknown timestamps.
- Bilibili newest-feed discovery persists an overlap boundary and a page continuation. Capped/failed scans do not advance the completed boundary.
- Every continuation rechecks page one. Page-number pagination can shift while scanning; it is not a guaranteed lossless cursor.
- Social search distinguishes unique keyword budget from request budget. Ordinary video terms refresh after six hours, candidate signals after fifteen minutes, subject to platform gaps and budgets. Index series retain a twenty-hour interval.
- Historical album/image/title identifiers are excluded from social heat attribution. Raw historical data is preserved.

## Not Established

- No platform-wide Douyin publication feed has been obtained. Search pages currently do not expose verified video records in the tested browser session.
- Xiaohongshu search supplies stable note IDs but the tested response lacks publication times.
- Kuaishou and index extraction require further platform-specific validation; credentials/login may be required.
- TikTok advertising counts are not organic video publication counts.
- Bilibili newest-feed pagination may be truncated or shift. A feed gap is explicitly recorded.
- A previous observed count of zero is not a verified zero baseline. Scores remain null until comparable historical coverage exists.
- Candidate signal labels are not validated anomaly alerts. There is no new notification delivery path in this change.
- No platform-wide pie chart is fabricated from search results. Search attribution can overlap between terms.
- No Steam/catalog scheduling changes are included.

## Half-Day Schedule

The default mode is now `SOCIAL_SCHEDULE_MODE=halfday`, superseding the adaptive search timing above.
UTC+8 boundaries are fixed at 00:00 and 12:00 regardless of the server's local timezone.
At noon the requested publication window is [00:00,12:00); at midnight it is [previous noon,midnight).
These are start times, not guaranteed report completion times.
The first launch processes the most recently closed window. Later launches catch up persisted missed windows oldest first.
Successful subtasks are persisted and skipped after restart. Failed subtasks retry after fifteen minutes, at most three attempts total.
Bilibili broad discovery and targeted searches no longer run on their old 20/10-minute schedule in half-day mode.
Existing engagement sampling, comments, and unrelated Steam/store collection are unchanged.
Current search adapters filter counts to the requested window locally; they do not establish exhaustive server-side time-range retrieval.
Completed batches remain marked partial. Hot lists and index observations are current snapshots, not historical half-day publication feeds.

## Operations

### October 22 Revision

Cross-platform collection uses ten worker slots, with serial queues for the same
platform (including TapTap workshop/board and App Store regions). Social batches
also use ten slots by default (`SOCIAL_MAX_WORKERS`); index and video tasks for
the same provider share a lane. Each wave waits for its workers before the next
wave. Existing background subsystems retain their independent scheduling.

Automatic social keyword task generation is removed, legacy queued searches are
cancelled, and the keyword entry point rejects calls before browser/network I/O.
Existing search observations remain historical evidence, not game verification.
Normal Bilibili page-cap continuation no longer consumes failure retries.
Runtime middleware timing entries are excluded from new hot-list ingestion.
Douyin game-category metadata and complete publication coverage are still missing;
neither live rooms nor a streamer's identity prove that a short video is a game video.

The tray process must restart to load Python changes. The dashboard HTML loads on refresh.
Daily keyword and request budgets remain bounded; this is not a promise of fifteen-minute coverage for every term.
New tables are additive. Legacy evidence is not silently backfilled into validated publication records.
Use `python -m unittest discover -s tests -p test_publications.py -v` for focused validation.

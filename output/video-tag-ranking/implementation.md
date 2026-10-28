# Video Ranking Verification

Date: 2026-10-28 (Asia/Shanghai).

## Loaded

- Running GameRadar PID: 42716.
- New API revision: `2026-10-28-two-day-tags-v1`.
- Entry: `http://127.0.0.1:8765/#video`.
- The video hub and Bilibili show one Tag-growth table, without status/game
  classifiers, metric-mode tabs or a second full platform sidebar.
- Search, date, refresh, pagination and platform navigation remain available.
- Collection status is collapsed by default.

## Actual Data

- 2026-10-27 versus 2026-10-26: 3,898 comparable Tags at verification time.
- Example search and historical date switching succeeded.
- Metric: daily unique tagged-video count percentage change.
- No ten-day playback history is required.
- One-day-only Tags are counted but not assigned zero baselines.
- Completion fractions were about 3.5% for October 26 and 65.5% for October 27.
  The page shows these counts and warns that unequal completion affects rates.
  This ranking is sample posting growth, not verified whole-platform heat growth.

## Tests

- 10 Tag ranking tests passed.
- 1 frontend routing/rendering regression passed.
- 18 existing playback growth/association regressions passed.
- 2 collection pipeline integration regressions passed.
- Managed CDP browser: search, next/previous page, date switch, refresh,
  collection-status expansion, platform navigation and legacy `#kw` route passed.
- Browser reported no JavaScript errors.
- Screenshots inspected: desktop, wide desktop, mobile and narrow mobile.
- No independent browser, browser restart or native desktop control was used.

Browser details: `verification.json`.

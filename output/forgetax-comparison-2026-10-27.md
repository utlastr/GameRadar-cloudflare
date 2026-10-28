# ForgeTax: YouTube and Bilibili comparison

Observed on 2026-10-27, Asia/Shanghai. Counts are observations, not synchronized lifetime maxima.

## YouTube

Read the public search response and, for two videos, the public watch-page player metadata through background HTTP requests.

| Creator | Video ID | Observed views | Duration | Observation source |
| --- | --- | ---: | --- | --- |
| Dabble | MF__uR1czhk | 195,734 | about 66 minutes | Watch-page videoDetails |
| Zueljin Plays | 1lajGQjtc5M | 24,926 | 52:34 | Watch-page videoDetails |
| Zueljin Plays | 7pncxTWTqVw | 36,077 | 1:09:29 | Search results |
| Dabble | n3wUlMP6nvA | 80,524 | 59:30 | Search results |
| Dabble | 8_F8ONlv9SY | 43,009 | 1:14:10 | Search results |
| perpetuumworld | ioAk6uw1fVs | 143,512 | 25:41 | Search results |

Search URL: https://www.youtube.com/results?search_query=ForgeTax

Watch URLs:

- https://www.youtube.com/watch?v=MF__uR1czhk
- https://www.youtube.com/watch?v=1lajGQjtc5M
- https://www.youtube.com/watch?v=7pncxTWTqVw
- https://www.youtube.com/watch?v=n3wUlMP6nvA
- https://www.youtube.com/watch?v=8_F8ONlv9SY
- https://www.youtube.com/watch?v=ioAk6uw1fVs

The watch pages for MF__uR1czhk and 1lajGQjtc5M both link to Steam app 5245810.
Search and watch-page titles may differ. Search is neither an exhaustive census nor proof of the highest-viewed video.

## Bilibili

Located candidates in local video_records/video_samples, then refreshed three BV IDs using the existing project's bili.view function.
These HTTP reads did not update local video sample rows.

| Creator | BV ID | Views observed today | Duration | Local sample views on 2026-10-24 |
| --- | --- | ---: | --- | ---: |
| Rug | BV19gpw6XE95 | 2,689,541 | 15:36 | 2,615,133 |
| Yuanqiushu STG | BV1TSJD66EUo | 3,670,278 | 1:01 | 2,911,439 |
| Shenshi | BV1tPHb6UE9C | 1,194,530 | 4:19 | 1,135,405 |

- https://www.bilibili.com/video/BV19gpw6XE95
- https://www.bilibili.com/video/BV1TSJD66EUo
- https://www.bilibili.com/video/BV1tPHb6UE9C

Rug's locally saved description explicitly names "forge tax".
The other two were found in the local game-name tracking pool; their gameplay was not visually inspected in this research.
Their identity is less certain than Rug's description-confirmed match.
Broad Chinese forging-related keywords also retrieve unrelated games; do not sum every search hit as ForgeTax.

## Interpretation Limits

- Views are not click-through rate: impressions were not obtained.
- Video format and duration differ substantially across these samples.
- Creator baseline performance, exposure, retention, paid promotion, and audience overlap were not measured.
- No causal attribution to culture, nationality, or recommendation algorithms is established.
- Candidate explanation: creator packaging and audience fit, potentially amplified through distribution. This remains a hypothesis.
- Comparisons should next use same-age views relative to each creator's comparable-video median, and counts of independent creators outperforming their baselines.

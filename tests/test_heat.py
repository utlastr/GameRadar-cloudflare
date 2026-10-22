import unittest
from unittest.mock import patch

from radar import heat


class MultipleScoreTests(unittest.TestCase):
    def score(self, values, platforms=("bilibili",), factor=1.0):
        groups = {gid: ["steam:" + gid] for gid in values}
        entities = {("steam", gid): {"name": gid} for gid in values}
        scores = {gid: {"value": value, "full": True, "n": 2}
                  for gid, value in values.items()}
        with patch.multiple(heat,
                            entity_multiples=lambda now: ({}, {}),
                            penalty_factors=lambda *a: {"tag": {"factor": factor}},
                            region_platforms=lambda: platforms,
                            name_index=lambda *a: {}, video_links=lambda *a: {},
                            bili_videos=lambda *a: ([], {}),
                            social_videos=lambda *a: ([], {}),
                            platform_scores=lambda *a: (scores, {}),
                            big_reason=lambda *a: None,
                            _tags=lambda *a: [], _has=lambda *a: True), \
                patch.object(heat.config, "PENALTY_WORDS", {"tag": []}):
            return heat.score_groups(groups, entities)

    def test_ratios_are_not_capped_or_scaled_to_100(self):
        rows, meta = self.score({"low": 3, "normal": 10, "high": 40})
        self.assertEqual(rows["low"]["raw"], 0.3)
        self.assertEqual(rows["normal"]["raw"], 1)
        self.assertEqual(rows["high"]["raw"], 4)
        self.assertEqual(meta["score_kind"], "multiple")

    def test_platform_count_does_not_inflate_normal(self):
        rows, _ = self.score({"normal": 10}, ("bilibili", "douyin"))
        self.assertEqual(rows["normal"]["raw"], 1)
        self.assertEqual(len(rows["normal"]["videos"]), 2)

    def test_zero_baseline_has_no_score(self):
        rows, _ = self.score({"a": 0, "b": 0, "c": 10})
        self.assertEqual(rows, {})

    def test_penalty_only_changes_final(self):
        rows, _ = self.score({"normal": 10}, factor=0.5)
        self.assertEqual(rows["normal"]["raw"], 1)
        self.assertEqual(rows["normal"]["final"], 0.5)


if __name__ == "__main__":
    unittest.main()

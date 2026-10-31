import time
import unittest

from radar import rivals


class RivalTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.release = self.now - 10 * 86400
        self.entities = {
            ("steam", "source"): {
                "name": "Moon Sweepers",
                "extra": {"release_ts": self.release, "name_cn": "月球清扫"},
            }
        }

    def add(self, source, key, name="Moon Sweepers", **extra):
        self.entities[(source, key)] = {"name": name, "extra": extra}

    def result(self):
        cards = [{"gid": "steam:source"}]
        rivals.attach(cards, self.entities, {"steam:source": ["steam:source"]})
        return cards[0]["rivals"]

    def test_checks_unarchived_games_across_platforms_and_same_platform(self):
        for src in ("steam", "roblox", "taptap", "tapmaker"):
            self.add(src, src, release_ts=self.release + 86400)
        result = self.result()
        self.assertEqual(result["found_total"], 4)
        self.assertEqual({r["keys"][0] for r in result["found"]},
                         {"steam:steam", "roblox:roblox", "taptap:taptap", "tapmaker:tapmaker"})

    def test_excludes_earlier_equal_dates_and_unrelated_moon_titles(self):
        self.add("roblox", "before", release_ts=self.release - 1)
        self.add("roblox", "equal", release_ts=self.release)
        self.add("roblox", "moon", "Moon Base Simulator", release_ts=self.release + 1)
        self.add("taptap", "fuzzy", "Moon Sweep", release_ts=self.release + 1)
        self.assertEqual(self.result()["found_total"], 0)

    def test_creation_discovery_future_preorder_and_missing_dates_stay_pending(self):
        self.add("roblox", "created", created_ts=self.release + 1)
        self.add("taptap", "missing", first_seen=self.release + 1)
        self.add("steam", "future", release_ts=self.now + 86400)
        self.add("taptap", "preorder", released_time=self.release + 1, preorder=True)
        result = self.result()
        self.assertEqual(result["found_total"], 0)
        self.assertEqual(result["pending_total"], 4)

    def test_missing_original_date_cannot_confirm_any_candidates(self):
        self.entities[("steam", "source")]["extra"] = {"created_ts": self.release}
        self.add("roblox", "copy", release_ts=self.release + 1)
        result = self.result()
        self.assertIsNone(result["source_released"])
        self.assertEqual(result["found_total"], 0)
        self.assertEqual(result["pending_total"], 1)

    def test_exact_alias_and_case_punctuation_matching(self):
        self.add("taptap", "cn", "月球清扫", released_time=self.release + 1)
        self.add("roblox", "en", "MOON-SWEEPERS", release_ts=self.release + 1)
        self.assertEqual(self.result()["found_total"], 2)

    def test_taptap_and_maker_same_id_are_not_duplicate_candidates(self):
        self.add("taptap", "same", release_ts=self.release + 1)
        self.add("tapmaker", "same", release_ts=self.release + 1)
        self.assertEqual(self.result()["found_total"], 1)

    def test_dated_taptap_record_wins_over_undated_maker_duplicate(self):
        self.add("tapmaker", "same")
        self.add("taptap", "same", released_time=self.release + 1)
        result = self.result()
        self.assertEqual(result["found_total"], 1)
        self.assertEqual(result["pending_total"], 0)

    def test_original_native_id_is_never_its_own_candidate(self):
        self.assertEqual(self.result()["pending_total"], 0)
        self.assertEqual(self.result()["found_total"], 0)


if __name__ == "__main__":
    unittest.main()

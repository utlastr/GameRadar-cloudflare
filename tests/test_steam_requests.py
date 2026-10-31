import unittest
from unittest.mock import patch

from radar.sources import steam


class SteamRequestTests(unittest.TestCase):
    def test_player_request_forwards_timeout_and_retry_limit(self):
        payload = {"response": {"result": 1, "player_count": 12}}
        with patch.object(steam, "get_json", return_value=payload) as request:
            self.assertEqual(steam.current_players(42, retries=0, timeout=8), 12)
        self.assertEqual(request.call_args.kwargs, {"retries": 0, "timeout": 8})

    def test_details_forward_timeout_and_retry_limit_to_each_lookup(self):
        english = {
            "name": "Game",
            "type": "game",
            "release_date": {"date": "Oct 1, 2026", "coming_soon": False},
        }
        chinese = {"name": "游戏"}
        with patch.object(steam, "app_details", side_effect=[english, chinese]) as details, \
                patch.object(steam, "store_tags", return_value=["Action"]) as tags, \
                patch.object(steam.time, "sleep"):
            result = steam.detail(42, retries=0, timeout=10)
        self.assertEqual(result["name"], "Game")
        self.assertEqual(details.call_args_list[0].kwargs, {"retries": 0, "timeout": 10})
        self.assertEqual(details.call_args_list[1].kwargs, {"retries": 0, "timeout": 10})
        self.assertEqual(tags.call_args.kwargs, {"retries": 0, "timeout": 10})


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""检查第 11 节的数据表是否都建好。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from radar import db                                    # noqa: E402
from radar.opp import ensure                            # noqa: E402

WANT = ["opp_videos", "opp_vsamples", "opp_uploaders", "opp_demand", "opp_games", "opp_vmap",
        "opp_unknown", "opp_search", "opp_hot", "opp_steam", "opp_supply", "opp_features",
        "opp_scores", "opp_cases", "opp_models", "opp_alerts"]


def main():
    ensure()
    with db.session() as c:
        have = {r[0] for r in c.execute(
            "select name from sqlite_master where type='table' and name like 'opp_%'")}
    miss = [t for t in WANT if t not in have]
    if miss:
        print("缺表：" + "、".join(miss))
        return 1
    print("%d 张表齐全" % len(WANT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

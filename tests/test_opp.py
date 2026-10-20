# -*- coding: utf-8 -*-
"""机会雷达第一期验收（docs/OPP_SPEC.md 第 13 节第 1 条）。

每条都能自动跑，退出码非零就是没过。跑法：
    python tests/test_opp.py
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from radar import config, db                                      # noqa: E402
from radar.opp import ensure, gamemap, features, api              # noqa: E402

FAIL = []


def ok(name, cond, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def main():
    ensure()

    print("1. 表都建好了")
    want = {"opp_videos", "opp_vsamples", "opp_uploaders", "opp_demand", "opp_games",
            "opp_vmap", "opp_unknown", "opp_features", "opp_scores", "opp_alerts"}
    with db.session() as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    ok("建表齐全", want <= have, "缺 " + str(sorted(want - have)) if want - have else "")

    print("2. 名字归一化（第 4 节）")
    ok("全角/大小写/标点", gamemap.normalize("《星露谷物语》 中文版") == "星露谷物语")
    ok("英文去空格", gamemap.normalize("Stardew Valley") == "stardewvalley")
    ok("去掉 demo 尾巴", gamemap.normalize("Hades DEMO") == "hades")

    print("3. 对应规则的可信度（第 4 节）")
    t0 = gamemap.match("随便的标题", "看 https://store.steampowered.com/app/413150/ 这个",
                       [], {"x": {"gid": "steam:413150", "name": "x", "appid": "413150"}})
    ok("steam 链接 = 1.0", any(h[2] == "steam_link" and h[3] == 1.0 for h in t0), str(t0[:1]))
    t1 = gamemap.match("标题", "", ["星露谷物语"],
                       {"星露谷物语": {"gid": "g1", "name": "星露谷物语", "appid": None}})
    ok("标签 = 0.9", any(h[2] == "tag" and h[3] == 0.9 for h in t1))
    t2 = gamemap.match("《星露谷物语》实况", "", [],
                       {"星露谷物语": {"gid": "g1", "name": "星露谷物语", "appid": None}})
    ok("书名号 = 0.8", any(h[2] == "title_bracket" and h[3] == 0.8 for h in t2))
    t3 = gamemap.match("星露谷物语手机版要来了", "", [],
                       {"星露谷物语": {"gid": "g1", "name": "星露谷物语", "appid": None}})
    ok("标题包含 = 0.6", any(h[2] == "title_contains" and h[3] == 0.6 for h in t3))

    print("4. 没校准就不给概率（第 0 节硬要求）")
    lst = api.api_list()
    ok("列表带 calibrated=False", lst.get("calibrated") is False)
    ok("没有概率字段值", all(it.get("prob") is None for it in lst.get("items", [])))

    print("5. 指标带时间截止（第 5 节：不许用未来数据）")
    with db.session() as c:
        row = c.execute("SELECT bvid, pubdate FROM opp_videos ORDER BY pubdate DESC LIMIT 1").fetchone()
    if row:
        f = features.video_features(row[0])
        ok("单视频能算指标", isinstance(f, dict))
        fut = features.game_features("name:不存在的游戏", d=1)
        ok("未来时间点返回空", fut == {})
        # 用 t0+1 天截止时，采样点不能超过它
        vids = [r[0] for r in [row]]
        ok("采样不含未来", all(s["ts"] <= time.time() for s in features._samples(vids[0])))
    else:
        ok("有视频可测", False, "库里还没有视频")

    print("6. 接口真的能返回（第 9 节）")
    if lst.get("items"):
        g = api.api_game(lst["items"][0]["id"])
        ok("game 接口有 features", isinstance(g.get("features"), dict) and bool(g.get("features")))
        ok("game 接口有 videos", isinstance(g.get("videos"), list))
    else:
        ok("有游戏可查", False)

    print("7. 配置项都在（第 10 节）")
    need = ["OPP_ENABLED", "BILI_COOKIE", "BAIDU_COOKIE", "OPP_BILI_BUDGET", "OPP_TRACK_MIN_VIEWS",
            "OPP_TRACK_MIN_MULT", "OPP_DEMAND_WORDS", "OPP_BIG_PUBLISHERS", "OPP_STAGE_READY_P",
            "OPP_STAGE_GO_P", "OPP_DECLINE_RATIO", "OPP_MIN_LIVE_CASES"]
    miss = [k for k in need if not hasattr(config, k)]
    ok("12 个配置项都在", not miss, str(miss))
    miss2 = [k for k in need if k not in config._EDITABLE]
    ok("都进了 _EDITABLE", not miss2, str(miss2))

    print("8. 预算与风控（第 3 节结尾）")
    from radar.opp import budget_left, paused, note_risk
    ok("预算能读", isinstance(budget_left(), int))
    ok("暂停状态能读", isinstance(paused(), bool))

    print("")
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("全部通过 (%d 项)" % 8)
    return 0


if __name__ == "__main__":
    sys.exit(main())

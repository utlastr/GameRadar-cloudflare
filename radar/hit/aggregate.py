"""从「每周必看」里把游戏挑出来，合并同名，生成历史候选。

方案第一步（docs/HIT_SPEC.md）：
  1.3 合并去重 —— 名字是 B站 话题成员（btopic:），和商店游戏是不是同一个由游戏档案决定；
      档案自动判断（Fellegi–Sunter），没并进商店档案的标"未对上商店"
  3   按"起爆那一天"对齐 —— 起爆日 = 这个游戏第一次进必看的那一周
"""
import json
import re
import time

from .. import db
from ..opp import gamemap
from ..sources import bili

_BRACKET = bili.BRACKET
GAME_TIDS = bili.GAME_TIDS

# 标题里不可能是游戏名的词
_STOP = {"全网", "直播", "实况", "解说", "攻略", "合集", "教程", "更新", "视频", "动画", "整活",
         "搞笑", "名场面", "高能", "挑战", "速通", "评测", "资讯", "新闻", "纪录", "排行",
         "骂", "笑", "泪", "绝", "神", "牛", "菜"}

# 明显不是游戏的分区
_NONGAME = {"搞笑", "动画", "鬼畜", "生活", "娱乐", "影视", "美食", "舞蹈", "音乐", "科技", "知识", "资讯",
            "鬼畜调教", "搞笑", "动物圈", "时尚", "汽车", "运动", "纪录片", "电影", "电视剧"}

_SERIES = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*[期集季话]|下集|上集|番外|预告")


def _clean(name):
    n = (name or "").strip()
    n = re.sub(r"[《》【】\[\]（）()“”\"'、，。！？~～]", "", n)
    return n.strip()


def extract_names(title, tname):
    """从标题里抽可能的游戏名：书名号优先。"""
    out = []
    for m in _BRACKET.finditer(title or ""):
        n = _clean(m.group(1))
        if not (2 <= len(n) <= 30):
            continue
        if n in _STOP or _SERIES.search(n):
            continue
        out.append(n)
    return out


def build(rebuild=False):
    """把 hit_videos 汇总成 hit_games。只认游戏分区的视频。"""
    from . import ensure
    ensure()
    from .. import archive
    with db.session() as c:
        if rebuild:
            c.execute("DELETE FROM hit_games")
        rows = c.execute("SELECT bvid, title, tname, tid, play, mid, up, pubdate, first_week "
                         "FROM hit_videos WHERE tid IN (%s)" % ",".join(str(t) for t in GAME_TIDS)).fetchall()

    games = {}
    for r in rows:
        if r["tname"] in _NONGAME:
            continue
        names = extract_names(r["title"], r["tname"])
        if not names:
            continue
        name = names[0]
        norm = gamemap.normalize(name)
        member = gamemap.topic_member(name)
        if not member:
            continue
        g = games.setdefault(norm, {"name": name, "member": member, "first_week": 10 ** 9, "first_ts": 0,
                                    "n_video": 0, "mids": set(), "play": 0, "bvids": []})
        g["n_video"] += 1
        g["play"] += int(r["play"] or 0)
        g["mids"].add(r["mid"])
        if r["first_week"] < g["first_week"]:
            g["first_week"] = r["first_week"]
            g["first_ts"] = int(r["pubdate"] or 0)
        if len(g["bvids"]) < 50:
            g["bvids"].append(r["bvid"])

    now = int(time.time())
    n = 0
    with db.session() as c:
        for norm, g in games.items():
            gid = g["member"]
            state = "已识别" if archive.is_linked(g["member"]) else "未对上商店"
            c.execute("INSERT OR REPLACE INTO hit_games(gid, name, norm, first_ts, first_week, n_video, "
                      "n_up, play_sum, source, state, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (gid, g["name"], norm, g["first_ts"], g["first_week"], g["n_video"],
                       len(g["mids"]), g["play"], "weekly", state,
                       json.dumps(g["bvids"][:20], ensure_ascii=False)))
            n += 1
    return n


def summary(limit=30):
    with db.session() as c:
        total = c.execute("SELECT COUNT(*) FROM hit_games").fetchone()[0]
        ident = c.execute("SELECT COUNT(*) FROM hit_games WHERE state = '已识别'").fetchone()[0]
        rows = c.execute("SELECT name, first_week, n_video, n_up, play_sum, state FROM hit_games "
                         "ORDER BY n_up DESC, play_sum DESC LIMIT ?", (limit,)).fetchall()
    return {"total": total, "identified": ident, "top": [dict(r) for r in rows]}

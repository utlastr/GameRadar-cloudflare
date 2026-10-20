"""从直播标题里找游戏名（不用 AI）：主播常把游戏名写在《》【】[]「」里。

过滤规则：
- 太短、纯数字
- 含常见非游戏词（实况、有车位、摄像头、抽奖……）
- 省份、城市
- "第 X 集 / 期 / 季 / 章"、"X 倍"
调用方还会做两步过滤（见 collect.py）：只看大杂烩分区；主播昵称里含这个词的不算。
"""
import re

from . import config, match

_BR = re.compile(r"[《【\[「『〖]([^》】\]」』〗]{2,24})[》】\]」』〗]")
_STOP = [match.normalize(s) or s for s in config.KW_STOP_SUBSTRINGS]
_PLACES = {match.normalize(p) for p in config.KW_PLACES}
_PATTERNS = [
    re.compile(r"^第.{1,5}[集期季章回话]"),
    re.compile(r"\d+倍"),
    re.compile(r"^\d+[月日号点]"),
    re.compile(r"20[2-3]\d"),          # 带年份的，一般是比赛或活动
]


def extract(title):
    """返回 {规范化名字: 原文}"""
    out = {}
    for raw in _BR.findall(title or ""):
        k = re.sub(r"\s+", " ", raw).strip()
        n = match.normalize(k)
        if not match.linkable(n) or n.isdigit():
            continue
        if n in _PLACES:
            continue
        if any(s and s in n for s in _STOP):
            continue
        if any(p.search(k) for p in _PATTERNS):
            continue
        if k in config.NON_GAME_CATEGORIES:
            continue
        out.setdefault(n, k)
    return out

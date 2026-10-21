"""Game identity uses a platform namespace and its native ID only."""

_NAMESPACES = {"tapmaker": "taptap", "appcn": "appstore", "appus": "appstore"}
_NAME_ONLY = {"huya_t", "douyu_t", "bili_t", "twitch_t", "bvideo"}
# 直播平台记的是"分区名"，不是游戏编号。分区名不能当游戏身份，否则每个分区都会变成一个"游戏"。
_LIVE = {"huya", "douyu", "bili", "twitch", "kick"}


def game_key(source, entity):
    entity = str(entity)
    if source.startswith("_") or source in _NAME_ONLY:
        return None
    # Older live collectors stored category names in the entity column.
    if source in _LIVE and not entity.isdecimal():
        return None
    return f"{_NAMESPACES.get(source, source)}:{entity}" if entity else None

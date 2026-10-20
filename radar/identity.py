"""Game identity uses a platform namespace and its native ID only."""

_NAMESPACES = {"tapmaker": "taptap", "appcn": "appstore", "appus": "appstore"}
_NAME_ONLY = {"huya_t", "douyu_t", "bili_t", "twitch_t", "bvideo"}
_LIVE = {"huya", "douyu", "bili", "twitch"}


def game_key(source, entity):
    entity = str(entity)
    if source.startswith("_") or source in _NAME_ONLY:
        return None
    # Older live collectors stored category names in the entity column.
    if source in _LIVE and not entity.isdecimal():
        return None
    return f"{_NAMESPACES.get(source, source)}:{entity}" if entity else None

"""v51：从各平台存下来的资料里找游戏图片（图标 / 封面）。

各平台存的字段名和格式不一样：有的是字符串，有的是 {"small_url": ...}、{"url": ...}，有的是列表。
统一在这里取，找不到返回空字符串。Steam 没抓到详情时，用 appid 拼官方封面地址。
"""

_KEYS = ("image", "icon", "cover", "banner", "thumb", "thumbnail", "box_art")
_SUB = ("small_url", "url", "src", "medium_url", "original_url", "contentUrl", "imageUrl", "responsive")


def url_of(v, depth=0):
    if depth > 3 or v is None:
        return ""
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("//"):
            return "https:" + v
        return v if v.startswith(("http://", "https://")) else ""
    if isinstance(v, dict):
        for k in _SUB:
            u = url_of(v.get(k), depth + 1)
            if u:
                return u
        return ""
    if isinstance(v, (list, tuple)):
        for x in v:
            u = url_of(x, depth + 1)
            if u:
                return u
    return ""


def image_of(source, entity, extra):
    extra = extra or {}
    for k in _KEYS:
        u = url_of(extra.get(k))
        if u:
            return u
    if source == "steam" and str(entity).isdigit():
        return f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{entity}/header.jpg"
    return ""

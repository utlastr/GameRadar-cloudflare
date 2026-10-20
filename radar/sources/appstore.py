"""苹果 App Store 搜索（官方 iTunes Search API，免费，不需要 Key）。
注意：大约每分钟 20 次的频率限制，所以调用方每次之间要停几秒。
"""
from ..net import get_json


def search(term, country, limit=50):
    d = get_json("https://itunes.apple.com/search",
                 {"term": term, "country": country, "entity": "software", "limit": limit}) or {}
    return d.get("results") or []

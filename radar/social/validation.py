"""Source-specific contracts. Unrecognized page data is not a ranking."""
import math
from urllib.parse import urlparse

VERSION = 1
HOT_CONTRACTS = {
    "douyin_index_hot": {
        "$.current": ("douyin.com", ("topic_index",), ("rank",), "实时热点"),
        "$.rocketing": ("douyin.com", ("topic_index",), ("rank",), "飙升热点"),
    },
}
BLOCKED = {
    "douyin_hot": "抖音综合热榜已按用户要求移除",
    "bili_index_home": "首页返回营销报告和导航，尚未验证真正的指数榜单来源",
    "kuaishou_hot": "尚未验证快手热榜字段，推荐视频、A站内容和直播列表不算热榜",
    "tiktok_hashtag": "页面已改版，尚未验证热门话题榜来源",
}
KEYWORD_REASON = "关键词查询来源及目标词归属尚未验证，已停用自动查询"


def reason(job):
    return BLOCKED.get(job, KEYWORD_REASON if job not in HOT_CONTRACTS else "")


def _host_matches(url, domain):
    try:
        host = urlparse(str(url)).hostname or ""
        return host == domain or host.endswith("." + domain)
    except ValueError:
        return False


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def hot_lists(found, job, page_url):
    """Preserve official rank/metric fields, list identity, and source provenance."""
    out, seen = [], set()
    for candidate in found.get("lists", []):
        path = candidate.get("path")
        contract = HOT_CONTRACTS.get(job, {}).get(path)
        if not contract or path in seen:
            continue
        domain, metrics, ranks, name = contract
        if not _host_matches(page_url, domain):
            continue
        source = candidate.get("src") or ""
        if source != "parse":
            if not source.startswith(("fetch:", "xhr:")):
                continue
            if not _host_matches(source.split(":", 1)[1], domain):
                continue
        items, used_ranks, used_words = [], set(), set()
        valid = True
        for item in candidate.get("items", []):
            nums = item.get("nums") or {}
            rank = next((nums[k] for k in ranks if k in nums), None)
            value = next((nums[k] for k in metrics if k in nums), None)
            word = (item.get("text") or "").strip()
            if rank is None:
                continue
            if (not _number(rank) or int(rank) != rank or not _number(value)
                    or not word or rank in used_ranks or word in used_words):
                valid = False
                break
            used_ranks.add(rank)
            used_words.add(word)
            items.append(dict(item, rank=int(rank), value=value,
                              nums={k: nums[k] for k in ranks + metrics if k in nums}))
        if valid and len(items) >= 3:
            seen.add(path)
            out.append(dict(candidate, items=sorted(items, key=lambda it: it["rank"]),
                            name=name, source_url=page_url, validation_version=VERSION,
                            contract=job + ":" + path))
    return out

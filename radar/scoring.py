"""打分：重度大作判断、能不能跟（星级 > AI > 标签）、同类检查分。v19 起这些都只影响排序，不隐藏游戏。"""
from . import config, match


def _tags(info):
    return {t.lower().strip() for t in info.get("tags", []) if t}


def heavy(info):
    """重度大作 / 大厂重度产品：返回原因（只当标签、排序打折，不隐藏），否则 None。"""
    tags = _tags(info)
    heavy = bool(tags & config.TAGS_HEAVY)
    size = info.get("size_gb") or 0
    price = info.get("price_usd") or 0
    big = any(match.company_match(d, config.BIG_COMPANIES) for d in info.get("devs", []))
    minus = tags & config.TAGS_MINUS
    if heavy and (size > 20 or price > 40 or big):
        return "重度大作（" + "、".join(sorted(tags & config.TAGS_HEAVY)) + \
               (f"，{size:.0f}GB" if size > 20 else "") + (f"，${price:.0f}" if price > 40 else "") + \
               ("，大厂" if big else "") + "）"
    if big and len(minus) >= 2:
        return "大厂重度产品（" + "、".join(sorted(minus)[:3]) + "）"
    return None


def imitability(info, label, ai=None):
    """返回 (0–1 分数, 原因列表, 依据)。优先级：你打的星 > AI 读评论 > 标签。缺数据按中等算，不扣分。"""
    stars = label.get("stars")
    if stars:
        return {1: 0.1, 2: 0.3, 3: 0.55, 4: 0.8, 5: 1.0}.get(int(stars), 0.6), [f"你打的 {stars} 星"], "人工"
    res = (ai or {}).get("res")
    if res and res.get("score") is not None:
        return res["score"] / 10.0, [f"AI 读了{ai.get('src', '资料')}"], "AI"
    s, why = _tag_imit(info)
    return s, why, "标签"


def _tag_imit(info):
    tags = _tags(info)
    if not tags and info.get("size_gb") is None:
        return 0.6, ["没有标签和安装包信息，按中等算（缺数据不扣分）"]
    s, why = 0.6, []
    plus = sorted(tags & config.TAGS_PLUS)
    minus = sorted(tags & config.TAGS_MINUS)
    if plus:
        s += min(0.35, 0.08 * len(plus))
        why.append("+" + "、".join(plus[:5]))
    if minus:
        s -= 0.12 * len(minus)
        why.append("−" + "、".join(minus[:5]))
    if "3d" in tags:
        s -= 0.05
        why.append("−3D")
    size = info.get("size_gb")
    if size is not None:
        if size < 1:
            s += 0.1
            why.append("+安装包小于 1GB")
        elif size > 20:
            s -= 0.25
            why.append(f"−安装包 {size:.0f}GB")
        elif size > 8:
            s -= 0.1
            why.append(f"−安装包 {size:.0f}GB")
    if (info.get("price_usd") or 0) > 40:
        s -= 0.15
        why.append(f"−售价 ${info['price_usd']:.0f}")
    return max(0.05, min(1.0, s)), why


def window(target, res):
    """返回 (0–1 窗口分, 说明)。"""
    name = config.TARGET_NAMES.get(target, target)
    if res is None:
        return config.UNCHECKED_WINDOW, f"{name}：还没检查"
    launched = [i for i in res["items"] if i["status"] == "launched"]
    pre = [i for i in res["items"] if i["status"] == "preorder"]
    n = len(launched)
    pre_note = f"；{len(pre)} 个预约中（玩家玩不到，不算）" if pre else ""
    if n == 0:
        w = 1.0
        return w, f"{name}：没有已上线的同类{pre_note}"
    strong_line = config.STRONG_STRENGTH.get(target, 1000)
    strong = [i for i in launched if (i.get("strength") or 0) >= strong_line]
    ages = [i["age_days"] for i in launched if i.get("age_days") is not None]
    oldest = max(ages) if ages else None
    if n == 1:
        if strong:
            w = 0.25
        elif oldest is not None and oldest < 3:
            w = 0.6
        else:
            w = 0.5
    elif n <= 3:
        w = 0.3
    else:
        w = 0.1
    if oldest is not None and oldest > 7:
        w *= 0.5
    desc = f"{name}：{n} 个已上线同类" + (f"（{len(strong)} 个数据很好）" if strong else "") + \
           (f"，最早 {oldest:.0f} 天前" if oldest is not None else "") + pre_note
    return w, desc

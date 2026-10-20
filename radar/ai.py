"""AI 读评论：判断一款正在火的游戏值不值得跟（DeepSeek，OpenAI 格式接口）。

不是对话。分析打分时，程序把商店简介和玩家评论发给 AI，AI 按固定问题打分并摘出评论原话，
结果写在卡片上，只影响排序，不隐藏任何游戏。
- 密钥：radar_data/settings.json 的 AI_KEY；不填就不用 AI，改用标签判断
- 每天花费上限 AI_DAILY_LIMIT_YUAN，到了当天停用
- 结果按游戏缓存；Steam 评论数涨过 AI_REJUDGE_AT 里的数时重判一次
- 读什么：Steam 简介 + 50 条评论；TapTap / TapTap制造 简介 + 50 条评价（网页版评价接口，不用登录）；
  其他商店类平台读简介和标签；只在直播 / 视频里出现的游戏没东西可读，不判断
- 启动后马上读一轮（只有 AI 联网），之后每次完整分析读一轮；每轮的候选数、读了几款、没读的原因记在
  ai_status["last"]，面板“运行情况”能看到
"""
import json
import re
import time
import urllib.parse

from . import activity, config, db
from .net import HttpError, get_text, post_json
from .sources import steam, taptap

SYSTEM = """你在帮一个独立开发者判断：一款正在变火的游戏，值不值得他抢先做一个同玩法的游戏（Fast Follow），发到 TapTap 制造（手机上的轻量小游戏平台，简单美术，一个人几周内做完）。
只根据给你的资料（简介、分类、玩家评论）判断，资料里没有的不要编。按下面几项打 0-10 分：
- rule：好玩是不是来自一条能单独拿出来的核心规则（高分）；还是来自大量内容、关卡、剧情、美术、IP、长线养成（低分）
- strangers：不需要一群固定朋友都买同一款游戏也能玩——单人能玩，或能和陌生人随机匹配一局一局玩（高分）；主要靠凑好友合作（低分）
- replay：一局一局反复玩（高分）；有通关、全成就、玩十几个小时就玩完（低分）
- small：一个人用简单美术几周能做出能玩的版本（高分）
- mobile：搬到手机上操作还成立（高分）
score 是综合分，rule 和 strangers 最重要。
参考尺度：MECCHA CHAMELEON（把自己涂成背景色玩躲猫猫；评论说"和陌生人的公共房间也好玩""点子简单又天才"）应在 8 分以上；How to Fish（和朋友一起钓鱼、开枪、赌博的事故喜剧；评论说"4 小时通关""和朋友玩"；有任务、首领、岛屿、装备成长）应在 4 分以下。
只返回 JSON：{"core":"一句话说清核心玩法规则","rule":0,"strangers":0,"replay":0,"small":0,"mobile":0,"score":0,"evidence":["2-3 条最能支撑判断的评论或简介原话，翻译成中文，每条不超过 40 字"],"risk":"跟这款最大的问题，一句话"}"""


def _today():
    return time.strftime("%Y-%m-%d")


def _endpoint():
    """AI_URL 可以填完整地址，也可以只填中转站的基础地址（https://xxx.com 或 https://xxx.com/v1），自动补全。"""
    u = (config.AI_URL or "").strip().rstrip("/")
    if not u:
        return "https://api.deepseek.com/chat/completions"
    if not u.startswith("http"):
        u = "https://" + u
    if u.endswith("/chat/completions"):
        return u
    path = urllib.parse.urlparse(u).path
    return u + ("/chat/completions" if path and path != "/" else "/v1/chat/completions")


def _host():
    return urllib.parse.urlparse(_endpoint()).hostname or ""


def _official():
    return _host().endswith("deepseek.com")


_DEFAULT_MODEL = "deepseek-flash"
_SKIP = ("reason", "r1", "think", "embed", "rerank", "vision", "-vl", "audio", "tts", "image", "ocr", "coder")


def _rank_model(mid):
    """越小越好：DeepSeek 的普通对话模型最优先，其次其他国产对话模型；深度思考、向量、图片类不用。"""
    s = mid.lower()
    if any(k in s for k in _SKIP):
        return None
    if "deepseek" in s:
        return 0 if any(k in s for k in ("flash", "chat", "v3", "v4")) else 1
    for i, k in enumerate(("qwen", "glm", "doubao", "kimi", "moonshot", "hunyuan", "ernie", "minimax", "gpt")):
        if k in s:
            return 2 + i
    return None


def _auto_model(force=False, exclude=()):
    """中转站没指定 AI_MODEL 时：问中转站这把密钥能用哪些模型（/v1/models），自动挑一个。结果记住，不每次问。
    exclude：调用失败过的模型（比如中转站列出来但这个分组用不了），换下一个。"""
    key = f"{_endpoint()}|{str(config.AI_KEY)[-6:]}"
    saved = db.kv_get("ai_model_auto", {}) or {}
    if not force and saved.get("key") == key and saved.get("model"):
        return saved["model"]
    exclude = set(exclude) | (set(saved.get("bad") or []) if saved.get("key") == key else set())
    url = _endpoint()[:-len("/chat/completions")] + "/models"
    try:
        text = get_text(url, headers={"Authorization": "Bearer " + str(config.AI_KEY).strip(),
                                      "Accept": "application/json"}, retries=1, timeout=30)
    except RuntimeError as e:
        if "401" in str(e):
            raise HttpError(401, str(e))
        raise RuntimeError(f"问中转站有哪些模型时出错（{e}），可以在 settings.json 里手动填 AI_MODEL")
    try:
        ids = [str(m.get("id")) for m in (json.loads(text or "{}").get("data") or []) if m.get("id")]
    except (ValueError, AttributeError):
        raise RuntimeError("中转站的模型列表读不出来，在 settings.json 里手动填 AI_MODEL")
    cands = sorted((r, m) for m in ids if m not in exclude for r in [_rank_model(m)] if r is not None)
    if not cands:
        raise RuntimeError(f"中转站这把密钥能用的模型里没找到合适的（共 {len(ids)} 个："
                           f"{'、'.join(ids[:8])}），在 settings.json 里手动填 AI_MODEL")
    model = cands[0][1]
    db.kv_set("ai_model_auto", {"key": key, "model": model, "all": ids[:50], "bad": sorted(exclude),
                                "ts": int(time.time())})
    activity.say(f"AI：中转站上自动选了模型 {model}（这把密钥共能用 {len(ids)} 个模型）")
    return model


def _manual_model():
    """用户在 settings.json 里自己填了 AI_MODEL（不是默认值）就用他的。"""
    m = str(config.AI_MODEL or "").strip()
    return m if m and (m != _DEFAULT_MODEL or _official()) else ""


def _model():
    return _manual_model() or (_DEFAULT_MODEL if _official() else _auto_model())


def status():
    st = db.kv_get("ai_status", {}) or {}
    if st.get("date") != _today():
        st.update({"date": _today(), "spent": 0.0, "count": 0})
    st["enabled"] = bool(config.AI_KEY)
    st["limit"] = config.AI_DAILY_LIMIT_YUAN
    st["host"] = _host()
    auto = (db.kv_get("ai_model_auto", {}) or {}).get("model")
    st["model"] = _manual_model() or (_DEFAULT_MODEL if _official() else (f"{auto}（自动选的）" if auto else "自动选，等第一次调用"))
    st["official"] = _official()
    return st


def _save_status(st):
    db.kv_set("ai_status", st)


def _peak():
    t = time.localtime()
    return t.tm_wday < 5 and (9 <= t.tm_hour < 12 or 14 <= t.tm_hour < 18)


def _cost(usage):
    """按 DeepSeek flash 价格（元/百万 token）：缓存命中 0.04、未命中 2、输出 8；空闲时段减半。"""
    pt = usage.get("prompt_tokens") or 0
    hit = usage.get("prompt_cache_hit_tokens") or 0
    miss = usage.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, pt - hit)
    out = usage.get("completion_tokens") or 0
    f = 1.0 if _peak() else 0.5
    return f * (hit * 0.04 + miss * 2 + out * 8) / 1e6


def _bucket(n):
    if n is None or n < config.AI_MIN_REVIEWS:
        return 0
    return 1 + sum(1 for t in config.AI_REJUDGE_AT if n >= t)


# 优先读哪个平台：有玩家评论的排前面（Steam、TapTap），其次有简介的
_PREFER = ("steam", "tapmaker", "taptap", "roblox", "haoyou", "itch", "fortnite", "mistore",
           "appcn", "appus", "poki", "crazygames")
_PLAT = {"steam": "Steam", "tapmaker": "TapTap 制造", "taptap": "TapTap", "roblox": "Roblox",
         "haoyou": "好游快爆", "itch": "itch.io", "fortnite": "Fortnite 创意地图", "mistore": "小米游戏中心",
         "appcn": "App Store 中国", "appus": "App Store 美国", "poki": "Poki", "crazygames": "CrazyGames"}


def _pick_member(members):
    """选一个平台的资料给 AI 读。没有上面这些平台（只在直播 / 视频里出现）返回 None。"""
    for src in _PREFER:
        for m in members:
            if m.split(":", 1)[0] == src:
                return m
    return None


def _ent(entities, member):
    s, e = member.split(":", 1)
    return entities.get((s, e)) or {}


def _material(member, entities, card):
    """准备发给 AI 的资料。返回 (文本, 评论数, 来源说明) 或 (None, None, 原因)。"""
    s, e = member.split(":", 1)
    ent = _ent(entities, member)
    x = ent.get("extra") or {}
    name = ent.get("name") or card.get("name") or e
    alias = card.get("name_cn") if card.get("name_cn") and card.get("name_cn") != name else ""
    lines = [f"游戏：{name}" + (f"（{alias}）" if alias else "")]
    if s == "steam":
        try:
            b = steam.basic_info(e)
        except Exception:
            b = {}
        time.sleep(0.4)
        revs, total = steam.reviews_text(e, config.AI_REVIEWS)
        if not b.get("short") and not revs:
            raise RuntimeError("Steam 简介和评论都没拿到（可能连不上 Steam 商店）")
        lines.append("平台：Steam" + (f"，售价 ${x['price_usd']:.2f}" if x.get("price_usd") else "，免费"))
        if b.get("categories"):
            lines.append("Steam 分类：" + "、".join(b["categories"]))
        if b.get("genres"):
            lines.append("类型：" + "、".join(b["genres"]))
        if x.get("tags"):
            lines.append("玩家标签：" + "、".join(x["tags"][:15]))
        lines.append("简介：" + (b.get("short") or ""))
        if b.get("about"):
            lines.append("详细介绍：" + b["about"])
        if revs:
            lines.append(f"玩家评论（共 {total or len(revs)} 条，下面是最有用的 {len(revs)} 条）：")
            for r in revs:
                lines.append(f"[{'好评' if r['up'] else '差评'} {r['hours']}小时] {r['text']}")
        if (total or 0) < config.AI_MIN_REVIEWS:
            lines.append("（评论很少，证据不足的项打 5 分左右）")
        return "\n".join(lines), total, f"Steam 简介和 {len(revs)} 条评论"

    plat = _PLAT.get(s, s)
    desc = x.get("desc") or x.get("text") or x.get("rec_text") or ""
    tags = [t for t in (x.get("tags") or []) if isinstance(t, str)] or ([x["genre"]] if x.get("genre") else [])
    tags = tags or [t for t in card.get("tags") or [] if isinstance(t, str)]
    revs, total = [], None
    rev_err = ""
    if s in ("taptap", "tapmaker"):
        try:
            revs, total = taptap.reviews_text(e, config.AI_REVIEWS)
        except Exception as ex:           # 评价没拿到就只读简介，不卡住
            rev_err = str(ex)[:60]
            activity.say(f"AI：{name} TapTap 评价没拿到（{ex}），只读简介")
    if not desc and not tags and not revs:
        return None, None, "只有名字，没有简介、标签和评论，判断不了"
    lines.append(f"平台：{plat}")
    if tags:
        lines.append("标签：" + "、".join(tags[:15]))
    if x.get("rec_text") and x.get("rec_text") != desc:
        lines.append("一句话介绍：" + x["rec_text"])
    if desc:
        lines.append("简介：" + desc)
    if revs:
        lines.append(f"玩家评价（共 {total or len(revs)} 条，下面 {len(revs)} 条，星级 1-5）：")
        for r in revs:
            lines.append(f"[{r['score'] or '-'}星 {r['hours']}小时] {r['text']}")
        return "\n".join(lines), total, f"{plat} 简介和 {len(revs)} 条评价"
    lines.append("（这个平台拿不到玩家评论，只能根据简介和标签判断，证据不足的项打 5 分左右）")
    return "\n".join(lines), None, f"{plat} 简介（{'评价没拿到' if rev_err else '没有评论'}）"


_plain = {"on": False}     # 中转站不认 thinking / response_format 参数时，之后都不带
_checked = set()           # 这次运行里试通过的（地址|密钥尾号|模型）


def _model_error(e):
    b = (e.body or "").lower()
    return e.code in (400, 404, 422, 503) and any(k in b for k in ("model", "模型", "渠道", "channel", "分组"))


def _post(body):
    """发请求。中转站不认 DeepSeek 专有参数就去掉再试；自动选的模型用不了就换下一个（最多换 3 次）。"""
    extra = {"response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    head = {"Authorization": "Bearer " + str(config.AI_KEY).strip()}
    tried = []
    for _ in range(5):
        body["model"] = _model() if not tried else body["model"]
        try:
            return post_json(_endpoint(), body if _plain["on"] else {**body, **extra}, head, timeout=90)
        except HttpError as e:
            b = e.body or ""
            if not _plain["on"] and e.code in (400, 422) and ("thinking" in b or "response_format" in b
                                                              or not _model_error(e)):
                _plain["on"] = True       # 去掉两个专有参数再试
                continue
            if _model_error(e) and not _manual_model() and not _official() and len(tried) < 3:
                tried.append(body["model"])
                body["model"] = _auto_model(force=True, exclude=tried)
                continue
            raise
    raise RuntimeError("AI 接口一直调用失败")


def _ask(text):
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
            "temperature": 0.2, "max_tokens": 8192}
    d = _post(body)
    if not isinstance(d, dict):
        raise RuntimeError("AI 接口返回的不是数据，检查 AI_URL 是不是填成了网页地址")
    if d.get("error") and not d.get("choices"):
        er = d["error"]
        raise RuntimeError("AI 接口报错：" + str(er.get("message") if isinstance(er, dict) else er)[:150])
    choice = (d.get("choices") or [{}])[0]
    msg = (choice.get("message") or {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        raise RuntimeError("AI 输出达到 token 上限，回答被截断（思考过程也占用输出额度）")
    if not msg.strip():
        raise RuntimeError("AI 返回了空正文，没有可解析的分析结果")
    m = re.search(r"\{.*\}", msg, re.S)
    try:
        res = json.loads(m.group(0) if m else msg)
    except ValueError:
        raise RuntimeError("AI 回的不是规定格式：" + msg[:80])
    out = {"core": str(res.get("core") or "")[:120], "risk": str(res.get("risk") or "")[:120],
           "evidence": [str(t)[:80] for t in (res.get("evidence") or [])][:3]}
    for k in ("rule", "strangers", "replay", "small", "mobile", "score"):
        try:
            out[k] = max(0.0, min(10.0, float(res.get(k))))
        except (TypeError, ValueError):
            out[k] = None
    if out["score"] is None:
        raise ValueError("AI 没有给出综合分")
    return out, _cost(d.get("usage") or {})


def _errmsg(e):
    who = "DeepSeek 官方" if _official() else f"中转站 {_host()}"
    if isinstance(e, HttpError):
        body = re.sub(r"\s+", " ", e.body or "")[:120]
        if e.code == 401:
            if _official():
                return ("密钥不对（DeepSeek 官方返回 401）。如果你用的是中转站的密钥，"
                        "settings.json 里还要加一行 AI_URL 填中转站地址，否则密钥会被发到 DeepSeek 官方")
            return f"密钥不对（{who} 返回 401）。核对 settings.json 里的 AI_KEY 是不是这个中转站的密钥"
        if e.code == 402:
            return f"{who} 说余额不足，去充值"
        if e.code == 403:
            return f"{who} 拒绝了（403）：{body}"
        if e.code == 404:
            return f"{who} 返回 404，AI_URL 地址不对：{body}"
        if e.code == 429:
            return f"{who} 说请求太频繁或额度用完（429），下一轮再试：{body}"
        if _model_error(e):
            if _manual_model():
                return f"模型名不对（{who} 不认 {_manual_model()}），把 settings.json 里的 AI_MODEL 删掉让程序自动选，或改成中转站支持的名字：{body}"
            return f"{who} 上自动选的几个模型都用不了，可能是这个分组没有可用的模型：{body}"
        return f"{who} 返回错误 {e.code}：{body}"
    return f"{who}：{type(e).__name__}：{e}"


def apply(cards, entities, allow_network=True):
    """给卡片挂上 AI 判断（card["ai"]）。有缓存直接用；需要新判断的按热度从高到低，每轮最多 AI_PER_RUN 款。

    每轮把"候选几款、已判断几款、这轮读了几款、为什么没读"记进 ai_status["last"] 并写日志，
    面板上能直接看到 AI 有没有在干活、卡在哪。
    """
    cache = db.kv_get("ai_judge", {}) or {}
    st = status()
    now = int(time.time())
    todo, judged, no_src = [], 0, 0
    for c in cards:
        m = _pick_member(c["members"])
        c["_ai_member"] = m
        hit = cache.get(m) if m else None
        if hit:
            c["ai"] = hit
        if c["label"].get("blocked"):
            continue
        if not m:
            no_src += 1
            c["ai"] = {"res": None, "note": "只在直播 / 视频平台出现，没有商店简介和评论可读", "ts": now}
            continue
        if hit and hit.get("res"):
            judged += 1
            rv = (c.get("_reviews") or {}).get(m)
            if m.startswith("steam:") and rv is not None and _bucket(rv) > hit.get("bucket", 0):
                todo.append(c)        # 评论数涨过一个台阶，重新判断
        elif not hit or not hit.get("v") or now - hit.get("ts", 0) > 6 * 3600:
            todo.append(c)            # 没判断过，或上次资料不够、6 小时后再试（v31 以前留下的"判断不了"马上重试）
    todo.sort(key=lambda c: -c["hot"])
    last = {"ts": now, "cand": len(cards), "judged": judged, "todo": len(todo), "no_src": no_src,
            "read": 0, "fail": 0, "why": ""}
    fp = f"{_endpoint()}|{str(config.AI_KEY).strip()[-6:]}|{config.AI_MODEL}"
    if not config.AI_KEY:
        last["why"] = "没填密钥"
    elif st.get("bad_key") == fp:
        last["why"] = st.get("error") or "密钥被拒"
        last["fail"] = 1
    elif not allow_network:
        last["why"] = "这次是只用已有数据的快速重算，不联网；下一次完整分析时读"
    elif not todo:
        last["why"] = "没有需要新读的游戏"
    if last["why"]:
        if allow_network and config.AI_KEY:
            activity.say(f"AI：{len(cards)} 款候选，已判断 {judged} 款，{last['why']}")
            st["last"] = last
            _save_status(st)
        return st

    # 先发一句很短的话试一下密钥、地址、模型（几乎不花钱），不对就不去抓评论白等几分钟
    if fp not in _checked:
        try:
            _post({"messages": [{"role": "user", "content": '只回复 JSON：{"ok":1}'}], "max_tokens": 10})
            _checked.add(fp)
        except Exception as e:
            msg = _errmsg(e)
            last.update({"fail": 1, "why": msg[:160]})
            st.update({"error": msg, "err_ts": int(time.time()), "last": last})
            if isinstance(e, HttpError) and e.code in (401, 402, 403):
                st["bad_key"] = fp        # 密钥不对：改 settings.json 重开之前不再试
            _save_status(st)
            activity.say(f"AI 判断失败：{msg}")
            return st
        st.pop("bad_key", None)
    activity.say(f"AI：{len(cards)} 款候选，已判断 {judged} 款，待读 {len(todo)} 款，这轮读热度最高的 "
                 f"{min(len(todo), config.AI_PER_RUN)} 款")
    done = tried = 0
    for c in todo:
        if done >= config.AI_PER_RUN or tried >= config.AI_PER_RUN * 2:
            break
        tried += 1
        if st["spent"] >= config.AI_DAILY_LIMIT_YUAN:
            st["note"] = f"今天已花 {st['spent']:.2f} 元，到上限，停用到明天"
            last["why"] = st["note"]
            activity.say("AI：" + st["note"])
            break
        m = c["_ai_member"]
        activity.progress(done, min(len(todo), config.AI_PER_RUN), f"AI 读评论：{c['name']}")
        try:
            text, total, src = _material(m, entities, c)
        except Exception as e:
            last["fail"] += 1
            last["why"] = f"资料没拿到：{e}"[:160]
            activity.say(f"AI：{c['name']} 资料没拿到（{e}），下一轮再试")
            continue
        if text is None:
            cache[m] = {"res": None, "note": src, "ts": now, "bucket": 0, "v": 2}
            c["ai"] = cache[m]
            activity.say(f"AI：{c['name']} {src}")
            continue
        try:
            res, cost = _ask(text)
        except Exception as e:
            last["fail"] += 1
            last["why"] = _errmsg(e)[:160]
            st.update({"error": _errmsg(e), "err_ts": int(time.time())})
            activity.say(f"AI 判断失败：{_errmsg(e)}")
            if isinstance(e, HttpError) and e.code in (401, 402):
                break
            continue
        done += 1
        st["spent"] = round(st["spent"] + cost, 4)
        st["count"] += 1
        st.update({"ok_ts": int(time.time()), "error": ""})
        cache[m] = {"res": res, "src": src, "ts": int(time.time()), "bucket": _bucket(total) if total else 1,
                    "reviews": total, "cost": round(cost, 4)}
        c["ai"] = cache[m]
        activity.say(f"AI：{c['name']} 能不能跟 {res['score']:.0f}/10（{res['core'][:30]}），花费 {cost * 100:.1f} 分钱")
        db.kv_set("ai_judge", cache)      # 每读完一款就存，程序中途退出也不白花钱
    last["read"] = done
    st["last"] = last
    db.kv_set("ai_judge", cache)
    _save_status(st)
    activity.say(f"AI：这轮读了 {done} 款，失败 {last['fail']} 款，今天共 {st['count']} 款、{st['spent']:.2f} 元")
    return st

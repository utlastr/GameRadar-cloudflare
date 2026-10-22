"""游戏档案：一个游戏 = 一份档案。所有平台、商店、B站 话题都挂到同一份档案上。全部程序自动判断，没有待确认。

成员（member）：平台:编号（steam:123、taptap:456、wxmini:789、huya:分区名……）或 B站 话题 btopic:<名字>。
同一个平台编号的成员天然是一个"节点"（taptap:1 和 tapmaker:1，appcn:1 和 appus:1）。

节点怎么连成一份档案：
  1. 确定的：同一个平台编号；商店页里写着对方的商店地址；成功案例配置里写的别名；
  2. 其余（名字一样 / B站 视频简介挂着 Steam 链接）用 Fellegi–Sunter 概率记录链接（业界标准做法，
     美国人口普查局、Splink 用的就是它）：
     - 每一对候选比几项：开发商（一致 / 对不上）、上线日期差、名字多常见（几款撞这个名字）、名字长短、
       两边是什么平台、含不含大作名、多少条视频共现；
     - "真是同一个时每项长什么样"(m) 和 "不是同一个时每项长什么样"(u) 都从数据里算：
       u 里的开发商和日期从随机配对里估（随机两款几乎不可能是同一个），其余用 EM 从候选对里估；
     - 每对得到"是同一个的概率"，大于 0.5 就连（最大后验判定，不是拍的门槛）；
     - 同一个平台上有两款都可能、而且分不出谁更像（概率一样）的，都不连。
约束：一份档案里每个平台只能有一个编号（btopic 除外：一个游戏在 B站 可以有好几个叫法）；
你在详情里点"拆出"记下的"不是"，整份档案检查，不会经第三方绕过去。

纯计算在 resolve() / em()（tests/test_archive.py 直接测），读写数据库在 rebuild()。
"""
import json
import math
import random
import re
import time
from collections import Counter, defaultdict

from . import config, db, identity, match

EXEMPT = {"btopic"}
DEFAULT_GOLD = ["竞拍之王", "铸剑大师", "翡翠精英模拟器", "ForgeTax", "影之刃零"]
_URLS = [
    (re.compile(r"store\.steampowered\.com/app/(\d+)"), "steam"),
    (re.compile(r"taptap\.(?:cn|io|com)/app/(\d+)"), "taptap"),
    (re.compile(r"apps\.apple\.com/[^\s\"']*?id(\d+)"), "appstore"),
]
_SKIP_FIELDS = {"url", "image", "icon", "images", "cover", "header_image", "screenshots", "capsule", "banner"}
# 比较档位：只是把连续数分段（照 Splink 日期比较的常用分法：1 周 / 1 月 / 1 年），每段的权重由数据算出来
DEV_LEVELS = ("same", "conflict")
REL_LEVELS = ("≤7天", "≤30天", "≤1年", ">1年")
FIXED_U = ("dev", "rel")
# EM 起点的方向（只决定从哪边开始迭代，不参与最后的判定）
_INIT = {("dev", "same"): 1, ("dev", "conflict"): -1, ("rel", "≤7天"): 1, ("rel", ">1年"): -1,
         ("spec", "2"): 1, ("spec", "5+"): -1, ("len", "长"): 1, ("len", "短"): -1,
         ("vlink", "5+"): 1, ("vlink", "1"): -1, ("super", "是"): -1}


# ---------------- 基础 ----------------
def namespace(member):
    s = member.split(":", 1)[0]
    return "btopic" if s == "btopic" else identity._NAMESPACES.get(s, s)


def kind(member):
    s = member.split(":", 1)[0]
    if s == "btopic":
        return "btopic"
    if s in identity._LIVE:
        return "live"
    return "store"


def eligible(src, entity):
    return bool(str(entity)) and not src.startswith("_") and src not in identity._NAME_ONLY and "_" not in src


def node_of(src, entity):
    return identity.game_key(src, entity) or "%s:%s" % (src, entity)


def _release(src, x):
    if src == "steam" and x.get("coming_soon"):
        return None
    return x.get("release_ts") or x.get("released_time") or x.get("created_ts") or None


def _strings(x, depth=0):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict) and depth < 4:
        for k, v in x.items():
            if k not in _SKIP_FIELDS:
                for s in _strings(v, depth + 1):
                    yield s
    elif isinstance(x, list) and depth < 4:
        for v in x:
            for s in _strings(v, depth + 1):
                yield s


# ---------------- 比较 ----------------
def dev_level(da, dbb):
    da, dbb = [d for d in da or [] if d], [d for d in dbb or [] if d]
    if not da or not dbb:
        return None
    if any(match.company_match(d, dbb) for d in da) or any(match.company_match(d, da) for d in dbb):
        return "same"
    return "conflict"


def rel_level(a, b):
    if not a or not b:
        return None
    d = abs(a - b) / 86400.0
    return "≤7天" if d <= 7 else "≤30天" if d <= 30 else "≤1年" if d <= 365 else ">1年"


def len_level(k):
    if not k:
        return None
    n = len(k)
    if match.has_cjk(k):
        return "短" if n <= 3 else "中" if n <= 6 else "长"
    return "短" if n <= 5 else "中" if n <= 12 else "长"


def spec_level(n):
    return None if not n else "2" if n <= 2 else "3-4" if n <= 4 else "5+"


def vlink_level(n):
    return None if n is None else "0" if n <= 0 else "1" if n == 1 else "2-4" if n <= 4 else "5+"


def estimate_u(nodes, sample=20000, seed=0):
    """不是同一个游戏时，开发商一致 / 上线日期接近的概率：随机抽节点对估（随机两款几乎不可能是同一个）。"""
    keys = sorted(n for n in nodes if not n.startswith("btopic:"))
    cnt = {"dev": Counter(), "rel": Counter()}
    if len(keys) >= 2:
        rng = random.Random(seed)
        ns = {n: {namespace(m) for m in nodes[n]["members"]} for n in keys}
        for _ in range(int(sample)):
            a, b = rng.sample(keys, 2)
            if ns[a] & ns[b]:
                continue
            dl = dev_level(nodes[a].get("devs"), nodes[b].get("devs"))
            rl = rel_level(nodes[a].get("rel"), nodes[b].get("rel"))
            if dl:
                cnt["dev"][dl] += 1
            if rl:
                cnt["rel"][rl] += 1
    out = {}
    for f, ls in (("dev", DEV_LEVELS), ("rel", REL_LEVELS)):
        tot = float(sum(cnt[f].values()) + len(ls))
        out[f] = {l: (cnt[f][l] + 1) / tot for l in ls}
    return out


def em(vectors, u_fixed=None, iters=200, tol=1e-6):
    """Fellegi–Sunter 两类混合模型的 EM（条件独立，拉普拉斯平滑）。vectors: [{特征: 档位或 None}]。
    返回 (每对是同一个的概率, 参数)。u_fixed 里的特征只估 m，不估 u。"""
    u_fixed = u_fixed or {}
    n = len(vectors)
    if not n:
        return [], {"lambda": None, "m": {}, "u": {}, "iters": 0, "pairs": 0}
    levels = defaultdict(set)
    for v in vectors:
        for f, l in v.items():
            if l is not None:
                levels[f].add(l)
    for f in u_fixed:
        if f in levels:
            levels[f] |= set(u_fixed[f])
    w = []
    for v in vectors:
        s = sum(_INIT.get((f, l), 0) for f, l in v.items())
        w.append(1.0 / (1.0 + math.exp(-s)))
    lam, m, u, it = 0.5, {}, {}, 0
    for it in range(1, iters + 1):
        lam = min(max(sum(w) / n, 1e-6), 1 - 1e-6)
        m, u = {}, {}
        for f, ls in levels.items():
            sm = {l: 1.0 for l in ls}
            su = {l: 1.0 for l in ls}
            for v, wi in zip(vectors, w):
                l = v.get(f)
                if l is not None:
                    sm[l] += wi
                    su[l] += 1 - wi
            tm, tu = sum(sm.values()), sum(su.values())
            m[f] = {l: sm[l] / tm for l in ls}
            u[f] = dict(u_fixed[f]) if f in u_fixed else {l: su[l] / tu for l in ls}
        new = [_posterior(v, lam, m, u) for v in vectors]
        delta = max(abs(a - b) for a, b in zip(new, w))
        w = new
        if delta < tol:
            break
    return w, {"lambda": lam, "m": m, "u": u, "iters": it, "pairs": n}


def _posterior(v, lam, m, u):
    s = math.log(lam / (1 - lam))
    for f, l in v.items():
        if l is None or f not in m or l not in m[f] or not u.get(f, {}).get(l):
            continue
        s += math.log(m[f][l] / u[f][l])
    s = max(-50.0, min(50.0, s))
    return 1.0 / (1.0 + math.exp(-s))


# ---------------- 纯计算 ----------------
class _UF:
    def __init__(self, nodes, banned):
        self.p = {n: n for n in nodes}
        self.members = {n: set(nodes[n]["members"]) for n in nodes}
        self.ns = {n: {namespace(m) for m in nodes[n]["members"]} for n in nodes}
        self.nodes = {n: {n} for n in nodes}
        self.banned = banned

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def why_not(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return "already"
        both = (self.ns[ra] & self.ns[rb]) - EXEMPT
        if both:
            return "同一平台两个编号（%s）" % "/".join(sorted(both))
        small, big = sorted((self.members[ra], self.members[rb]), key=len)
        for m in small:
            if self.banned.get(m, set()) & big:
                return "你拆开过"
        return None

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        self.p[rb] = ra
        self.members[ra] |= self.members.pop(rb)
        self.ns[ra] |= self.ns.pop(rb)
        self.nodes[ra] |= self.nodes.pop(rb)
        return ra


def _rep(nodes, nids):
    """一组节点的代表成员：优先商店、再直播、最后 B站 话题；同类取最早出现的。"""
    best = None
    for n in nids:
        info = nodes[n]
        m = info["members"][0]
        k = {"store": 0, "live": 1, "btopic": 2}[kind(m)]
        key = (k, info.get("first") or 9e18, m)
        if best is None or key < best[0]:
            best = (key, m)
    return best[1]


def _vector(nodes, uf, ra, rb, info, big_words):
    A, B = uf.nodes[ra], uf.nodes[rb]
    da = [d for n in A for d in nodes[n].get("devs") or []]
    dbb = [d for n in B for d in nodes[n].get("devs") or []]
    ta = min([nodes[n]["rel"] for n in A if nodes[n].get("rel")] or [0]) or None
    tb = min([nodes[n]["rel"] for n in B if nodes[n].get("rel")] or [0]) or None
    key = info.get("name") or ""
    kinds = "|".join(sorted((kind(_rep(nodes, A)), kind(_rep(nodes, B)))))
    has_steam = any(namespace(m) == "steam" for n in list(A) + list(B) for m in nodes[n]["members"])
    has_bt = "btopic" in kinds
    return {"name": "同名" if key else "不同名",
            "dev": dev_level(da, dbb),
            "rel": rel_level(ta, tb),
            "len": len_level(key),
            "spec": spec_level(info.get("spec")),
            "kinds": kinds,
            "vlink": vlink_level(info.get("vlink", 0)) if has_bt and has_steam else None,
            "super": ("是" if any(w and w in key for w in big_words) else "否") if key else None}


def resolve(nodes, decisions=(), hard=(), prev=None, block_max=8, big_words=(), cooccur=(), u_fixed=None):
    """nodes: {节点: {"members": [...], "names": [归一化名字], "devs": [...], "rel": ts, "first": ts,
    "keyed": 有没有平台编号, "disp": 显示名}}
    decisions: [(成员a, 成员b, "same"/"diff")]；hard: [(节点a, 节点b, 方法)]；prev: 上次的 {成员: 档案号}；
    cooccur: [(btopic 节点, steam 节点, 视频数)]；u_fixed: estimate_u() 的结果（不给就现算）。
    返回 {"gid", "method", "evidence", "conflicts", "stats", "fs"}"""
    prev = prev or {}
    m2n = {m: n for n, info in nodes.items() for m in info["members"]}
    banned = defaultdict(set)
    for a, b, rel in decisions:
        if rel == "diff":
            banned[a].add(b)
            banned[b].add(a)
    uf = _UF(nodes, banned)
    method, evid, conflicts = {}, {}, []

    def link(na, nb, how, ev=None):
        why = uf.why_not(na, nb)
        if why == "already":
            return "already"
        if why:
            return why
        for n, other in ((na, nb), (nb, na)):
            if n not in method:
                method[n] = how
                if ev is not None:
                    evid[n] = dict(ev, with_=_rep(nodes, uf.nodes[uf.find(other)]))
        uf.union(na, nb)
        return None

    for a, b, rel in decisions:
        if rel == "same" and a in m2n and b in m2n:
            why = link(m2n[a], m2n[b], "human")
            if why and why != "already":
                conflicts.append({"a": a, "b": b, "why": why})
    for na, nb, how in hard:
        if na in nodes and nb in nodes:
            link(na, nb, how)

    # 候选对（按当前的档案，不是按单个节点）
    cands = {}

    def add(ra, rb, **kw):
        ra, rb = uf.find(ra), uf.find(rb)
        if ra == rb or uf.why_not(ra, rb):
            return
        k = tuple(sorted((ra, rb)))
        c = cands.setdefault(k, {"vlink": 0})
        for x, y in kw.items():
            if x == "vlink":
                c["vlink"] = max(c["vlink"], y)
            elif x == "spec":
                c["spec"] = min(c.get("spec") or y, y)
            else:
                c.setdefault(x, y)

    blocks = defaultdict(set)
    for n, info in nodes.items():
        for k in info.get("names") or []:
            if match.linkable(k):
                blocks[k].add(n)
    skipped = 0
    for k in sorted(blocks):
        roots = sorted({uf.find(n) for n in blocks[k]})
        if len(roots) < 2:
            continue
        if len(roots) > block_max:
            skipped += 1
            continue
        for i, ra in enumerate(roots):
            for rb in roots[i + 1:]:
                add(ra, rb, name=k, spec=len(roots))
    for bt, st, n in cooccur:
        if bt in nodes and st in nodes:
            add(bt, st, vlink=int(n or 0))

    keys = sorted(cands)
    vecs = [_vector(nodes, uf, a, b, cands[(a, b)], big_words) for a, b in keys]
    if u_fixed is None:
        u_fixed = estimate_u(nodes)
    probs, fs = em(vecs, u_fixed)

    # 同一个平台上两款分不出谁更像：都不连
    side_ns = {r: set(uf.ns[r]) - EXEMPT for k in keys for r in k}
    best = defaultdict(list)
    for (a, b), p in zip(keys, probs):
        for me, other in ((a, b), (b, a)):
            for ns in side_ns[other]:
                best[(me, ns)].append(p)
    for k in best:
        best[k].sort(reverse=True)

    def tied(me, other, p):
        for ns in side_ns[other]:
            ps = best[(me, ns)]
            if len(ps) >= 2 and abs(ps[0] - p) < 1e-9 and abs(ps[1] - p) < 1e-9:
                return True
        return False

    stat = Counter()
    order = sorted(range(len(keys)), key=lambda i: -probs[i])
    for i in order:
        (a, b), p, v = keys[i], probs[i], vecs[i]
        if p <= 0.5:
            stat["below"] += 1
            continue
        if tied(a, b, p) or tied(b, a, p):
            stat["tie"] += 1
            continue
        why = link(a, b, "fs", {"p": round(p, 4), "why": v, "name": cands[(a, b)].get("name") or ""})
        stat["linked" if why in (None, "already") else "blocked"] += 1

    clusters = defaultdict(list)
    for n in nodes:
        clusters[uf.find(n)].append(n)
    gid, meth, ev_out, used = {}, {}, {}, set()
    for root in sorted(clusters, key=lambda r: min((nodes[n].get("first") or 9e18) for n in clusters[r])):
        ns = clusters[root]
        members = [m for n in ns for m in nodes[n]["members"]]
        ok = set(ns)
        old = [prev[m] for m in members if prev.get(m) in ok and prev[m] not in used]
        if old:
            g = Counter(old).most_common(1)[0][0]
        else:
            keyed = sorted((n for n in ns if nodes[n].get("keyed")),
                           key=lambda n: (nodes[n].get("first") or 9e18, n))
            g = (keyed or sorted(ns))[0]
            if g in used:
                g = "arc:" + sorted(members)[0]
        used.add(g)
        founder = g if g in ok else sorted(ns)[0]
        for n in ns:
            for i, m in enumerate(nodes[n]["members"]):
                gid[m] = g
                if i:
                    meth[m] = "same_id"
                elif n == founder:
                    meth[m] = "founder"
                else:
                    meth[m] = method.get(n, "same_id")
                    if n in evid:
                        e = dict(evid[n])
                        e["with"] = e.pop("with_", None)
                        ev_out[m] = e
    stats = {"candidates": len(keys), "linked": stat["linked"], "tie": stat["tie"], "blocked": stat["blocked"],
             "below": stat["below"], "skipped": skipped}
    return {"gid": gid, "method": meth, "evidence": ev_out, "conflicts": conflicts, "stats": stats, "fs": fs}


# ---------------- 读写数据库 ----------------
def _big_words():
    return [match.normalize(x) for x in getattr(config, "KW_BIG_GAMES", []) if match.normalize(x)]


def _extra_members():
    """B站 话题成员：成功案例配置里的标签 / 标题短语 + 已登记的 B站 话题。"""
    out, hard = {}, []
    try:
        from .hit import bsearch
        from .sources import bili
        for g in bsearch.games():
            ts = bsearch.topics(g)
            terms = list(g.get("tags") or []) + list(g.get("title_terms") or [])
            for t in terms:
                n = bili.normalize(t)
                if n:
                    out.setdefault("btopic:" + n, t)
            for t in ts[1:]:
                hard.append((ts[0], t, "hit_cfg"))
    except Exception:                             # noqa: BLE001
        pass
    try:
        with db.session() as c:
            for r in c.execute("SELECT gid, name_cn FROM opp_games WHERE gid LIKE 'btopic:%'"):
                out.setdefault(r[0], r[1] or r[0].split(":", 1)[1])
    except Exception:                             # noqa: BLE001
        pass
    return out, hard


def _cooccur():
    try:
        with db.session() as c:
            return [(r[0], r[1], r[2]) for r in c.execute(
                "SELECT a.gid, b.gid, COUNT(DISTINCT a.bvid) FROM opp_vmap a JOIN opp_vmap b ON a.bvid = b.bvid "
                "WHERE a.gid LIKE 'btopic:%' AND b.method = 'steam_link' GROUP BY a.gid, b.gid")]
    except Exception:                             # noqa: BLE001
        return []


def build_nodes(entities, topics):
    nodes = {}
    for (src, e), ent in entities.items():
        if not eligible(src, e):
            continue
        n = node_of(src, e)
        x = ent.get("extra") or {}
        info = nodes.setdefault(n, {"members": [], "names": [], "devs": [], "rel": None, "first": None,
                                    "keyed": identity.game_key(src, e) is not None, "disp": ent.get("name") or n})
        info["members"].append("%s:%s" % (src, e))
        for nm in (ent.get("name"), x.get("name_cn"), x.get("name_en")):
            k = match.normalize(nm or "")
            if k and k not in info["names"]:
                info["names"].append(k)
        info["devs"] += [d for d in (x.get("developers") or []) + (x.get("publishers") or []) if d]
        if x.get("dev"):
            info["devs"].append(x["dev"])
        rel = _release(src, x)
        if rel and (not info["rel"] or rel < info["rel"]):
            info["rel"] = rel
        f = ent.get("first_seen")
        if f and (not info["first"] or f < info["first"]):
            info["first"] = f
        if match.has_cjk(ent.get("name") or "") and not match.has_cjk(info["disp"]):
            info["disp"] = ent.get("name")
    for n in nodes.values():
        n["members"].sort(key=lambda m: (m.split(":", 1)[0] not in ("taptap", "appcn", "steam"), m))
    for m, name in topics.items():
        if m not in nodes:
            k = match.normalize(name or "")
            nodes[m] = {"members": [m], "names": [k] if k else [], "devs": [], "rel": None, "first": None,
                        "keyed": False, "disp": name or m}
    return nodes


def store_links(entities, nodes):
    out = []
    for (src, e), ent in entities.items():
        if not eligible(src, e):
            continue
        me = node_of(src, e)
        for s in _strings(ent.get("extra") or {}):
            if "://" not in s and ".com" not in s and ".cn" not in s:
                continue
            for rx, ns in _URLS:
                for i in rx.findall(s):
                    t = "%s:%s" % (ns, i)
                    if t != me and t in nodes:
                        out.append((me, t, "store_link"))
    return out


def load_decisions():
    with db.session() as c:
        return [dict(r) for r in c.execute("SELECT * FROM archive_decisions")]


def rebuild(entities=None):
    """重算全部档案并存库。返回给 analyze.build_groups 用的 {成员: 档案号}（只含有平台编号的档案）。"""
    entities = entities if entities is not None else db.load_entities()
    topics, hit_hard = _extra_members()
    decs = load_decisions()
    for d in decs:
        for m in (d["a"], d["b"]):
            if m.startswith("btopic:"):
                topics.setdefault(m, m.split(":", 1)[1])
    nodes = build_nodes(entities, topics)
    with db.session() as c:
        prev = {r[0]: r[1] for r in c.execute("SELECT member, gid FROM game_members")}
    u = estimate_u(nodes, int(getattr(config, "ARCHIVE_U_SAMPLE", 20000) or 20000))
    bmax = int(getattr(config, "ARCHIVE_BLOCK_MAX", 8) or 8)
    res = resolve(nodes, [(d["a"], d["b"], d["relation"]) for d in decs],
                  store_links(entities, nodes) + hit_hard, prev, bmax, _big_words(), _cooccur(), u)
    now = int(time.time())
    disp = {m: nodes[n].get("disp") for n in nodes for m in nodes[n]["members"]}
    sizes = Counter(res["gid"].values())
    moved = [(m, prev[m], g) for m, g in res["gid"].items() if prev.get(m) and prev[m] != g]
    with db.session() as c:
        c.execute("DELETE FROM game_members")
        c.executemany("INSERT INTO game_members(member, gid, name, method, evidence, ts) VALUES (?,?,?,?,?,?)",
                      [(m, g, disp.get(m), res["method"].get(m),
                        json.dumps(res["evidence"][m], ensure_ascii=False) if m in res["evidence"] else None, now)
                       for m, g in res["gid"].items()])
        c.execute("DELETE FROM games")
        names = {}
        for m, g in res["gid"].items():
            if m.split(":", 1)[0] != "btopic" and (g not in names or match.has_cjk(disp.get(m) or "")):
                names[g] = disp.get(m)
        c.executemany("INSERT INTO games(gid, name, n, created, updated) VALUES (?,?,?,?,?)",
                      [(g, names.get(g) or disp.get(g) or g, n, now, now) for g, n in sizes.items()])
        c.execute("DELETE FROM archive_queue")
        if prev:
            c.executemany("INSERT INTO archive_log(ts, action, a, b, detail) VALUES (?,?,?,?,?)",
                          [(now, "move", m, g, "从 %s 移到 %s" % (p, g)) for m, p, g in moved[:500]])
    db.kv_set("archive_stats", dict(res["stats"], ts=now, games=len(sizes),
                                    multi=sum(1 for v in sizes.values() if v > 1),
                                    members=len(res["gid"]), moved=len(moved), block_max=bmax))
    db.kv_set("archive_fs", res["fs"])
    from . import revisit
    db.kv_set("revisit_skip", revisit.skip_list(nodes, res["gid"], _big_words(), dev_level))
    db.kv_set("archive_conflicts", res["conflicts"][:50])
    keyed = {g for m, g in res["gid"].items() if nodes.get(g, {}).get("keyed")}
    return {m: g for m, g in res["gid"].items() if g in keyed and not m.startswith("btopic:")}


# ---------------- 查询 ----------------
def gid_of(member):
    if not member:
        return None
    with db.session() as c:
        r = c.execute("SELECT gid FROM game_members WHERE member = ?", (member,)).fetchone()
    return r[0] if r else None


def members_of(gid):
    if not gid:
        return []
    with db.session() as c:
        return [r[0] for r in c.execute("SELECT member FROM game_members WHERE gid = ? ORDER BY member", (gid,))]


def is_linked(member):
    """这个成员是不是已经和一个商店 / 平台编号并在同一份档案里。"""
    ms = members_of(gid_of(member))
    return len(ms) > 1 and any(not m.startswith("btopic:") for m in ms if m != member)


def cluster_view(gid):
    with db.session() as c:
        rows = [dict(r) for r in c.execute("SELECT member, name, method, evidence FROM game_members WHERE gid = ? "
                                           "ORDER BY method != 'founder', member", (gid,))]
    for r in rows:
        try:
            r["evidence"] = json.loads(r["evidence"]) if r["evidence"] else None
        except ValueError:
            r["evidence"] = None
    return rows


def bili_block(gid):
    """档案里 B站 话题 / Steam 成员对应的视频，以及成功案例的状态。"""
    ms = members_of(gid)
    keys = [m for m in ms if m.startswith("btopic:") or m.startswith("steam:")]
    out = {"topics": [m for m in ms if m.startswith("btopic:")], "videos": 0, "ups": 0, "top": [], "cases": []}
    if keys:
        q = ",".join("?" * len(keys))
        try:
            with db.session() as c:
                r = c.execute("SELECT COUNT(DISTINCT v.bvid), COUNT(DISTINCT v.mid) FROM opp_vmap m "
                              "JOIN opp_videos v ON v.bvid = m.bvid WHERE m.gid IN (%s)" % q, keys).fetchone()
                out["videos"], out["ups"] = r[0] or 0, r[1] or 0
                out["top"] = [dict(x) for x in c.execute(
                    "SELECT v.bvid, v.title, v.owner, v.pubdate, (SELECT MAX(view) FROM opp_vsamples s "
                    "WHERE s.bvid = v.bvid) AS view FROM opp_videos v WHERE v.bvid IN "
                    "(SELECT bvid FROM opp_vmap WHERE gid IN (%s)) ORDER BY view DESC LIMIT 5" % q, keys)]
        except Exception:                         # noqa: BLE001
            pass
    try:
        from .hit import bsearch, profile
        for g in bsearch.games():
            if set(bsearch.topics(g)) & set(ms):
                st = profile.case_state(g["gid"]) or {}
                with db.session() as c:
                    r = c.execute("SELECT COUNT(*), COUNT(DISTINCT mid), SUM(play) FROM hit_topic WHERE gid = ?",
                                  (g["gid"],)).fetchone()
                out["cases"].append({"gid": g["gid"], "name": g["name"], "state": st.get("state"),
                                     "videos": r[0] or 0, "ups": r[1] or 0, "play": r[2] or 0})
    except Exception:                             # noqa: BLE001
        pass
    return out


# ---------------- 纠错（只有"拆出"一个入口） ----------------
def decide(a, b, relation, origin="split"):
    if relation not in ("same", "diff"):
        raise ValueError("relation 只能是 same / diff")
    a, b = sorted((str(a), str(b)))
    if a == b:
        raise ValueError("两边是同一个成员")
    now = int(time.time())
    with db.session() as c:
        c.execute("INSERT OR REPLACE INTO archive_decisions(a, b, relation, pattern, origin, ts) VALUES (?,?,?,?,?,?)",
                  (a, b, relation, None, origin, now))
        c.execute("INSERT INTO archive_log(ts, action, a, b, detail) VALUES (?,?,?,?,?)",
                  (now, relation, a, b, origin))
    return {"ok": True, "a": a, "b": b, "relation": relation}


def undo(a, b):
    a, b = sorted((str(a), str(b)))
    with db.session() as c:
        c.execute("DELETE FROM archive_decisions WHERE a = ? AND b = ?", (a, b))
        c.execute("INSERT INTO archive_log(ts, action, a, b, detail) VALUES (?,?,?,?,?)",
                  (int(time.time()), "undo", a, b, ""))
    return {"ok": True}


def split(member):
    """把一个成员从它的档案里拆出来：和档案里其他（不同平台编号的）成员都记成"不是"。"""
    gid = gid_of(member)
    if not gid:
        raise ValueError("这个成员不在任何档案里")
    mine = node_of(*member.split(":", 1)) if not member.startswith("btopic:") else member
    others = [m for m in members_of(gid) if m != member and
              (m.startswith("btopic:") or node_of(*m.split(":", 1)) != mine)]
    with db.session() as c:
        c.execute("DELETE FROM archive_decisions WHERE relation = 'same' AND (a = ? OR b = ?)", (member, member))
    for o in others:
        decide(member, o, "diff", origin="split")
    return {"ok": True, "member": member, "n": len(others)}


# ---------------- 报告 ----------------
def gold_report():
    gold = getattr(config, "ARCHIVE_GOLD", None) or {}
    names = list(gold) or DEFAULT_GOLD
    with db.session() as c:
        rows = [dict(r) for r in c.execute("SELECT member, gid, name FROM game_members")]
    by_gid = defaultdict(list)
    for r in rows:
        by_gid[r["gid"]].append(r)
    out = []
    for name in names:
        k = match.normalize(name)
        if not k:
            continue
        hits = []
        for r in rows:
            rk = match.normalize(r["name"] or "")
            if rk and (rk == k or (match.linkable(k) and k in rk)) and r["gid"] not in hits:
                hits.append(r["gid"])
        arcs = [{"gid": g, "members": [{"member": r["member"], "name": r["name"]} for r in by_gid[g]][:12]}
                for g in hits[:6]]
        item = {"name": name, "archives": arcs, "has_answer": name in gold}
        if name in gold:
            exp = set(gold[name] or [])
            g = next((gid_of(m) for m in exp if gid_of(m)), None)
            act = set(members_of(g)) if g else set()
            item.update({"correct": sorted(exp & act), "missing": sorted(exp - act), "extra": sorted(act - exp)})
        out.append(item)
    return out


def report():
    with db.session() as c:
        by = Counter(r[0] for r in c.execute("SELECT method FROM game_members"))
        log = [dict(r) for r in c.execute("SELECT * FROM archive_log WHERE action != 'move' "
                                          "ORDER BY ts DESC LIMIT 20")]
    try:
        from . import revisit
        rv = revisit.report()
    except Exception:                             # noqa: BLE001
        rv = None
    return {"stats": db.kv_get("archive_stats") or {}, "fs": db.kv_get("archive_fs") or {}, "by_method": dict(by),
            "revisit": rv,
            "conflicts": db.kv_get("archive_conflicts") or [], "gold": gold_report(), "log": log}

"""Same-name competition candidates, strictly released after the source game."""
import math
import time
import unicodedata
from collections import defaultdict

from . import config, match

SHOW = 8
SAME_PLAT = {"tapmaker": "taptap"}


def plat(src):
    return SAME_PLAT.get(src, src)


def _devs(ent):
    x = (ent or {}).get("extra") or {}
    out = set()
    for k in ("developers", "publishers", "dev", "publisher", "creator"):
        v = x.get(k)
        for d in (v if isinstance(v, list) else [v]):
            if isinstance(d, str) and match.normalize(d):
                out.add(match.normalize(d))
    return out


def _names(ent):
    extra = ent.get("extra") or {}
    aliases = extra.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    out = {}
    for name in [ent.get("name"), extra.get("name_en"), extra.get("name_cn"),
                 extra.get("name_local"), extra.get("full_name"), *aliases]:
        if not isinstance(name, str):
            continue
        # Preserve the full title; never match a shared word or fuzzy substring.
        key = "".join(c for c in unicodedata.normalize("NFKC", name).casefold() if c.isalnum())
        if key:
            out.setdefault(key, name)
    return out


def _release(ent, now):
    extra = ent.get("extra") or {}
    if extra.get("coming_soon") or extra.get("preorder"):
        return None
    for field in ("release_ts", "released_time"):
        raw = extra.get(field)
        if isinstance(raw, bool):
            continue
        try:
            ts = float(raw)
        except (ValueError, TypeError, OverflowError):
            continue
        if math.isfinite(ts) and 0 < ts <= now:
            return int(ts)
    # Roblox created_ts and first_seen do not establish a public release.
    return None


def attach(cards, entities, groups, big=()):
    """Search all collected store records for same-name, later-release candidates."""
    now = int(time.time())
    records = {}
    index = defaultdict(set)
    coverage = defaultdict(int)
    for (src, entity), ent in entities.items():
        if src not in config.DISCOVERY_SOURCES:
            continue
        key = f"{src}:{entity}"
        record = {"key": key, "src": src, "ent": ent, "names": _names(ent),
                  "release": _release(ent, now), "devs": _devs(ent)}
        records[key] = record
        coverage[plat(src)] += 1
        for name in record["names"]:
            index[name].add(key)
    for card in cards:
        gid = card["gid"]
        members = [records[m] for m in groups.get(gid, []) if m in records]
        source = records.get(gid)
        if source is None and members:
            dated = [r for r in members if r["release"] is not None]
            source = min(dated, key=lambda r: r["release"]) if dated else members[0]
        names = {}
        for record in members + ([source] if source else []):
            names.update(record["names"])
        release = source["release"] if source else None
        source_key = source["key"] if source else None
        own_devs = source["devs"] if source else set()
        found, pending = [], []
        candidates = set().union(*(index[n] for n in names)) if names else set()
        seen = set()
        # Prefer a dated record when TapTap and Maker contain the same app ID.
        for key in sorted(candidates, key=lambda k: (records[k]["release"] is None, k)):
            r = records[key]
            native = (plat(r["src"]), key.split(":", 1)[1])
            if source_key:
                s, e = source_key.split(":", 1)
                if native == (plat(s), e):
                    continue
            if native in seen:
                continue
            seen.add(native)
            ent = r["ent"]
            common = sorted(names.keys() & r["names"].keys())
            item = {"gid": key, "name": ent.get("name") or key, "keys": [key],
                    "plats": [plat(r["src"])], "released": r["release"],
                    "source_released": release,
                    "matched_names": [names[n] for n in common],
                    "official": bool(own_devs & r["devs"])}
            if release is None or r["release"] is None:
                item["reason"] = ("原作上线时间未核实" if release is None
                                  else "候选上线时间未核实")
                pending.append(item)
            elif r["release"] > release:
                found.append(item)
        found.sort(key=lambda r: (-r["released"], r["name"], r["gid"]))
        card["rivals"] = {
            "mode": "name_release", "own": [plat(source["src"])] if source else [],
            "source_key": source_key, "source_released": release,
            "found": found[:SHOW], "found_total": len(found),
            "pending": pending[:SHOW], "pending_total": len(pending),
            "coverage": dict(coverage),
            "unchecked": sorted({plat(s) for s in config.DISCOVERY_SOURCES} - set(coverage)),
        }

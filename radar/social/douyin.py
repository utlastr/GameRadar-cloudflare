"""Non-search Douyin observation and conservative, provenance-aware game gate."""
import json
import time

from .. import db
from . import extract, incremental, page, growth

SCHEMA = """
CREATE TABLE IF NOT EXISTS douyin_game_evidence(
 vid TEXT PRIMARY KEY, observed INTEGER, decision TEXT, reason TEXT, evidence TEXT);
"""


def classify(evidence):
    """Only verified platform classification can include a video; never infer from titles."""
    category = evidence.get("platform_game")
    if category is False:
        return "excluded", "平台明确归为非游戏"
    if category is True:
        return "included", "平台明确归为游戏"
    # Absence is not a negative classification, even with a gaming audience.
    return "unknown", "缺少可验证的平台游戏分类"


def ensure():
    with db.session() as c:
        c.executescript(SCHEMA)


def observe(caps):
    """Keep raw tag candidates without claiming undocumented fields are categories."""
    evidence = {}
    def visit(obj, source, depth=0):
        if depth > 15:
            return
        if isinstance(obj, list):
            for value in obj:
                visit(value, source, depth + 1)
        elif isinstance(obj, dict):
            vid = str(obj.get("aweme_id") or "")
            if vid.isdecimal():
                author = obj.get("author") or {}
                author = author if isinstance(author, dict) else {}
                evidence[vid] = {
                    "source": source, "platform_game": None,
                    "audience_game_share": None, "author_game_tag": None,
                    "raw_video_fields": {k: obj[k] for k in obj if any(
                        token in k.lower() for token in ("tag", "category", "label", "game", "audience"))},
                    "raw_author_fields": {k: author[k] for k in author if any(
                        token in k.lower() for token in ("tag", "category", "label", "game"))},
                    "field_semantics_verified": False,
                }
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value, source, depth + 1)
    for cap in caps:
        if not extract.junk_src(cap.get("src")):
            visit(cap.get("obj"), cap.get("src") or "unknown")
    return evidence


def save_evidence(evidence):
    ensure()
    with db.session() as c:
        for vid, value in evidence.items():
            decision, reason = classify(value)
            c.execute("INSERT OR REPLACE INTO douyin_game_evidence VALUES (?,?,?,?,?)",
                      (vid, int(time.time()), decision, reason, json.dumps(value, ensure_ascii=False)))


def run(window=None):
    """Homepage observation only: no keyword, no guessed game-channel endpoint."""
    started = time.time()
    data = page.open_capture("https://www.douyin.com/", wait=9, scroll=2)
    evidence = observe(data.get("caps") or [])
    items = extract.video_items(data.get("caps") or [], "douyin")
    save_evidence(evidence)
    growth.ingest(data.get("caps") or [])
    added = incremental.ingest("douyin", items)
    error = "" if items else "未取得结构化视频；平台分类和受众比例不可用，不能验证游戏视频"
    incremental.record_run("douyin_game_feed", started, items, added,
                           coverage="homepage_sample_not_historical", error=error)
    db.kv_set("douyin_game_probe", {
        "ts": int(time.time()), "captures": len(data.get("caps") or []), "videos": len(items),
        "requested_window": list(window) if window else None,
        "window_complete": False, "error": error,
    })
    return bool(items)


def report(include_growth=True):
    ensure()
    incremental.ensure()
    with db.session() as c:
        counts = dict(c.execute("SELECT decision,COUNT(*) FROM douyin_game_evidence GROUP BY decision"))
        rows = [dict(r) for r in c.execute(
            "SELECT e.vid,e.decision,e.reason,e.observed,p.title,p.published "
            "FROM douyin_game_evidence e LEFT JOIN pub_records p "
            "ON p.platform='douyin' AND p.vid=e.vid ORDER BY e.observed DESC LIMIT 50")]
        legacy = c.execute("SELECT COUNT(*) FROM pub_records WHERE platform='douyin' AND vid NOT IN "
                           "(SELECT vid FROM douyin_game_evidence)").fetchone()[0]
    return {"counts": counts, "historical_unverified": legacy, "rows": rows,
            "growth": growth.report() if include_growth else {},
            "probe": db.kv_get("douyin_game_probe", {}), "full_coverage": False,
            "platform_category_available": False, "audience_available": False}

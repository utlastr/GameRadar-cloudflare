"""Backfill mobile-store metadata independently of downloads and package size."""
import json
import re
import threading
import time
from html.parser import HTMLParser

SOURCES = ("haoyou", "taptap", "tapmaker")
VERSION = 1
_lock = threading.Lock()
_started = False


def company_names(value):
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return company_names(value.get("name") or value.get("title"))
    if isinstance(value, list):
        return list(dict.fromkeys(n for item in value for n in company_names(item)))
    return []


def taptap_companies(game, nuxt, app_id):
    developers = company_names(game.get("author") or game.get("developer"))
    publishers = company_names(game.get("publisher"))
    try:
        data = json.loads(nuxt) if nuxt else []
    except ValueError:
        data = []
    if isinstance(data, list):
        def resolve(value, depth=0):
            if depth > 8:
                return None
            if type(value) is int:
                if not 0 <= value < len(data):
                    return None
                item = data[value]
                return resolve(item, depth + 1) if isinstance(item, (dict, list)) else item
            if isinstance(value, dict):
                return {k: resolve(v, depth + 1) for k, v in value.items()}
            if isinstance(value, list):
                return [resolve(v, depth + 1) for v in value]
            return value

        for row in data:
            if not isinstance(row, dict) or str(resolve(row.get("id"))) != str(app_id):
                continue
            if not any(k in row for k in ("stat", "title", "name", "developer", "publisher")):
                continue
            for key in ("developer", "developers", "developer_company", "author"):
                developers += company_names(resolve(row.get(key)))
            for key in ("publisher", "publishers", "publisher_company"):
                publishers += company_names(resolve(row.get(key)))
    return list(dict.fromkeys(developers)), list(dict.fromkeys(publishers))


class _StorePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.cell = None
        self.part = None
        self.label = []
        self.value = []
        self.item = None
        self.company_link = False
        self.canonical = ""
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href", "")
        if tag == "title":
            self.in_title = True
        if tag == "td":
            self.cell, self.label, self.value = True, [], []
        if self.cell and tag in ("em", "p"):
            self.part = tag
        if tag == "li":
            self.item = {"label": [], "value": []}
        if tag == "a":
            self.company_link = bool(self.item and "/cp/" in attrs.get("href", ""))
        if tag == "span" and self.item is not None:
            self.item["in_label"] = True

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.cell and self.part:
            (self.label if self.part == "em" else self.value).append(data)
        if self.item is not None:
            if self.item.get("in_label"):
                self.item["label"].append(data)
            elif self.company_link:
                self.item["value"].append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("em", "p"):
            self.part = None
        if tag == "td" and self.cell:
            key, value = "".join(self.label).strip(), "".join(self.value).strip()
            if key and value:
                self.fields[key] = value
            self.cell = None
        if tag == "span" and self.item is not None:
            self.item["in_label"] = False
        if tag == "a":
            self.company_link = False
        if tag == "li" and self.item is not None:
            key = "".join(self.item["label"]).strip().rstrip("：:")
            value = "".join(self.item["value"]).strip()
            if key in ("开发", "发行", "厂商") and value:
                self.fields[key] = value
            self.item = None


def parse_haoyou(app_id, html):
    from .sources.taptap import size_gb_of
    page = _StorePage()
    page.feed(html)
    if page.canonical and not re.search(r"/a/" + re.escape(str(app_id)) + r"\.htm(?:[?#]|$)",
                                        page.canonical):
        raise ValueError("Good Game detail belongs to another ID")
    if any(s in "".join(page.title).lower() for s in ("404", "验证码", "verification")):
        raise ValueError("Good Game detail is unavailable")
    f = page.fields
    if not any(k in f for k in ("开发", "开发商", "发行", "发行商", "游戏大小", "更新时间")):
        raise ValueError("Good Game page has no game detail fields")
    developers = list(dict.fromkeys(f[k] for k in ("开发", "开发商") if f.get(k)))
    publishers = list(dict.fromkeys(f[k] for k in ("发行", "发行商") if f.get(k)))
    out = {"developers": developers, "publishers": publishers,
           "dev": "、".join(developers), "publisher": "、".join(publishers),
           "url": f"https://www.3839.com/a/{app_id}.htm"}
    size = size_gb_of(f.get("游戏大小") or f.get("文件大小"))
    if size is not None:
        out["size_gb"] = size
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def preserve(extra, previous):
    """Rank-only refreshes must not erase independently fetched store fields."""
    detail = (previous or {}).get("store_detail")
    if not isinstance(detail, dict):
        return extra
    out = dict(extra, store_detail=detail)
    for key, value in detail.get("fields", {}).items():
        if out.get(key) in (None, "", []) and value not in (None, "", []):
            out[key] = value
    return out


def _fetch(source, entity):
    from .sources import taptap
    if source == "haoyou":
        from .net import get_text
        url = f"https://www.3839.com/a/{entity}.htm"
        return parse_haoyou(entity, get_text(url, retries=1, timeout=20))
    app = taptap.detail(entity, strict=True)
    if app == taptap.GONE:
        raise ValueError("TapTap detail returned 404")
    extra = taptap.parse_app(app)["extra"]
    return {k: extra[k] for k in ("dev", "publisher", "size_gb", "url")
            if extra.get(k) not in (None, "", [])}


def _save(source, entity, fields, error="", now=None):
    from . import db
    now = int(time.time() if now is None else now)
    with db.session() as c:
        row = c.execute("SELECT extra FROM entities WHERE source=? AND entity=?",
                        (source, entity)).fetchone()
        if row is None:
            return False
        extra = json.loads(row[0] or "{}")
        old = extra.get("store_detail") or {}
        saved = dict(old.get("fields") or {})
        saved.update(fields)
        detail = {"version": VERSION, "attempt_at": now, "error": error[:300],
                  "checked_at": old.get("checked_at", 0) if error else now, "fields": saved}
        extra.update(fields)
        extra = preserve(extra, {"store_detail": detail})
        c.execute("UPDATE entities SET extra=? WHERE source=? AND entity=?",
                  (json.dumps(extra, ensure_ascii=False), source, entity))
    return bool(fields)


def pending(now=None):
    from . import config, db
    now = time.time() if now is None else now
    dashboard = db.kv_get("dashboard", {}) or {}
    priority = {m: rank for rank, card in reversed(list(enumerate(dashboard.get("cards") or [])))
                for m in card.get("members", [])}
    rows = []
    with db.session() as c:
        for source, entity, name, raw in c.execute(
                "SELECT source,entity,name,extra FROM entities "
                "WHERE source IN ('haoyou','taptap','tapmaker')"):
            if not config.source_on(source):
                continue
            extra = json.loads(raw or "{}")
            detail = extra.get("store_detail") or {}
            delay = 6 * 3600 if detail.get("error") else 14 * 86400 if (
                extra.get("dev") and extra.get("publisher")) else 86400
            if detail.get("version") == VERSION and now - detail.get("attempt_at", 0) < delay:
                continue
            member = source + ":" + entity
            rows.append((priority.get(member, 100000), detail.get("attempt_at", 0),
                         source, entity, name))
    return sorted(rows)


def run_batch(stop_event, limit=12):
    from . import activity, db, server
    tasks = pending()[:limit]
    updated = failed = 0
    for _, _, source, entity, name in tasks:
        if stop_event.is_set():
            break
        try:
            fields = _fetch(source, entity)
            updated += int(_save(source, entity, fields))
            companies = fields.get("dev") or fields.get("publisher") or "厂商未公开"
            activity.say(f"手游详情：{name} · {companies}")
        except Exception as e:
            _save(source, entity, {}, error=str(e))
            failed += 1
            activity.say(f"手游详情：{name} 补充失败：{e}")
        db.kv_set("mobile_details_status", {"ts": int(time.time()), "source": source,
                  "entity": entity, "name": name, "updated": updated, "failed": failed})
        if updated:
            server.reanalyze_soon()
        if stop_event.wait(2):
            break
    return updated


def start(stop_event=None):
    global _started
    with _lock:
        if _started:
            return
        _started = True
    stop_event = stop_event or threading.Event()

    def loop():
        from . import activity
        while not stop_event.is_set():
            try:
                run_batch(stop_event)
            except Exception as e:
                activity.say(f"手游详情后台异常：{e}")
            if stop_event.wait(30):
                break

    threading.Thread(target=loop, name="mobile-details", daemon=True).start()

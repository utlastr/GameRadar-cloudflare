"""B站：直播（网游、手游、单机三个大分区的直播间）和游戏区视频排行。

都是网页自己用的公开接口，不用登录；先打开一次 bilibili.com 拿到访客 cookie（buvid3），不然容易被拦。
开发时在海外服务器上测这两个接口都返回 412（被 B站风控拦了），没法验证；在你电脑上跑一次后看"运行情况"。
"""
import time
import re
from html import unescape

from ..net import get_json, get_json_strict, get_text

PARENTS = {2: "网游", 3: "手游", 6: "单机"}
_HEAD = {"Referer": "https://live.bilibili.com/", "Origin": "https://live.bilibili.com"}
_warm = {"ts": 0}


def _warmup():
    if time.time() - _warm["ts"] < 3600:
        return
    try:
        get_text("https://www.bilibili.com/", retries=0)
        get_text("https://live.bilibili.com/", retries=0)
    except Exception:
        pass
    _warm["ts"] = time.time()


def _check(d, what):
    if not isinstance(d, dict):
        raise RuntimeError(f"{what}：返回的不是数据")
    if d.get("code") not in (0, None):
        raise RuntimeError(f"{what}：B站返回 code={d.get('code')} {d.get('message') or d.get('msg') or ''}"
                           "（-412 / -352 是风控拦截，换个时间或开代理再试）")
    return d.get("data") or {}


def live_rooms(max_pages=25, progress=None):
    """三个大分区按人气排序翻页，每页 30 个直播间。"""
    _warmup()
    rooms, total = [], len(PARENTS) * max_pages
    done = 0
    for pid, pname in PARENTS.items():
        for page in range(1, max_pages + 1):
            done += 1
            if progress:
                progress(done, total)
            params = {"platform": "web", "parent_area_id": pid, "cate_id": 0, "area_id": 0,
                      "sort_type": "online", "page": page, "page_size": 30}
            url = "https://api.live.bilibili.com/room/v3/area/getRoomList"
            if page == 1 and pid == 2:
                data = _check(get_json_strict(url, params, headers=_HEAD, retries=1), "B站直播列表")
            else:
                d = get_json(url, params, headers=_HEAD, retries=1)
                if not isinstance(d, dict) or d.get("code") not in (0, None):
                    break
                data = d.get("data") or {}
            lst = data.get("list") or []
            if not lst:
                break
            for r in lst:
                rooms.append({"game": (r.get("area_name") or r.get("area_v2_name") or "").strip(),
                              "heat": int(r.get("online") or 0),
                              "streamer": str(r.get("uid") or r.get("roomid") or ""),
                              "nick": r.get("uname") or "", "title": r.get("title") or ""})
            if (lst[-1].get("online") or 0) < 20:
                break
            time.sleep(0.6)
    if not rooms:
        raise RuntimeError("B站直播一个直播间都没拿到")
    return rooms


def game_videos():
    """游戏区排行榜（近 3 天）+ 全站热门里游戏分区的视频。返回 [{bvid, title, views, tname}]。"""
    _warmup()
    head = {"Referer": "https://www.bilibili.com/"}
    out, errors = {}, []
    try:
        data = _check(get_json_strict("https://api.bilibili.com/x/web-interface/ranking/v2",
                                      {"rid": 4, "type": "all"}, headers=head, retries=1), "游戏区排行榜")
        for v in data.get("list") or []:
            out[v.get("bvid")] = v
    except Exception as e:
        errors.append(str(e))
    for pn in range(1, 6):
        try:
            data = _check(get_json_strict("https://api.bilibili.com/x/web-interface/popular",
                                          {"ps": 50, "pn": pn}, headers=head, retries=1), "全站热门")
        except Exception as e:
            errors.append(str(e))
            break
        lst = data.get("list") or []
        for v in lst:
            if "游戏" in (v.get("tname") or "") or v.get("tid") in (17, 171, 172, 65, 173, 121, 136, 19):
                out.setdefault(v.get("bvid"), v)
        if not lst or data.get("no_more"):
            break
        time.sleep(0.6)
    if not out:
        raise RuntimeError("B站游戏视频一个都没拿到：" + "；".join(errors[:2]))
    return [video_record(v) for k, v in out.items() if k], errors


def video_record(v):
    owner = v.get("owner") or {}
    title = unescape(re.sub(r"<[^>]+>", "", v.get("title") or ""))
    return {"bvid": v.get("bvid") or "", "title": title,
            "views": int((v.get("stat") or {}).get("view", v.get("play") or 0)),
            "owner_id": str(owner.get("mid") or v.get("mid") or ""),
            "owner": owner.get("name") or v.get("author") or "",
            "published": int(v.get("pubdate") or 0), "tname": v.get("tname") or "",
            "description": v.get("desc") or v.get("description") or ""}


def search_videos(term, order="pubdate"):
    data = _check(get_json_strict("https://api.bilibili.com/x/web-interface/search/type",
                  {"search_type": "video", "keyword": term, "order": order, "page": 1,
                   "page_size": 30, "tids": 4},
                  headers={"Referer": "https://search.bilibili.com/"}, retries=0, timeout=12),
                  "视频搜索")
    return [video_record(v) for v in data.get("result") or [] if v.get("bvid")]


def video_detail(bvid):
    data = _check(get_json_strict("https://api.bilibili.com/x/web-interface/view",
                  {"bvid": bvid}, headers={"Referer": "https://www.bilibili.com/"},
                  retries=0, timeout=12), "视频详情")
    if not data.get("bvid"):
        raise RuntimeError("视频详情缺少 BV 号")
    return video_record(data)

"""斗鱼直播：全站直播间列表（游戏分区、热度、标题）。"""
import time

from ..net import get_json


def live_rooms(max_pages=60, progress=None):
    """抓全站直播间。中途某一页失败会重试；抓到的页数明显少于应有页数时整轮作废（记为缺失）。

    斗鱼现在返回的总页数（pgcnt）是 0：这时一直翻到返回空列表（= 翻到底）或翻满 max_pages 页。
    """
    rooms = []
    page, pgcnt, fails = 1, None, 0
    reached_end, last_err = False, ""
    while page <= (min(pgcnt, max_pages) if pgcnt else max_pages):
        try:
            d = get_json(f"https://www.douyu.com/japi/weblist/apinc/allpage/6/{page}",
                         headers={"Referer": "https://www.douyu.com/directory/all"})
        except RuntimeError as e:
            d, last_err = None, str(e)
        data = (d or {}).get("data") if isinstance(d, dict) else None
        lst = (data or {}).get("rl") or []
        if not lst:
            answered = isinstance(data, dict)        # 斗鱼正常回了，只是这一页没有直播间
            if fails < (1 if answered and page > 1 else 3):
                fails += 1
                time.sleep(5 * fails)
                continue
            if answered and page > 1:
                reached_end = True
            elif not answered:
                last_err = last_err or "返回的不是直播间数据"
            break
        fails = 0
        pgcnt = int(data.get("pgcnt") or 0) or pgcnt
        for r in lst:
            if r.get("type") not in (None, 1):
                continue
            rooms.append({
                "game": (r.get("c2name_display") or r.get("c2name") or "").strip(),
                "heat": int(r.get("ol") or 0),
                "title": r.get("rn") or "",
                "streamer": str(r.get("uid") or r.get("nn") or ""),
                "nick": r.get("nn") or "",
            })
        if progress:
            progress(page, min(pgcnt, max_pages) if pgcnt else max_pages)
        page += 1
        time.sleep(0.6)
    got = page - 1
    if pgcnt:
        want = min(pgcnt, max_pages)
        pages = f"全站共 {pgcnt} 页，本轮抓 {got} / {want} 页"
    else:
        want = got
        pages = (f"本轮抓 {got} 页（斗鱼没给总页数，" +
                 ("翻到底了）" if reached_end else ("翻满上限）" if got >= max_pages else "中途失败）")))
    print(f"{time.strftime('%H:%M:%S')}  斗鱼：{pages}，{len(rooms)} 个直播间")
    if got == 0:
        raise RuntimeError(f"第 1 页就没拿到直播间：{last_err or '斗鱼返回空列表'}")
    if pgcnt and got < want * 0.9:
        raise RuntimeError(f"只抓到 {got} / {want} 页，可能被限流，这一轮作废（记为缺失）")
    if not pgcnt and not reached_end and got < max_pages:
        raise RuntimeError(f"翻到第 {page} 页时失败（{last_err or '原因不明'}），这一轮作废（记为缺失）")
    return rooms

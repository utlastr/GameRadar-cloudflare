"""虎牙直播：全站直播间列表（每个直播间属于哪个游戏分区、热度、标题）。"""
import time

from ..net import get_json


def live_rooms(max_pages=80, progress=None):
    """抓全站直播间。中途某一页失败会重试；抓到的页数明显少于应有页数时整轮作废（记为缺失，不记成热度下降）。"""
    rooms = []
    page, total, fails = 1, 1, 0
    while page <= min(total, max_pages):
        try:
            d = get_json("https://www.huya.com/cache.php",
                         {"m": "LiveList", "do": "getLiveListByPage", "tagAll": 0, "page": page},
                         headers={"Referer": "https://www.huya.com/l"})
        except RuntimeError:
            d = None
        data = (d or {}).get("data") or {}
        lst = data.get("datas") or []
        if not lst:
            if fails < 3:
                fails += 1
                time.sleep(5 * fails)
                continue
            break
        fails = 0
        total = int(data.get("totalPage") or total)
        for r in lst:
            rooms.append({
                "game": (r.get("gameFullName") or "").strip(),
                "heat": int(r.get("totalCount") or 0),
                "title": r.get("introduction") or r.get("roomName") or "",
                "streamer": str(r.get("uid") or r.get("nick") or ""),
                "nick": r.get("nick") or "",
            })
        if progress:
            progress(page, min(total, max_pages))
        page += 1
        time.sleep(0.6)
    want, got = min(total, max_pages), page - 1
    print(f"{time.strftime('%H:%M:%S')}  虎牙：全站共 {total} 页，本轮抓 {got} / {want} 页，{len(rooms)} 个直播间")
    if got < want * 0.9:
        raise RuntimeError(f"只抓到 {got} / {want} 页，可能被限流，这一轮作废（记为缺失）")
    return rooms

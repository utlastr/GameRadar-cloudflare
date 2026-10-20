"""Twitch 直播（官方 Helix 接口）。需要你自己免费申请的 Client ID 和 Client Secret：

1. 打开 https://dev.twitch.tv/console ，用 Twitch 账号登录（要先在账号设置里开两步验证）
2. Applications → Register Your Application：名字随便填，OAuth Redirect URL 填 http://localhost ，
   Category 选 Analytics Tool，Client Type 选 Confidential
3. 点 Manage，复制 Client ID；点 New Secret，复制 Client Secret
4. 填进 radar_data/settings.json："TWITCH_CLIENT_ID": "...", "TWITCH_CLIENT_SECRET": "..."，然后重开程序
"""
import time

from ..net import get_json_strict, post_form

_token = {"v": None, "exp": 0, "cid": None}


def _get_token(cid, secret):
    if _token["v"] and _token["cid"] == cid and time.time() < _token["exp"] - 600:
        return _token["v"]
    try:
        d = post_form("https://id.twitch.tv/oauth2/token",
                      {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    except Exception as e:
        raise RuntimeError(f"Twitch 登录失败（多半是 Client ID 或 Secret 填错了）：{e}")
    if not d.get("access_token"):
        raise RuntimeError(f"Twitch 登录失败：{str(d)[:200]}")
    _token.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in") or 3600), cid=cid)
    return _token["v"]


def live_rooms(cid, secret, max_pages=20, progress=None):
    """观看人数最多的直播（每页 100 个），一直翻到观看人数很少为止。"""
    if not cid or not secret:
        raise RuntimeError("没填 Twitch 密钥：需要在 radar_data/settings.json 里填 TWITCH_CLIENT_ID 和 "
                           "TWITCH_CLIENT_SECRET（免费申请，步骤见“各平台 → 直播平台 → Twitch”）")
    tok = _get_token(cid, secret)
    headers = {"Client-Id": cid, "Authorization": "Bearer " + tok}
    rooms, cursor = [], None
    for p in range(1, max_pages + 1):
        if progress:
            progress(p, max_pages)
        params = {"first": 100}
        if cursor:
            params["after"] = cursor
        try:
            d = get_json_strict("https://api.twitch.tv/helix/streams", params, headers=headers, retries=1)
        except Exception as e:
            if "401" in str(e):
                _token["v"] = None
            if rooms:
                break
            raise
        data = d.get("data") or []
        for s in data:
            rooms.append({"game": s.get("game_name") or "", "heat": s.get("viewer_count") or 0,
                          "streamer": s.get("user_id") or s.get("user_login"), "nick": s.get("user_name") or "",
                          "title": s.get("title") or ""})
        cursor = (d.get("pagination") or {}).get("cursor")
        if not data or not cursor or (data[-1].get("viewer_count") or 0) < 30:
            break
        time.sleep(0.3)
    return rooms

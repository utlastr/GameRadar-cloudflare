"""右下角通知。后台模式下用托盘图标发通知；命令行模式下用 Windows 自带功能弹出。"""
import subprocess
import sys
import time

from . import config, db

_tray = None

_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
{sound}
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(15000, '{title}', '{msg}', 'Info')
Start-Sleep -Seconds 16
$n.Dispose()
"""


def set_tray(t):
    global _tray
    _tray = t


def status(text):
    """更新托盘图标的鼠标悬停文字。"""
    if _tray:
        try:
            _tray.set_tip(text)
        except Exception:
            pass


def popup(title, msg, sound=False):
    if _tray:
        try:
            if _tray.balloon(title, msg, sound):
                return
        except Exception:
            pass
    if sys.platform != "win32":
        print(f"[提醒] {title}：{msg}")
        return
    t, m = title.replace("'", "''")[:60], msg.replace("'", "''")[:250]
    script = _PS.format(title=t, msg=m,
                        sound="[System.Media.SystemSounds]::Exclamation.Play()" if sound else "")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         creationflags=0x08000000)
    except Exception as e:
        print("通知失败：", e)


def process(cards):
    """按提醒预算和冷却时间决定通知哪些。"""
    if not config.NOTIFY_ENABLED:
        return
    now = int(time.time())
    today = time.strftime("%Y-%m-%d")
    day_start = int(time.mktime(time.strptime(today, "%Y-%m-%d")))
    with db.session() as c:
        sent_today = c.execute("SELECT COUNT(*) FROM alerts WHERE ts >= ?", (day_start,)).fetchone()[0]
        last = {r[0]: (r[1], r[2]) for r in c.execute("SELECT akey, MAX(ts), score FROM alerts GROUP BY akey")}
    video_sent = sum(v.get("day") == today for v in (db.kv_get("video_alerts", {}) or {}).values())
    budget = config.NOTIFY_MAX_PER_DAY - sent_today - video_sent
    ranked = sorted((c for c in cards if c.get("final") is not None), key=lambda x: -x["final"])
    todo = []
    for i, card in enumerate(ranked[:max(0, budget)], 1):
        prev = last.get(card["akey"])
        if prev and now - prev[0] < config.NOTIFY_COOLDOWN_HOURS * 3600:
            continue
        todo.append((i, card))
    for i, card in todo:
        level = "第 %d 名" % i
        popup(f"热度雷达 [{level}] {card['name']}", f"综合互动 {card['final']:.2f} 倍正常水平", sound=i == 1)
        with db.session() as c:
            c.execute("INSERT INTO alerts(akey, ts, score, level) VALUES (?, ?, ?, ?)",
                      (card["akey"], now, card["final"], level))
        time.sleep(1)

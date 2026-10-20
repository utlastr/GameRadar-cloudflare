"""每天一次：压缩旧数据（不删除）、备份数据库。

备份规则（v70 起，硬约束）：**只有固定文件名，永远覆盖，绝不新增。**
目录里永远只有这两个文件：
    radar_backup.db       最新一份
    radar_backup.prev.db  上一份（覆盖前留的一手，固定只有一份）
写入过程：先写 .tmp → 校验完整性 → 通过才替换，坏的那份不会覆盖好的。
任何 radar_YYYYMMDD.db 这种按日期新增的旧备份会被自动清掉，避免越堆越多。
"""
import os
import re
import sqlite3
import time

from . import config, db

BACKUP_NAME = "radar_backup.db"
PREV_NAME = "radar_backup.prev.db"
_LEGACY_RE = re.compile(r"^radar_\d{8}\.db$")


def downsample():
    """超过 RAW_KEEP_DAYS 天的高频数据压缩成每小时一条（最高、平均、最低），永久保存。"""
    cutoff = int(time.time()) - config.RAW_KEEP_DAYS * 86400
    with db.session() as c:
        c.execute("""INSERT OR REPLACE INTO metrics_hourly(hour_ts, source, entity, metric, vmin, vavg, vmax)
                     SELECT (ts / 3600) * 3600, source, entity, metric, MIN(value), AVG(value), MAX(value)
                     FROM metrics WHERE ts < ? GROUP BY (ts / 3600) * 3600, source, entity, metric""",
                  (cutoff,))
        c.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))


def _check_integrity(path):
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            return c.execute("pragma quick_check").fetchone()[0] == "ok"
    except Exception:
        return False


def _prune_legacy(d):
    """清掉历史遗留的按日期命名的备份，保证目录里不会越堆越多。"""
    removed = 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        if _LEGACY_RE.match(name) or name.startswith(BACKUP_NAME + ".tmp"):
            try:
                os.remove(os.path.join(d, name))
                removed += 1
            except OSError:
                pass
    return removed


def backup():
    d = config.BACKUP_DIR
    if not d:
        return None
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, BACKUP_NAME)
    prev = os.path.join(d, PREV_NAME)
    tmp = dst + ".tmp"

    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass

    with db._lock:
        src = sqlite3.connect(config.DB_PATH, timeout=60)
        out = sqlite3.connect(tmp)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()

    if not _check_integrity(tmp):                 # 坏备份绝不覆盖好备份
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("备份文件校验失败，已丢弃，保留原备份")

    if os.path.exists(dst):
        os.replace(dst, prev)                     # 只留一份"上一版"，固定名字
    os.replace(tmp, dst)                          # 原子覆盖

    _prune_legacy(d)
    return dst


def daily():
    t0 = time.time()
    ok, detail = True, ""
    try:
        downsample()
        dst = backup()
        detail = f"覆盖备份到 {dst}" if dst else "未设置备份文件夹"
    except Exception as e:
        ok, detail = False, str(e)
    db.log_run("maintenance", time.time() - t0, ok, detail)

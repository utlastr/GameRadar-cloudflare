"""图表组件 ECharts（Apache-2.0 开源许可，可以自由使用和分发）。

第一次运行时自动下载到 radar_data/vendor，之后不联网也能用；自动更新不会删掉它。
国内镜像优先，失败再试国外的。
"""
import os
import threading
import time
import urllib.request

from . import config, net

VERSION = "5.5.1"
PATH = os.path.join(config.DATA_DIR, "vendor", f"echarts-{VERSION}.min.js")
MIRRORS = [
    f"https://registry.npmmirror.com/echarts/{VERSION}/files/dist/echarts.min.js",
    f"https://cdn.bootcdn.net/ajax/libs/echarts/{VERSION}/echarts.min.js",
    f"https://lib.baomitu.com/echarts/{VERSION}/echarts.min.js",
    f"https://cdn.jsdelivr.net/npm/echarts@{VERSION}/dist/echarts.min.js",
    f"https://fastly.jsdelivr.net/npm/echarts@{VERSION}/dist/echarts.min.js",
    f"https://unpkg.com/echarts@{VERSION}/dist/echarts.min.js",
]
_lock = threading.Lock()
_last_fail = 0.0


def _ok():
    return os.path.isfile(PATH) and os.path.getsize(PATH) > 300000


def ensure_echarts():
    """返回本地文件路径；下载不下来返回 None（5 分钟内不重复尝试）。"""
    global _last_fail
    if _ok():
        return PATH
    with _lock:
        if _ok():
            return PATH
        if time.time() - _last_fail < 300:
            return None
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        for url in MIRRORS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": net._UA})
                with net._get_opener().open(req, timeout=40) as r:
                    data = r.read()
                if len(data) > 300000 and b"echarts" in data[:20000]:
                    tmp = PATH + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, PATH)
                    print(f"{time.strftime('%H:%M:%S')}  图表组件已下载（{url.split('/')[2]}）")
                    return PATH
            except Exception:
                continue
        _last_fail = time.time()
        print(f"{time.strftime('%H:%M:%S')}  图表组件下载失败，面板上的图表暂时显示不了（检查网络或在设置里填代理）")
        return None

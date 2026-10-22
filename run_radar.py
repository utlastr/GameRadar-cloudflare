"""启动热度雷达。

    python run_radar.py              一直运行：定时抓取 + 打开看板
    python run_radar.py --once       只抓一次、分析一次就退出（测试用）
    python run_radar.py --serve      只打开看板，不抓数据
    python run_radar.py --no-browser 不自动打开浏览器
"""
import argparse
import time
import webbrowser

from radar import config, db, main, server

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    if a.once:
        d = main.run_once()
        print(f"完成：必看 {d.get('totals', {}).get('cards', len(d['cards']))} 款")
        for name, st in (d.get("sources") or {}).items():
            print(f"  {name}: {'正常 ' + str(st.get('count')) + ' 条' if not st.get('error') else '失败 ' + st['error']}")
    elif a.serve:
        db.init()
        _, url = server.start()
        print("看板：", url)
        if not a.no_browser:
            webbrowser.open(url)
        while True:
            time.sleep(3600)
    else:
        main.run_forever(open_browser=not a.no_browser and config.OPEN_BROWSER)

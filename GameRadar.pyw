"""双击这个文件启动热度雷达（后台运行，没有黑色窗口，右下角托盘出现图标）。"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from radar import app
    app.run()
except Exception:
    err = traceback.format_exc()
    try:
        os.makedirs(os.path.join(HERE, "radar_data"), exist_ok=True)
        with open(os.path.join(HERE, "radar_data", "error.log"), "a", encoding="utf-8") as f:
            f.write(err + "\n")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "热度雷达启动失败，错误已保存到 radar_data\\error.log：\n\n" + err[-1500:], "热度雷达", 0x10)
    except Exception:
        pass

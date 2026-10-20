"""临时用：找出引力引擎（rank.gravity-engine.com）请求里 gravity-signature 签名是怎么算的。

助手那边读不全它的网页代码，所以让程序在你电脑上下载它的公开网页代码，把加请求头 / 算签名附近的代码
存到 radar_data/gravity_probe.txt，你把这个文件发给助手。只下载公开网页，不登录，不发任何你的信息。
每个版本只跑一次。

v44：v42/v43 只搜完整的 "gravity-signature" 字样，网页代码里这个词是拆开拼的，所以什么都没搜到，
文件里只有一串文件名。现在改搜更宽的词（ignature / imestamp / 请求拦截器 interceptors.request.use /
接口名 public_list），只下载网页真正用到的代码文件（名字带哈希的那种），文件最上面直接写找到没有。
"""
import os
import re
import threading
import time

from . import config, db
from .net import get_text

BASE = "https://rank.gravity-engine.com/"
# 按重要程度排：前面的找到就多给篇幅
KEYS = ["ignature", "interceptors.request.use", "imestamp", "ravity-", "public_list", "apprank"]
OUT = os.path.join(config.DATA_DIR, "gravity_probe.txt")
MAX_CHARS = 60000      # 文件总长度上限，方便整段复制发出
SPAN = 1200            # 每处前后各取多少字


def _say(msg):
    print(f"{time.strftime('%H:%M:%S')}  引力引擎签名探测：{msg}")


def run():
    _say("开始，下载 rank.gravity-engine.com 首页")
    html = get_text(BASE, timeout=30, retries=1) or ""
    _say(f"首页下载完成（{len(html)} 字）")
    files = []
    for m in re.finditer(r'(?:src|href)="\./(assets/[^"]+\.js)"', html):
        if m.group(1) not in files:
            files.append(m.group(1))
    snippets, got, i = [], {}, 0
    while i < len(files) and i < 60:
        f = files[i]
        i += 1
        _say(f"下载第 {i} 个代码文件 {f}")
        try:
            js = get_text(BASE + f, timeout=60, retries=1) or ""
        except Exception as e:
            _say(f"{f} 下载失败：{e}")
            continue
        if js.lstrip()[:15].lower().startswith("<!doctype"):
            continue  # 不存在的文件，服务器回的是首页
        # 只跟网页自己的代码文件（名字形如 Xxx-AbC123de.js），不跟第三方库里提到的 ./md5.js 之类
        for m in re.finditer(r'"\./([A-Za-z0-9_]+-[A-Za-z0-9_-]{6,}\.js)"', js):
            p = "assets/" + m.group(1)
            if p not in files:
                files.append(p)
        for k in KEYS:
            for m in list(re.finditer(re.escape(k), js))[:4]:
                got[k] = got.get(k, 0) + 1
                a, b = max(0, m.start() - SPAN), m.end() + SPAN
                snippets.append((KEYS.index(k), f"## 文件 {f}  位置 {m.start()}  找到“{k}”\n{js[a:b]}\n"))
        # v46：签名已经对上了，还差“返回的数据怎么解密”。解密函数就在加签名代码的前面，整段取出来
        p = js.find('"gravity-timestamp"')
        if p >= 0:
            got["解密代码"] = 1
            snippets.append((-2, f"## 文件 {f}  加签名代码前面 6000 字（解密函数在这里）\n{js[max(0, p - 6000):p + 300]}\n"))
            for m in list(re.finditer(r"AES\.decrypt", js))[:5]:
                snippets.append((-1, f"## 文件 {f}  位置 {m.start()}  找到“AES.decrypt”\n{js[max(0, m.start() - 700):m.end() + 400]}\n"))
    snippets.sort(key=lambda x: x[0])
    if got.get("解密代码"):
        snippets = [s for s in snippets if s[0] < 0]   # 只留解密相关的，文件短一点
    found = got.get("ignature", 0) + got.get("interceptors.request.use", 0)
    head = [f"引力引擎签名探测  {time.strftime('%Y-%m-%d %H:%M:%S')}  共下载 {i} 个代码文件",
            "结果：" + ("找到了解密代码，把整个文件发给助手" if got.get("解密代码") else
                        "没找到解密代码，也把整个文件发给助手"),
            "各关键词找到几处：" + "，".join(f"{k} {got.get(k, 0)}" for k in KEYS), ""]
    body, size = [], sum(len(x) for x in head)
    for _, s in snippets:
        if size + len(s) > MAX_CHARS:
            break
        body.append(s)
        size += len(s)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(head + body))
    return OUT, found, got


def start():
    # v45：不再判断“这个版本跑过没有”，每次启动都跑（只下载二十来个网页文件，几十秒）
    _say("马上开始（每次启动都会跑一次）")

    def go():
        time.sleep(2)
        try:
            p, found, got = run()
            _say(("找到了解密代码" if got.get("解密代码") else "没找到解密代码") +
                 f"（各词：{got}）。结果在 {p}，用记事本打开，全选复制发给助手")
        except Exception as e:
            _say(f"失败（{e}），下次启动程序会再试")

    threading.Thread(target=go, daemon=True).start()

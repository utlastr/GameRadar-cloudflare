"""Render the reviewed BidKing evidence map; never modify the radar database."""
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
CST = timezone(timedelta(hours=8))
DB = ROOT / "radar_data" / "radar.db"

# D = directly observed page; R = attributed report; U = unresolved.
# Blank periods are unknown, not evidence of absence.
ROWS = [
    ("B站视频", ["D:01-11试玩", "D:03-05录播", "D:04-18试玩", "", "D:仍有内容"], "S4 / S5 / S6"),
    ("抖音", ["R:主播带量", "", "", "R:话题过亿*", "D:官方作品"], "S7 / S10"),
    ("微信公众号", ["", "D:03-10报道", "", "", "D:原文可读"], "S8"),
    ("百度贴吧", ["D:01-13建吧", "", "D:04-12吧规", "", "D:帖子可读"], "S9"),
    ("AcFun", ["", "", "R:索引04-24", "R:索引05-09", ""], "S11"),
    ("搜狐 / 游戏葡萄", ["", "", "", "D:05-06采访", ""], "S7"),
    ("QQ群", ["R:入群激增", "", "", "", ""], "S7；未进入群"),
    ("闲鱼", ["R:测试码交易", "", "", "", ""], "S7；未核实交易"),
    ("虎牙直播", ["", "", "", "", "D:10-01起采样"], "本地数据库"),
    ("斗鱼直播", ["", "", "", "", "D:10-01起采样"], "本地数据库"),
    ("B站直播", ["", "", "", "", "D:10-03起采样"], "本地数据库"),
    ("TapTap", ["", "", "", "", "D:785213"], "S12；Steam资料页"),
    ("微博", ["", "", "", "", "R:商店给出入口"], "S1；正文被登录拦截"),
    ("头条搜索", ["", "", "", "", "D:跨站索引"], "S13；不算原生传播"),
    ("快手 / 小红书 / 知乎", ["", "", "", "", "U:历史待核实"], "正文受限"),
    ("视频号等其他渠道", ["", "", "", "", "U:尚未覆盖"], "未覆盖"),
]
SOURCES = [
    {"id": "S1", "url": "https://store.steampowered.com/app/4128580/BidKing/",
     "finding": "正式发行2026-04-16；开发商MindSurge Network & Games；商店给出抖音、微博、QQ入口。"},
    {"id": "S2", "url": "https://steamdb.info/app/4128580/charts/",
     "finding": "57450 concurrent players on 25 April 2026；网站说明所有时间为UTC。全球Steam指标。"},
    {"id": "S3", "url": "https://steamcharts.com/app/4128580",
     "finding": "2026年4月峰值57125；5月46700；6月26476；与SteamDB不完全一致，不合并。"},
    {"id": "S4", "url": "https://www.bilibili.com/video/BV1Vc6RBkETb/",
     "finding": "張小臣试玩，页面发布时间2026-01-11 11:02:45；检查时累计8.8万播放，不是1月当天播放量。"},
    {"id": "S5", "url": "https://www.bilibili.com/video/BV1zjPkzjEQb/",
     "finding": "CSTG喵酱寅子录播合集，2026-03-05 13:04:19；检查时44.4万累计播放。"},
    {"id": "S6", "url": "https://www.bilibili.com/video/BV1xKdsBDEF9/",
     "finding": "吟游大司人大司马试玩，2026-04-18 21:22:35；检查时49.7万累计播放。"},
    {"id": "S7", "url": "https://game.sohu.com/a/1018755729_204824",
     "finding": "游戏葡萄制作人采访，2026-05-06 10:24。回溯1月B站试玩、抖音主播、QQ群增长和闲鱼测试码；报道抖音话题播放量破亿。后者未拿到原始平台统计。"},
    {"id": "S8", "url": "https://weixin.sogou.com/weixin?type=2&query=%E7%AB%9E%E6%8B%8D%E4%B9%8B%E7%8E%8B",
     "finding": "已通过此索引打开GameLook公众号原文《国产独游大黑马？〈竞拍之王〉大火！灵感来自三角洲和集装箱盲盒》，页面日期2026-03-10，描述Demo峰值突破23000。媒体转述，不作为自行测得的峰值。"},
    {"id": "S9", "url": "https://tieba.baidu.com/f?kw=%E7%AB%9E%E6%8B%8D%E4%B9%8B%E7%8E%8B",
     "finding": "页面列建吧日期2026.01.13，吧规2026.04.12更新；当前关注4479、贴子11.4W，不是历史当日统计。"},
    {"id": "S10", "url": "https://v.douyin.com/B7CiYXSqz1I/",
     "finding": "从Steam商店出站链接进入认证官方账号竞拍之王；抖音号85721411969；当前显示217作品，可读部分作品标签。不能由此倒推4月每日传播量。"},
    {"id": "S11", "url": "https://www.acfun.cn/search?keyword=%E7%AB%9E%E6%8B%8D%E4%B9%8B%E7%8E%8B",
     "finding": "站内搜索返回2026-04-24《板面！竞拍之秋》、04-26《【拍商SALA】HJM秋图秀vol.9》、05-09《竞拍之王一天小出三黑盒》。日期为搜索页面所列，个别作品尚未逐一观看核验。"},
    {"id": "S12", "url": "https://www.taptap.cn/app/785213?os=steam",
     "finding": "官方入驻竞拍之王，开发灵猪科技、厂商深圳小贝，Steam资料区。页面还标有26年Q3，不能覆盖Steam正式发行日。"},
    {"id": "S13", "url": "https://so.toutiao.com/search?keyword=%E7%AB%9E%E6%8B%8D%E4%B9%8B%E7%8E%8B",
     "finding": "检出抖音Steam夏促视频、TapTap、搜狐与澎湃报道，以及向僵尸开炮同名活动。跨站索引不等于头条原生内容。"},
]


def export_local():
    conn = sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    # A single read transaction keeps all exported tables on the same snapshot.
    conn.execute("BEGIN")
    rows = [dict(r) for r in conn.execute(
        "SELECT ts,source,entity,metric,value FROM metrics "
        "WHERE entity=? AND source IN ('huya','douyu','bili') ORDER BY ts",
        ("竞拍之王",))]
    entities = [dict(r) for r in conn.execute(
        "SELECT source,entity,name,first_seen,last_seen FROM entities WHERE name=?",
        ("竞拍之王",))]
    hist = [dict(r) for r in conn.execute(
        "SELECT appid,name,status,why FROM hist_steam WHERE appid=4128580")]
    videos = [dict(r) for r in conn.execute(
        "SELECT bvid,title,published FROM video_records WHERE title LIKE ?",
        ("%竞拍之王%",))]
    conn.close()
    for row in rows:
        row["sampled_at_cst"] = datetime.fromtimestamp(row["ts"], CST).isoformat()
    with (OUT / "radar_samples.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "ts", "source", "entity", "metric", "value", "sampled_at_cst"])
        writer.writeheader()
        writer.writerows(rows)
    evidence = {"checked_date": "2026-10-20", "timezone": "Asia/Shanghai",
                "sources": SOURCES, "entities": entities, "steam_history": hist,
                "videos_matching_title": videos, "sample_count": len(rows),
                "matrix": ROWS, "limitations": [
                    "不是全网穷尽；未访问私群或绕过登录。",
                    "Steam峰值是全球同时在线人数，不是中国区用户、销量或收入。",
                    "历史发布存在不等于当日热度高；累计播放量不能倒填历史。",
                    "同名活动、手机版、小说和同玩法作品必须分别核对。",
                    "本地记录按游戏名聚合直播分类，不证明逐房间游戏归属。",
                    "AcFun证据为站内结果，尚未对每个作品观看确认。"]}
    (OUT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def render():
    image = Image.new("RGB", (2000, 2250), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    font_path = "C:/Windows/Fonts/msyh.ttc"
    bold_path = "C:/Windows/Fonts/msyhbd.ttc"
    fonts = {n: ImageFont.truetype(font_path, n) for n in (22, 24, 26, 28, 30, 34, 40)}
    bold = {n: ImageFont.truetype(bold_path, n) for n in (30, 40, 56, 62)}
    ink, muted, green, amber = "#182322", "#596562", "#147D64", "#966310"

    def text(x, y, value, size=28, fill=ink, weight=False):
        f = bold[size] if weight else fonts[size]
        draw.text((x, y), value, font=f, fill=fill)

    text(70, 45, "竞拍之王  /  中国平台传播证据", 56, weight=True)
    text(72, 122, "BidKing · Steam 4128580  |  四人竞拍 / 信息博弈 / 藏品开箱", 28, muted)
    text(72, 166, "核查：2026-10-20    图中记录“何时已有证据”，不冒充各平台历史热度排名。", 26, muted)
    draw.line((70, 220, 1930, 220), fill="#DDE4E1", width=2)
    for x, big, label in [
        (72, "01月11日", "B站早期试玩原视频  S4"),
        (700, "04月16日", "Steam正式发行  S1"),
        (1330, "57,450", "全球Steam峰值 / 04-25 UTC  S2"),
    ]:
        text(x, 250, big, 62, weight=True)
        text(x, 340, label, 26, muted)

    text(72, 410, "全球Steam月度最高同时在线人数（SteamDB）", 30, weight=True)
    months = [("4月", 57450), ("5月", 47037), ("6月", 26502),
              ("7月", 19321), ("8月", 14010), ("9月", 9737)]
    for i, (month, value) in enumerate(months):
        x = 340 + i * 265
        height = round(value / 57450 * 140)
        draw.rectangle((x, 635 - height, x + 145, 635),
                       fill="#147D64" if i == 0 else "#88BBB0")
        text(x - 5, 640 - height - 37, f"{value:,}", 26)
        text(x + 45, 647, month, 24, muted)
    text(72, 694, "交叉核对：SteamCharts 4月峰值为57,125；两源有差异，未取平均。中国区峰值尚未取得。", 24, muted)

    text(72, 765, "平台 × 历史阶段", 40, weight=True)
    for x, color, label in [(680, green, "原页面 / 本地采样"),
                            (1120, amber, "转述 / 索引 / 外链"),
                            (1580, muted, "缺历史 ≠ 没出现")]:
        draw.rectangle((x, 785, x + 18, 803), fill=color)
        text(x + 28, 775, label, 24, color)
    left, top, row_h = 70, 845, 67
    xs = [70, 410, 665, 920, 1175, 1430, 1705, 1930]
    labels = ["平台", "1月 / 早期", "3月 / Demo", "4月 / 爆发", "5—6月", "10月 / 核查", "证据"]
    draw.rectangle((70, top, 1930, top + 58), fill="#EDF2F0")
    for i, label in enumerate(labels):
        text(xs[i] + 12, top + 12, label, 26)
    for ri, (platform, cells, source) in enumerate(ROWS):
        y = top + 58 + ri * row_h
        if ri % 2 == 0:
            draw.rectangle((left, y, 1930, y + row_h), fill="#F8FAF9")
        text(82, y + 18, platform, 24)
        for ci, cell in enumerate(cells):
            x = xs[ci + 1]
            if not cell:
                text(x + 16, y + 19, "—", 24, "#ABB5B0")
                continue
            kind, content = cell.split(":", 1)
            color = {"D": green, "R": amber, "U": muted}[kind]
            draw.rectangle((x + 12, y + 28, x + 21, y + 37), fill=color)
            text(x + 30, y + 18, content, 22, color)
        # The complete source description lives in evidence.json and report.md.
        text(xs[6] + 12, y + 20, source.split("；")[0], 22, muted)
        draw.line((70, y + row_h, 1930, y + row_h), fill="#E6EBE8")
    bottom = top + 58 + len(ROWS) * row_h
    text(72, bottom + 26, "* 抖音“话题破亿”来自5月6日游戏葡萄采访；没有取得原始话题曲线，不能定为4月峰值。", 24, amber)
    text(72, bottom + 70, "雷达缺口：直播仅有10月采样；Steam原作因MMO标签跳过；本地视频标题没有命中。", 26)
    text(72, bottom + 116, "同名污染：10月《向僵尸开炮》“竞拍之王”活动、手游下载页、小说不能并入原作。", 24, muted)
    text(72, bottom + 158, "来源编号与访问限制见同目录 report.md / evidence.json；这是个案图，不能推导整个类型的平台胜负。", 24, muted)
    image.save(OUT / "bidking_china_evidence.png")
    preview = image.copy()
    preview.thumbnail((1100, 1400))
    preview.save(OUT / "preview.png")


if __name__ == "__main__":
    result = export_local()
    render()
    print(json.dumps({"sample_count": result["sample_count"],
                      "image": str(OUT / "bidking_china_evidence.png")},
                     ensure_ascii=False))

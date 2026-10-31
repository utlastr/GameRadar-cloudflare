"""雷达默认设置。

注意：自动更新会覆盖这个文件。常用设置（代理、备份位置、端口、提醒数量等）
请改 radar_data/settings.json，改完右键托盘图标退出再重新打开生效。
"""
import os

# ================= 文件位置 =================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "radar_data")
DB_PATH = os.path.join(DATA_DIR, "radar.db")
RAW_DIR = os.path.join(DATA_DIR, "raw")          # 每次抓取的精简原始数据，按天压缩存档
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# 每天自动把数据库复制一份到这个文件夹。
# 默认放在 radar_data/backup 里（防止数据库文件损坏）；
# 更安全的做法是改成另一块硬盘、U 盘或网盘同步文件夹，例如 r"D:\radar_backup"（防止硬盘坏掉）。
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
BACKUP_KEEP = 14                                  # 备份保留份数

# ================= 网络 =================
# 国内访问 Steam、苹果、Roblox 不稳定时填代理，例如 "http://127.0.0.1:7890"；不用就留空
PROXY = ""
STEAM_CC = "us"

# ================= 运行频率 =================
FAST_MINUTES = 30        # 直播平台（虎牙、斗鱼）
SLOW_MINUTES = 60        # Steam、Roblox、TapTap
SUPPLY_HOURS = 12        # 同一个游戏在 App Store 的供给检查多久重查一次
RAW_KEEP_DAYS = 30       # 30 天后的普通原始数据删除

# ================= 看板 =================
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
OPEN_BROWSER = True

# ================= 数据源 =================
# 想关掉某个平台：settings.json 里写 "SOURCES_ENABLED": {"douyu": false}（其余平台不写就是开着）
STEAM_ONLY = True
SOURCES_ENABLED = {"steam": True, "roblox": True, "taptap": True, "tapmaker": True, "huya": True, "douyu": True,
                   # v30 新接的平台（来源名不能带下划线）
                   "haoyou": True, "mistore": True, "appcn": True, "appus": True, "itch": True, "poki": True,
                   "crazygames": True, "fortnite": True, "kick": True, "twitch": True, "bili": True, "bvideo": True,
                   # v48：微信 / 抖音小游戏榜（数据来自引力引擎小游戏排行榜，见 sources/gravity.py）
                   "wxmini": True, "dymini": True}

# Twitch 要你自己免费申请的密钥（步骤见 sources/twitch.py 或运行情况页"没接上的平台"），不填就显示失败原因
TWITCH_CLIENT_ID = ""
TWITCH_CLIENT_SECRET = ""

# 只有名次的榜单（好游快爆、小米商店、App Store、itch.io、Poki、CrazyGames、Fortnite）怎么算热度：
#   new   = 新品榜 / 预约榜 / 24 小时热门：上了榜就按名次给热度（越靠前越热）
#   trend = 热门榜：长期霸榜的老游戏不算；新上榜、或者名次一天里往前冲了才给热度
# 名次越小越靠前；都不进分数里的"能不能跟"，只进热度
RANK_BOARDS = {
    ("haoyou", "rank_new"): ("new", "好游快爆新品榜"),
    ("haoyou", "rank_week"): ("trend", "好游快爆本周热门"),
    ("haoyou", "rank_hot"): ("trend", "好游快爆热门榜"),
    ("haoyou", "rank_expect"): ("trend", "好游快爆预约榜"),
    ("haoyou", "rank_diy"): ("trend", "好游快爆玩家自制榜"),
    ("mistore", "rank_game"): ("trend", "小米商店游戏榜"),
    ("appcn", "rank_free"): ("trend", "App Store 中国免费游戏榜"),
    ("appcn", "rank_grossing"): ("trend", "App Store 中国畅销游戏榜"),
    ("appcn", "rank_newfree"): ("new", "App Store 中国新游榜"),
    ("appus", "rank_free"): ("trend", "App Store 美国免费游戏榜"),
    ("appus", "rank_grossing"): ("trend", "App Store 美国畅销游戏榜"),
    ("appus", "rank_newfree"): ("new", "App Store 美国新游榜"),
    ("itch", "rank_all"): ("new", "itch.io 新而热门"),
    ("itch", "rank_web"): ("new", "itch.io 网页游戏新而热门"),
    ("poki", "rank_popular"): ("trend", "Poki 本周热门"),
    ("crazygames", "rank_hot"): ("new", "CrazyGames 24 小时热门"),
    # v48 微信 / 抖音小游戏（引力引擎），各 100 名，按天出榜
    ("wxmini", "rank_popular"): ("trend", "微信小游戏人气榜"),
    ("wxmini", "rank_played"): ("trend", "微信小游戏最多人玩榜"),
    ("wxmini", "rank_grossing"): ("trend", "微信小游戏畅销榜"),
    ("dymini", "rank_popular"): ("trend", "抖音小游戏人气榜"),
    ("dymini", "rank_new"): ("new", "抖音小游戏新游榜"),
    ("dymini", "rank_grossing"): ("trend", "抖音小游戏畅销榜"),
}
for _g, _n in {"survival": "生存", "strategy": "策略", "sportsracing": "体育竞速", "simulationtycoon": "模拟经营",
               "shooter": "射击", "roleplayingsocial": "角色扮演社交", "roguelike": "肉鸽",
               "partyminigames": "派对小游戏", "musicrhythm": "音乐节奏", "horror": "恐怖",
               "deathrunplatformer": "跑酷闯关", "battleroyale": "大逃杀", "adventurerpg": "冒险RPG"}.items():
    RANK_BOARDS[("fortnite", "rank_" + _g)] = ("trend", f"Fortnite {_n}榜")
RANK_NEW_TOP = 0.9           # new 榜第 1 名给多少热度（往后按名次递减，大约前 25 名能进必看）
RANK_RISE_MIN = 3            # trend 榜一天里至少往前冲几名才算
# 这些平台的游戏不在 App Store / Steam 上，不去那里搜上线时间（会搜错）
NO_LAUNCH_LOOKUP = {"roblox", "fortnite", "itch", "poki", "crazygames", "wxmini", "dymini"}

# 没接上的平台：原因、你要提供什么。运行情况页会列出来
PLATFORM_TODO = [
    ("快手 / QQ / 支付宝 / 百度 / B站小游戏", "这几家没有公开榜单。引力引擎的接口只给微信、抖音两个平台（助手实测，别的平台名接口直接拒绝）", "接不了。如果你用的别的数据平台有这些榜，告诉助手是哪个网站"),
    ("快游戏（华为、小米、OPPO、vivo 快应用）", "四家都只在手机里的快应用中心有榜，没有网页版；引力引擎也没有", "接不了（只能手机抓包，有封号风险，不建议）"),
    ("华为 / OPPO / vivo 应用商店", "网页版没有游戏排行（只有手机 App 里有），接口要签名", "接不了。小米商店有网页排行，已经接上"),
    ("Google Play", "Google 2022 年起网页版不再显示排行榜，官方没有公开接口", "只能用付费数据（如 AppMagic、Sensor Tower），有账号的话把导出方式告诉我"),
    ("WeGame", "网页版没有热度或在线人数，只有商城页", "接不了"),
    ("Epic 游戏商城", "Epic 商城不公开在线人数和排行（只有免费游戏和促销列表）", "接不了。Fortnite 创意地图走官方数据接口，已经接上"),
    ("Telegram / Discord / YouTube Playables 小游戏", "三家都没有公开排行榜", "接不了"),
    ("抖音直播 / 快手直播", "接口要签名（a_bogus 等）和登录 cookie，签名算法经常换", "你登录网页版直播后，F12 复制分区列表那条请求的 cURL 发给我；签名过期后要重新复制"),
    ("YouTube 直播 / YouTube Shorts", "要 Google 的 API Key，每天免费额度只够查几百次", "去 console.cloud.google.com 开 YouTube Data API v3，建一个 API Key 发给我（不是密码）"),
    ("抖音 / 快手 / 小红书 / TikTok 短视频", "接口要签名和登录，不登录看不到热榜里的游戏数据", "接不了（抓包有封号风险）。B站视频已经接上"),
    ("Twitch", "要你自己免费申请的开发者密钥", "dev.twitch.tv/console 注册应用，把 Client ID 和 Client Secret 填进 settings.json 的 TWITCH_CLIENT_ID、TWITCH_CLIENT_SECRET"),
]

# 虎牙、斗鱼（v21 起）：不再从标题括号里拆词，改看两样
# 1. 分区：开播时就很大的老分区（英雄联盟、和平精英……）不显示；新开的分区、小分区主播数涨了才显示
# 2. 大主播：最近 30 天里进过本平台游戏区热度前 STAR_TOP 的主播，他们现在在播什么
STAR_TOP = 150
STAR_KEEP_DAYS = 30
STAR_HOT = 0.6             # 有大主播在播的游戏，国内热度至少算多少（保证能看到）

# ================= 上线时间权重（v23） =================
# 任何游戏都先查上线时间，再按上线多久打折。超过最后一档 = 0 分，放进"必看 → 老游戏"分栏（还能看到）
AGE_WEIGHTS = [(30, 1.0), (90, 0.7), (180, 0.4), (365, 0.15)]
UNKNOWN_AGE_WEIGHT = 1.0   # 查不到上线时间：不扣分（缺数据不扣分），卡片标"没查到上线时间"
BIG_FACTOR = 0.4           # 大厂（宫崎英高这种）的新游戏：降权，不排除
LAUNCH_LOOKUP_PER_RUN = 20 # 每次分析最多联网查几个上线时间（App Store 每个要等 3.5 秒）
NEW_CAT_DAYS = 14          # 程序开始运行一天以后才出现的分区，多少天内算"新开的分区"
NEW_CAT_MIN_ROOMS = 3      # 新分区至少几个主播在播才算
LIVE_SOURCES = {"huya", "douyu", "huya_kw", "douyu_kw", "huya_t", "douyu_t",
                "bili", "bili_t", "twitch", "twitch_t", "kick"}
# 直播平台的名字，以及"新开的分区"按哪个数算、至少多少（Kick 只给观看人数，没有主播数）
LIVE_PLATFORMS = {"huya": ("虎牙", "rooms", 3), "douyu": ("斗鱼", "rooms", 3), "bili": ("B站直播", "rooms", 3),
                  "twitch": ("Twitch", "rooms", 3), "kick": ("Kick", "viewers", 300)}
BILI_MAX_PAGES = 25       # B站每个大分区（网游、手游、单机）最多翻几页，每页 30 个直播间
TWITCH_MAX_PAGES = 20     # Twitch 每页 100 个直播

HUYA_MAX_PAGES = 80       # 虎牙每页 120 个直播间
DOUYU_MAX_PAGES = 60
# TapTap：做法照参考网站 as167888 的爬虫（只看网页，不用接口），见 sources/taptap.py
# TapTap 榜单：热门/热玩/新品/独家/各分类榜网页 + 逐个打开详情，一轮 40 分钟以上，所以几小时抓一次
TAPTAP_BOARD_HOURS = 6
# TapTap 制造：每小时翻标签页 1–50 页（和参考网站一样），标签页上的游戏每小时都打开详情
TAPTAP_TAG_HTML_PAGES = 50
TAPMAKER_MIN_STORE = 100        # 热度到这个数才开始记每小时数据；不到的只记名字（供给检查用）
TAPMAKER_REFRESH_HOURS = 20     # v74 起不再使用（改由抓取调度 radar/revisit.py 决定），留着免得旧设置报错
TAPMAKER_DETAIL_PER_RUN = 250   # 老游戏每轮最多补抓多少个
TAPTAP_WATCH_IDS = []
TAPTAP_PRIORITY_MINUTES = 15
TAPTAP_PRIORITY_PER_RUN = 12

MAX_STEAM_DETAILS_PER_RUN = 80   # 详情每款需多次请求；按轮次补齐，避免拖住整轮采集
STEAM_REVIEWS_TOP_N = 200        # 热门池每天检查评论数（含中文评论数）
STEAM_REVIEW_MIN = 20            # 评论低于此值的游戏不进入档案和探测池
STEAM_HOT_POOL = 200             # 每日在线人数探测池
STEAM_NEW_TOP = 20               # 第一次进入在线人数前 20 才算新游戏
STEAM_DISCOVERY_DAYS = 30
STEAM_DISCOVERY_PER_RUN = 20000
STEAM_DISCOVERY_PAGES = 3
STEAM_DISCOVERY_METRICS_PER_RUN = 200
BVIDEO_MINUTES = 30
BVIDEO_SEARCH_TERMS = []
BVIDEO_WATCH_IDS = []
BVIDEO_TRACK_PER_RUN = 30
BVIDEO_DETAIL_PER_RUN = 12
BVIDEO_EARLY_VIEWS = 50000
BVIDEO_FAST_VIEWS_PER_HOUR = 10000
BVIDEO_STREAM_ENABLED = True
BVIDEO_STREAM_SECONDS = 60
BVIDEO_STREAM_PAGE_SIZE = 100
BVIDEO_LATEST_SECONDS = 10
BVIDEO_TAG_WORKERS = 2
BVIDEO_BACKLOG_HIGH_WATER = 20000
BVIDEO_BACKLOG_LOW_WATER = 10000
BVIDEO_DAILY_ENABLED = False
BVIDEO_DAILY_HOUR = 0
BVIDEO_DAILY_MINUTE = 5
BVIDEO_DAILY_TIDS = [17, 65, 171, 172, 173, 121, 136, 19]
BVIDEO_DAILY_PAGE_SIZE = 100
BVIDEO_DAILY_REQUESTS_PER_RUN = 200
BVIDEO_DAILY_SECONDS_PER_RUN = 300
BVIDEO_DAILY_RETRY_SECONDS = 3600
BVIDEO_CANDIDATE_MIN_VIDEOS = 3
BVIDEO_CANDIDATE_MIN_AUTHORS = 2
BVIDEO_TITLE_KEEP_DAYS = 90
BVIDEO_NAME_ALIASES = {}
BVIDEO_WORD_BLACKLIST = []
BVIDEO_PERSON_NAMES = []
BVIDEO_GROWTH_ENABLED = True
BVIDEO_GROWTH_CANDIDATES = 300
BVIDEO_GROWTH_SMOOTHING = 100.0
BVIDEO_GROWTH_SAMPLE_HOURS = 6
BVIDEO_GROWTH_REQUEST_SECONDS = 10
BVIDEO_GROWTH_MAX_GAP_HOURS = 30
BVIDEO_GROWTH_TRACK_DAYS = 30

# 直播平台里不是游戏的分区（不参与游戏判断）
NON_GAME_CATEGORIES = {
    "星秀", "户外", "交友", "一起看", "颜值", "体育", "娱乐天地", "二次元", "美食", "音乐",
    "虎牙文化", "科技", "趣分享", "放映厅", "原创", "聊天", "虎牙官方", "鱼教", "汽车", "娱乐",
    "正能量", "文化", "舞蹈", "明星", "鱼乐", "电台", "情感", "旅游", "生活", "知识", "综合",
    "网游竞技", "赛事", "数码科技", "鱼吧", "斗鱼官方", "颜值主播", "派对", "一起玩",
    # B站
    "虚拟主播", "视频唱见", "视频聊天", "生活娱乐", "电台", "搞笑", "知识", "赛事", "游戏赛事",
    # Twitch / Kick
    "Just Chatting", "IRL", "Music", "Art", "ASMR", "Talk Shows & Podcasts", "Sports", "Special Events",
    "Pools, Hot Tubs, and Beaches", "Travel & Outdoors", "Food & Drink", "Science & Technology",
    "Software and Game Development", "Makers & Crafting", "Beauty & Body Art", "Animals, Aquariums, and Zoos",
    "Slots", "Virtual Casino", "Casino", "Gambling", "Politics", "Fitness & Health", "Crypto", "Poker",
    "Watch Party", "Co-working & Studying", "Dance",
}
# 大杂烩分区：里面什么游戏都有，本身不当成一个游戏，靠直播标题里的关键词发现具体游戏
GENERIC_CATEGORIES = {
    "主机游戏", "主机其他游戏", "热门游戏", "单机热游", "其他游戏", "手游休闲", "新游中心",
    "其他手游", "休闲益智", "独立游戏", "单机游戏", "怀旧游戏", "全部", "互动点播", "单机综合", "小游戏",
    # 按类型分的分区，也不是一个游戏
    "自走棋", "军事游戏", "二次元游戏", "恐怖游戏", "完美端游系列", "棋牌游戏", "棋牌娱乐", "射击游戏",
    "策略游戏", "体育游戏", "格斗游戏", "赛车游戏", "音乐游戏", "模拟经营", "角色扮演", "动作游戏", "网页游戏",
    # B站
    "其他单机", "其他网游", "其他手游", "单机综合", "网游综合", "手游综合", "主机综合", "游戏综合",
    # Twitch / Kick
    "Games + Demos", "Retro", "Indie Games", "Horror Games", "Variety",
}

# ================= 关键词发现 =================
# 只从"大杂烩分区"（主机游戏、互动点播、单机热游等）的直播标题里找游戏名。
# 王者荣耀、英雄联盟这类单个游戏分区里，标题括号里写的都是主播标签（有车位、摄像头、黑色玫瑰），不是游戏名；
# 星秀、交友这类非游戏分区更不用看。
KW_MIN_ROOMS = 2
KW_NICK_SHARE = 0.5        # 一半以上的直播间主播昵称里含这个词，说明是主播名或团队名，不算游戏
# 省份和常见城市：主播爱写在标题里，不是游戏
KW_PLACES = {
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
    "台湾", "内蒙古", "内蒙", "广西", "西藏", "宁夏", "新疆", "香港", "澳门", "东北", "成都", "南京", "杭州",
    "武汉", "西安", "长沙", "深圳", "广州", "苏州", "郑州", "青岛", "沈阳", "大连", "厦门", "济南", "合肥",
    "福州", "昆明", "哈尔滨", "长春", "石家庄", "南昌", "南宁", "贵阳", "太原", "无锡", "宁波", "东莞", "佛山",
    "温州", "徐州", "潍坊", "烟台", "洛阳", "唐山", "保定", "邯郸", "兰州", "乌鲁木齐", "呼和浩特", "银川",
}
# 标题里括号内容含这些字的不算游戏名
KW_STOP_SUBSTRINGS = [
    "直播", "主播", "粉丝", "抽奖", "福利", "上分", "带粉", "陪玩", "双倍", "国庆", "中秋", "春节",
    "快乐", "生日", "周年", "比赛", "赛事", "官方", "回放", "重播", "连麦", "pk", "新人", "首播",
    "冲榜", "教学", "车队", "开黑", "上车", "秒跟", "上号", "蓝光", "高清", "画质", "娱乐", "聊天",
    "唱歌", "舞蹈", "颜值", "美女", "帅哥", "日常", "通宵", "全天", "晚上", "下午", "早上", "冠军",
    "亚军", "第一", "国服", "榜一", "精选", "推荐", "互动", "点歌", "礼物", "红包",
    "实况", "预告", "有位", "车位", "点唱", "合集", "集锦", "精彩", "视频厅", "自由厅", "接待", "招人",
    "主持", "解说", "攻略", "速通", "通关", "全收集", "视角", "新手", "教程", "挑战", "试玩", "测评",
    "纪录", "摄像头", "露脸", "真人", "语音", "开麦", "陪伴", "守护", "上岸", "下车", "免费", "包赢",
    "带飞", "代练", "排位", "上星", "冲分", "lol", "王者", "吃鸡", "和平", "回归", "复播", "停播",
]

# 比赛相关、固定大游戏的名字和简称（标题里出现这些的不算新游戏）
KW_STOP_SUBSTRINGS += [
    "联赛", "杯赛", "赛季", "决赛", "预选", "淘汰赛", "表演赛", "邀请赛", "锦标赛", "挑战赛", "职业赛", "季后赛",
    "选拔", "海选", "周赛", "月赛", "战队", "春季", "夏季", "秋季", "冬季", "总决", "pcl", "kpl", "lpl", "pel",
]
# 固定大游戏的名字和简称：直播标题里找游戏名时，名字里带这些的不找
KW_BIG_GAMES = [
    "英雄联盟", "云顶", "金铲铲", "王者荣耀", "和平精英", "绝地求生", "pubg", "无畏契约", "valorant", "瓦罗兰特",
    "cs2", "csgo", "反恐精英", "dota", "刀塔", "原神", "崩坏", "星穹", "绝区零", "鸣潮", "永劫", "三角洲", "穿越火线",
    "dnf", "地下城", "魔兽", "炉石", "守望先锋", "我的世界", "minecraft", "蛋仔", "第五人格", "光遇", "火影",
    "逆水寒", "梦幻西游", "apex", "堡垒之夜", "fortnite", "暗区", "无限暖暖", "燕云",
]
KW_STOP_SUBSTRINGS += KW_BIG_GAMES

# ================= 老游戏（不进机会、观察） =================
# 程序刚开始运行那一天就已经很大的，算存量老游戏
OLD_FIRST_LEVEL = {"huya": ("rooms", 15, "开始运行时就有 {v} 个主播在播"),
                   "douyu": ("rooms", 15, "开始运行时就有 {v} 个主播在播"),
                   "roblox": ("players", 20000, "开始运行时就有 {v} 人在线"),
                   "steam": ("players", 5000, "开始运行时就有 {v} 人在线"),
                   "bili": ("rooms", 15, "开始运行时就有 {v} 个主播在播"),
                   "twitch": ("rooms", 30, "开始运行时就有 {v} 个主播在播"),
                   "kick": ("viewers", 2000, "开始运行时就有 {v} 人在看"),
                   "fortnite": ("players", 20000, "开始运行时就有 {v} 人在线")}
OLD_STEAM_DAYS = 180       # Steam 上线超过几天算老游戏
OLD_TAPTAP_DAYS = 120      # TapTap 上线超过几天算老游戏

# ================= 市场 =================
MARKETS = {"cn": "中国", "global": "海外/全球"}
SOURCE_MARKET = {
    "steam": "global", "roblox": "global",
    "taptap": "cn", "tapmaker": "cn", "huya": "cn", "douyu": "cn", "huya_kw": "cn", "douyu_kw": "cn", "huya_t": "cn", "douyu_t": "cn",
    "haoyou": "cn", "mistore": "cn", "appcn": "cn", "bili": "cn", "bili_t": "cn", "bvideo": "cn",
    "wxmini": "cn", "dymini": "cn",
    "appus": "global", "itch": "global", "poki": "global", "crazygames": "global", "fortnite": "global",
    "kick": "global", "twitch": "global", "twitch_t": "global",
}
# 每个市场要检查"有没有人跟"的平台
MARKET_TARGETS = {"cn": ["taptap", "appstore_cn"], "global": ["appstore_us"]}
TARGET_NAMES = {"taptap": "TapTap", "appstore_cn": "App Store 中国区", "appstore_us": "App Store 美国"}

# ================= 增长判断 =================
# (来源, 指标): (类型, 最低门槛)
#   level = 当前数量（在线人数、直播热度）；cumulative = 累计数（下载、预约、评论），先换算成增量再比较
ANALYZE = {
    ("steam", "players"): ("level", 500),
    ("steam", "reviews"): ("cumulative", 200),
    ("roblox", "players"): ("level", 1000),
    ("taptap", "downloads"): ("cumulative", 5000),
    ("taptap", "reserves"): ("cumulative", 5000),
    ("taptap", "fans"): ("cumulative", 5000),
    ("tapmaker", "downloads"): ("cumulative", 3000),
    ("tapmaker", "fans"): ("cumulative", 3000),
    ("huya", "heat"): ("level", 50000),
    ("huya", "rooms"): ("level", 10),
    ("douyu", "heat"): ("level", 50000),
    ("douyu", "rooms"): ("level", 10),
    # 大杂烩分区里标题写了某个已发现游戏的主播（大小主播都算）
    ("huya_t", "rooms"): ("level", 3),
    ("huya_t", "heat"): ("level", 20000),
    ("douyu_t", "rooms"): ("level", 3),
    ("douyu_t", "heat"): ("level", 20000),
    # v30
    ("bili", "heat"): ("level", 20000),
    ("bili", "rooms"): ("level", 10),
    ("bili_t", "rooms"): ("level", 3),
    ("twitch", "heat"): ("level", 2000),
    ("twitch", "rooms"): ("level", 10),
    ("twitch_t", "rooms"): ("level", 3),
    ("kick", "viewers"): ("level", 500),
    ("fortnite", "players"): ("level", 500),
    ("bvideo", "videos"): ("level", 2),
}
NEW_DAYS = 7               # 第一次出现不到几天算"新"
NEW_LEVEL_FACTOR = 0.25    # 新出现的，门槛降到原来的多少
Z_MIN_SPREAD = 0.05        # 防止大家都不动时除以 0
Z_START = 1.5              # 异常分从多少开始算
Z_FULL = 6.0               # 异常分到多少算满
Z_FAST = 5.0               # 增长异常阈值；提醒仍受预算和冷却限制
CUSUM_K = 1.0              # 累计异常分：每次比这个多的部分才存进账户
CUSUM_H = 6.0              # 账户超过这个算满

# ================= 打分 =================
OBSERVE_MIN_HOT = 0.25     # 热度分达到多少进"观察"
OPPORTUNITY_MIN = 35       # 最终分达到多少进"机会"
SUPPLY_TOP_N = 15          # 每次最多给多少个游戏做 App Store 供给检查
SUPPLY_MATCH = 0.7         # 名字相似度达到多少算同类
STRONG_STRENGTH = {"taptap": 100000, "appstore_cn": 1000, "appstore_us": 1000}  # 跟风品数据"很好"的门槛（下载数 / 评价数）
UNCHECKED_WINDOW = 0.5     # 还没检查供给时的窗口分（不知道 ≠ 没人做）
TAPTAP_UNKNOWN_WINDOW = 0.3   # 最近没抓到 TapTap 数据时，TapTap 的窗口分

# 能"发现"游戏的数据源：只有"平台自己的游戏编号"才认游戏身份，所以只有这些商店 / 平台算。
# 直播平台（虎牙、斗鱼、B站直播、Twitch、Kick）记的是"分区名"，不是游戏编号：它们只能说明
# 某个游戏现在有多少人在看，不能凭一个分区名断定"出现了一个新游戏"。所以不算发现源。
DISCOVERY_SOURCES = {"tapmaker", "taptap", "steam", "roblox",
                     "haoyou", "mistore", "appcn", "appus", "itch", "poki", "crazygames", "fortnite",
                     "wxmini", "dymini"}

BIG_COMPANIES = [
    "tencent", "腾讯", "netease", "网易", "mihoyo", "米哈游", "hoyoverse", "electronic arts", "ea",
    "ubisoft", "育碧", "activision", "blizzard", "暴雪", "sony", "playstation", "nintendo", "任天堂",
    "microsoft", "xbox game studios", "bethesda", "take-two", "2k", "rockstar games", "epic games",
    "square enix", "bandai namco", "capcom", "sega", "konami", "krafton", "nexon", "ncsoft",
    "lilith", "莉莉丝", "hypergryph", "鹰角", "perfect world", "完美世界", "kingsoft", "西山居",
    "三七互娱", "37games", "papergames", "叠纸", "bytedance", "字节跳动", "朝夕光年", "nuverse",
    "happy elements", "乐元素", "kuro games", "库洛", "garena", "level infinite", "riot games",
    # v85 补充（必看"大厂游戏：隐藏"用）。中文按包含，英文按整词
    "腾讯游戏", "天美工作室", "光子工作室", "魔方工作室", "北极光工作室", "proxima beta", "lightspeed studios",
    "timi studio", "网易游戏", "网易雷火", "灵游坊", "s-game", "散爆网络", "sunborn", "悠星网络", "yostar",
    "游族网络", "多益网络", "盛趣游戏", "盛大游戏", "世纪华通", "恺英网络", "吉比特", "雷霆游戏", "紫龙游戏",
    "紫龙互娱", "英雄互娱", "巨人网络", "心动网络", "xd entertainment", "xd inc", "farlight", "funplus", "igg",
    "tap4fun", "龙创悦动", "途游", "掌趣科技", "seasun", "搜狐畅游", "游卡网络", "冰川网络", "中手游", "祖龙娱乐",
    "电魂网络", "鹰角网络", "库洛游戏", "莉莉丝游戏",
    "valve", "sony interactive entertainment", "playstation studios", "microsoft studios", "xbox",
    "ea games", "ea sports", "2k games", "warner bros", "wb games", "embracer", "cd projekt", "fromsoftware",
    "koei tecmo", "level-5", "netmarble", "smilegate", "pearl abyss", "supercell", "king com", "playtika",
    "scopely", "zynga", "moon active", "niantic", "bandai", "square enix", "sega", "konami", "capcom",
]
BIG_COMPANIES_EXTRA = []      # 你自己补的大厂名（settings.json 里填，不会被更新覆盖）
BIG_GAME_NAMES = ["影之刃", "洛克王国", "异人之下"]   # 大作名字（名字里带就算），必看默认藏起来；可在 settings.json 里改
BIG_SIZE_GB = 20              # 临时默认值，待校准：电脑游戏安装包到这么大算大作（Steam 最低配置里的"存储空间"）
BIG_SIZE_GB_MOBILE = 4        # 临时默认值，待校准：手机游戏安装包到这么大算大作（平台给了大小才判断）
BIG_PRICE_USD = 40            # 临时默认值，待校准：Steam 售价到这么多美元算大作（3A 一般 $60–70）

# 标签（Steam 英文标签、TapTap 中文标签、Roblox 类型），全部小写
TAGS_PLUS = {
    "casual", "2d", "pixel graphics", "puzzle", "arcade", "idler", "clicker", "incremental",
    "match 3", "physics", "funny", "cute", "cartoon", "minimalist", "short", "roguelite",
    "bullet hell", "party game", "tower defense", "cooking", "relaxing", "cozy", "family friendly",
    "colorful", "hand-drawn", "platformer", "runner", "endless", "simulation", "party & casual",
    "obby & platformer", "休闲", "益智", "放置", "合成", "消除", "像素", "模拟经营", "塔防", "跑酷",
    "肉鸽", "物理", "解压", "经营", "模拟", "休闲益智", "点击", "派对",
}
TAGS_MINUS = {
    "fps", "open world", "mmorpg", "massively multiplayer", "mmo", "souls-like", "realistic",
    "story rich", "narrative", "visual novel", "walking simulator", "rts", "grand strategy", "4x",
    "competitive", "esports", "tactical", "military", "immersive sim", "jrpg", "action rpg",
    "open world survival craft", "mystery", "detective", "choices matter", "multiple endings",
    "开放世界", "arpg", "大世界", "二次元", "角色扮演", "剧情", "竞技", "多人在线", "推理", "侦探",
}
TAGS_HEAVY = {"fps", "open world", "mmorpg", "massively multiplayer", "mmo", "开放世界", "大世界", "多人在线"}

# ================= 排序（v19 起：只有热度决定看不看得到，其他判断只改排序或加标签） =================
NEW_GAME_DAYS = 14          # 上线不到几天算新游戏：按"上线以来平均每天涨多少"和同平台新游戏比
LAUNCH_TRACK_DAYS = 60      # Steam 上线不到几天的游戏，掉出榜单后继续跟踪（含评论数）
OLD_FACTOR = 0.5            # 老游戏排序打几折（不隐藏）
HEAVY_FACTOR = 0.5          # 重度大作排序打几折（不隐藏）
DISMISS_BACK = 1.5          # 你点了"先不看"的游戏，热度涨到当时的几倍重新出现

# ================= AI 读评论（DeepSeek） =================
# 密钥填在 radar_data/settings.json 的 "AI_KEY"。不填就不用 AI，改用标签判断
# 用第三方中转站：settings.json 里再填 "AI_URL"（中转站地址，只填 https://xxx.com/v1 也行）。
# 模型不用填：程序问中转站这把密钥能用哪些模型，自动挑 DeepSeek 普通对话模型（ai._auto_model）。
# 想指定就填 "AI_MODEL"。任何兼容 OpenAI 格式的接口都能用
AI_KEY = ""
AI_URL = "https://api.deepseek.com/chat/completions"
AI_MODEL = "deepseek-flash"
AI_DAILY_LIMIT_YUAN = 3.0   # 每天最多花多少钱，到了当天停用 AI
AI_PER_RUN = 15             # 每次分析最多新读几款
AI_REVIEWS = 50             # 每款读多少条评论
AI_MIN_REVIEWS = 30         # Steam 评论少于这个数先不判断（太少不可靠）
AI_REJUDGE_AT = [300, 1000, 5000]   # 评论数涨过这些数时重新判断一次

# ================= 提醒 =================
NOTIFY_ENABLED = True
NOTIFY_MIN_SCORE = 55
URGENT_SCORE = 75          # 达到这个加声音
NOTIFY_MAX_PER_DAY = 5     # 提醒预算：每天最多弹几次
NOTIFY_COOLDOWN_HOURS = 24 # 同一个游戏多久内不重复弹（分数又涨 15 分以上除外）

# ================= 节假日、平台活动（只用来在看板上标注） =================
HOLIDAYS = [
    ("2026-10-01", "2026-10-08", "cn", "国庆"),
    ("2026-11-26", "2026-11-29", "global", "感恩节 / 黑五"),
    ("2026-12-18", "2027-01-04", "global", "Steam 冬促 / 圣诞（日期待确认）"),
    ("2027-01-15", "2027-02-28", "cn", "寒假"),
    ("2027-02-06", "2027-02-14", "cn", "春节（日期待确认）"),
]

# ================= 历史对照（Steam 近两年小游戏的评论曲线，后台慢慢补，见 history.py） =================
HISTORY_ENABLED = True
HISTORY_DAYS = 730               # 收近多少天上线的游戏
HISTORY_MAX_PRICE = 20           # 原价不超过多少美元（免费的也收）
HISTORY_SUCCESS_REVIEWS = 1000   # "成功" = 上线 90 天内评论数达到这个数（可以在 settings.json 里改）
HISTORY_PAUSE = 1.5              # 每次请求 Steam 之间停几秒（再加 0–1 秒随机），太快会被限流

# ================= 自动更新 =================
AUTO_UPDATE = True
UPDATE_CHECK_HOURS = 6
# 仓库是私有的话，需要一个只读权限的 GitHub token 才能下载更新（填在 radar_data/settings.json 里）
GITHUB_TOKEN = ""

# ================= 个人设置 =================
# 自动更新会覆盖这个文件。要改设置，请改 radar_data/settings.json（第一次运行自动生成），
# 那个文件更新时不会被覆盖，里面的值会替换上面的默认值。
# 路径请用正斜杠，例如 "D:/radar_backup"。
# ================= 机会雷达（第一期：B站）=================
# 数字分两类：
#   工程参数（抓取频率、请求预算）——为了不被封，不是判断标准；
#   待校准默认值——没有历史案例之前的临时值，校准后由案例算出来替换。
OPP_ENABLED = True
BILI_COOKIE = ""                 # 可选，填 SESSDATA，能降低风控概率
BAIDU_COOKIE = ""                # 可选，百度指数要登录
OPP_BILI_BUDGET = 600            # 工程参数：每小时最多多少次 B站请求
OPP_TRACK_MIN_VIEWS = 3000       # 工程参数：发布 6 小时内播放到这个数就进跟踪池
OPP_TRACK_MIN_MULT = 3           # 工程参数：爆款倍数到这个数就进跟踪池
OPP_DEMAND_WORDS = {             # 评论/弹幕里的需求词，可改
    "mobile": "手机|手游|安卓|苹果|ios|移动端|手机版|手机上|能在手机",
    "name": "叫什么|什么游戏|游戏名|哪里下|在哪下|怎么下|多少钱|steam上",
    "want": "想玩|求链接|好想玩|买了|已购|入了",
}
OPP_BIG_PUBLISHERS = []          # 大厂名单，用它判断"大厂发行的"要不要降权
OPP_STAGE_READY_P = 0.6          # 待校准默认值：概率到这个数提醒"准备"
OPP_STAGE_GO_P = 0.8             # 待校准默认值：概率到这个数提醒"开工"
OPP_DECLINE_RATIO = 0.5          # 待校准默认值：新增播放跌到这个比例算"回落"
OPP_MIN_LIVE_CASES = 20          # 实时攒满这么多个有结果的游戏，才把小时级指标加进模型
BILI_TOPIC_MIN_UP = 3            # 临时默认值，待校准：一个名字至少几个不同 UP 主在做，才登记成 B站 话题

# ================= 评分（radar/heat.py）=================
PENALTY_WORDS = {"剧情": ["剧情", "story rich"], "3A": ["3a", "aaa"]}   # 你给的词；减多少从数据算
SCORE_REGION = "cn"              # 你定的：先只管中国。算哪些视频平台看地图里这个地区的视频平台
NEW_MAX_DAYS = 180               # 临时默认值，待校准（沿用 OLD_STEAM_DAYS）：上线不超过几天算新游戏，只有新游戏进必看
BILI_GAME_SEARCH_PER_RUN = 10    # 工程参数：每 10 分钟在 B站 按名字搜几款新游戏（每款一天一次，占 OPP_BILI_BUDGET 的 60/小时）

# ================= 抓取调度（radar/revisit.py）=================
REVISIT_ENABLED = True           # 关掉 = 回到以前：每轮每款都深查
REVISIT_LEARN = 5                # 工程参数：一款游戏前几次深查每轮都查（攒它自己的正常波动）
REVISIT_MAX_HOURS = 168          # 工程参数：平稳的游戏最长多久深查一次（间隔 1、2、4……小时翻倍到这里封顶）
REVISIT_Z = 3.0                  # 统计惯例：比自己过去高出几个稳健标准差算突增

# ================= 游戏档案（一个游戏 = 一份档案）=================
ARCHIVE_BLOCK_MAX = 8            # 工程参数：同一个名字对上超过这么多款，整组不当候选（"麻将""斗地主"这种泛名）
ARCHIVE_U_SAMPLE = 20000         # 工程参数：估"不是同一个时开发商一致 / 日期接近的概率"抽多少对随机配对
ARCHIVE_GOLD = {}                # 标准答案：{"竞拍之王": ["steam:123", "taptap:456", "btopic:竞拍之王"], ...}

# ================= 成功案例（hit）=================
HIT_WINDOW_START_YEAR = 2018     # 历史从这年起二分找起点
HIT_LEAD_MONTHS = 3              # 工程参数：找到第一条视频的月份后，再往前多取几个月
HIT_DISCOVER_RULE = {}           # 临时默认值，待校准，留空用代码里的默认：min_recent 8 / min_speedup 5 /
                                 # min_past 20 / zero_past 5 / zero_min_now 20 / min_title_hit 0.4 / min_title_n 5
HIT_CLOSE_RULE = {}              # 临时默认值，待校准：结案 play_total 0.03 / new_up 0.10 / days 14
HIT_REOPEN_RULE = {}             # 临时默认值，待校准：二次起爆 new_up 0.20 / play_total 0.10 / days 3
HIT_BURST_RULE = {}              # 临时默认值，待校准：起爆 min_n 3 / mult 3 / lookback 30
HIT_MIN_CASE_VIDEOS = 30         # 临时默认值，待校准：少于这么多条视频不做结案判定

USER_SETTINGS = os.path.join(DATA_DIR, "settings.json")
SETTINGS_ERROR = ""      # settings.json 读取失败的原因（格式错误时整个文件不生效）
_EDITABLE = ["PROXY", "BACKUP_DIR", "SERVER_PORT", "GITHUB_TOKEN", "AUTO_UPDATE", "UPDATE_CHECK_HOURS",
             "NOTIFY_ENABLED", "NOTIFY_MAX_PER_DAY", "NOTIFY_MIN_SCORE", "FAST_MINUTES", "SLOW_MINUTES",
             "AI_KEY", "AI_URL", "AI_DAILY_LIMIT_YUAN", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
             "OPP_ENABLED", "BILI_COOKIE", "BAIDU_COOKIE", "OPP_BILI_BUDGET", "OPP_TRACK_MIN_VIEWS",
             "OPP_TRACK_MIN_MULT", "OPP_DEMAND_WORDS", "OPP_BIG_PUBLISHERS", "OPP_STAGE_READY_P",
             "OPP_STAGE_GO_P", "OPP_DECLINE_RATIO", "OPP_MIN_LIVE_CASES", "BILI_TOPIC_MIN_UP",
             "ARCHIVE_BLOCK_MAX", "ARCHIVE_U_SAMPLE", "ARCHIVE_GOLD", "PENALTY_WORDS", "SCORE_REGION",
             "NEW_MAX_DAYS", "BILI_GAME_SEARCH_PER_RUN",
             "BIG_COMPANIES_EXTRA", "BIG_GAME_NAMES", "BIG_SIZE_GB", "BIG_SIZE_GB_MOBILE", "BIG_PRICE_USD",
             "REVISIT_ENABLED", "REVISIT_LEARN", "REVISIT_MAX_HOURS", "REVISIT_Z",
             "HIT_WINDOW_START_YEAR", "HIT_LEAD_MONTHS", "HIT_DISCOVER_RULE", "HIT_CLOSE_RULE",
             "HIT_REOPEN_RULE", "HIT_BURST_RULE", "HIT_MIN_CASE_VIDEOS"]


def _load_user_settings():
    import json
    g = globals()
    try:
        with open(USER_SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(USER_SETTINGS, "w", encoding="utf-8") as f:
                json.dump({k: g[k] for k in _EDITABLE}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return
    except ValueError as e:
        g["SETTINGS_ERROR"] = f"settings.json 格式错误，整个文件没有生效（{e}）。常见原因：最后一项后面多了逗号、少了引号、路径用了反斜杠"
        return
    except OSError as e:
        g["SETTINGS_ERROR"] = f"settings.json 打不开：{e}"
        return
    if not isinstance(data, dict):
        g["SETTINGS_ERROR"] = "settings.json 格式错误：最外层应该是 { }"
        return
    for k, v in data.items():
        if k == "SOURCES_ENABLED" and isinstance(v, dict):
            g[k] = {**g[k], **v}          # 只写想改的平台就行
        elif k.isupper() and k in g:
            g[k] = v


def source_on(source):
    """huya_kw、douyu_kw（v20 以前的标题括号词）不再使用。"""
    if source.endswith("_kw"):
        return False
    return SOURCES_ENABLED.get(source.split("_")[0], True)     # huya_t 跟着 huya 走


_load_user_settings()

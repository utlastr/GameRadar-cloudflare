# 收入排名与免费资料记录

更新日期：2026-10-26。所有来源均可免费阅读，没有购买数据。

## 排名口径

主排名 = 每日新增首次用户的估计量 × 每位新增用户在 D90 内的开发者净收入。

这是“每天获得的新用户预计带来的收入价值”，不是当天实际到账金额。
例如日新增 2000 人，D90 净价值 3 元/人，对应每天新增 6000 元收入价值；
这 6000 元可能在之后 90 天陆续实现。要预测日现金流水，还需要留存和收入释放曲线。

开发、买量、达人推广等成本不参与本模型。
热度、趋势、可模仿程度不乘进人民币金额，保留为另外的参考视图。

实现位置：

- `radar/revenue.py`：原始量→每日新增→每用户净值→收入排名。
- `radar/revenue_model.json`：有版本、来源、日期的参数与合同模板。
- 数据库 KV `revenue_model`：覆盖参数，不改原始采样。
- 单个实体 `extra.revenue`：国家构成、品类、变现方式、合同与单游戏参数。

## 1. 原始数据怎么换算

| 平台 | 当前 raw material | 处理 | 当前状态 |
| --- | --- | --- | --- |
| Steam 付费游戏 | 累计评论、美国商店现价 | 评论计数差分→日新增评论，乘销量/评论系数 | 可做情景估算 |
| Steam 免费游戏 | 评论、在线人数 | 付费游戏评论系数不适用；在线是存量 | 缺首次用户和内购价值模型 |
| TapTap / TapTap 制造 | 采集器中的 `downloads`，网页叫热度 | 需要热度与真实新增用户的配对样本 | 未取得校准系数 |
| 抖音小游戏 | 新品榜、热门榜、畅销榜、发行人榜名次 | 每个榜单单独拟合；新品榜保留 51–200 | 未取得同日名次/新增用户配对样本 |
| App Store 中国 / 美国 | 各区榜单名次、评价 | 国家、榜单类型分别拟合，不能除平台总用户数 | 未取得校准样本 |
| 好游快爆 / 小米 | 标为下载的累计计数 | 先确认是否包含更新/重复下载，之后差分 | 尚未验证首次用户口径 |
| CrazyGames / Poki | 榜单、评分、游玩或访问指标 | 场次×去重用户率×新客率 | 缺去重和首次用户比例 |
| Roblox | 累计访问、实时在线 | 访问包含复玩；在线不可直接差分成新用户 | 缺首次用户与可兑换收入 |
| Fortnite 创意地图 | 在线人数、榜单 | 用户存量不能当新增，创作者收入池另建模型 | 未接入 |
| itch.io | 访问、下载、评分或榜单 | 区分免费/付费、独立访问与重复下载 | 未接入 |
| 微信小游戏 | 榜单与公开展示数据 | 需要对应榜单的数量锚点 | 未取得校准样本 |

累计计数使用实际采样间隔做日增量，最近三天按 50%/30%/20% 平滑。
不把首个快照的累计总量当新增，不把过期、计数回退或缺少采样写成 0。
收入不再乘 `(a-b)/(a+b)` 的趋势奖励。

### Steam：已读的研究

1. Gamalytic，2023-05-13，
   [How to accurately estimate Steam game sales](https://gamalytic.com/blog/how-to-accurately-estimate-steam-sales)。
   作者用公开销量样本对比估算方法，评论倍数法采用约 35 的中位数；
   测试排除了销量不足 1000 的游戏，因此不能把测试精度直接用于极早期小样本。
2. Gamalytic，2023-07-23，
   [A deep dive into the Steam review ratio](https://gamalytic.com/blog/a-deep-dive-into-the-steam-review-ratio)。
   文章分析价格、折扣、评价倾向、受众和发布时间带来的倍数差异。

第一版采用 35 作为有来源的默认情景系数，不声称每个游戏都等于 35。
它是累计销量关系，用于日评论增量时还受评论延迟影响。
现有评论接口包含激活码评论；没有擅自把所有评论都视为当日 Steam 商店购买。
免费游戏不套这个付费销售模型。

### TapTap：已读的官方定义与讨论线索

[商店数据指标定义](https://developer.taptap.cn/docs/store/operation/metrics/)
把下载请求、下载完成、更新下载、iOS 唤起 App Store 分开统计。
因此“下载请求”也不等于首次安装；这些后台定义不能证明前台热度等于某一项。

搜索过“TapTap 热度 下载量 换算”“TapTap 热度 下载量 开发者”等，
没有找到可直接使用、带前台热度与后台首次用户对照的公开系数。
搜索摘要和搜索引擎 AI 概述没有作为系数依据。

可继续取得的校准原料：开发者公开复盘中的后台新增/下载完成截图，
同日游戏页面热度快照、上线日期、平台/应用类型。只有下载总量或热度单边数据不能拟合。

新增一条收入侧访谈线索：
[2026-09-17，游戏茶馆采访，钛媒体刊载](https://www.tmtpost.com/8142784.html)，
提到《全村最好的剑》上线第三天单日收益超过 2000 元。
这可用于寻找作者、对应日期的后台数据；文章没有同时点热度与新增用户，
本轮没有把这个单例硬换成“每热度多少元”的系数。

### 抖音：已读的开发者讨论

[Cocos 论坛：请有在字节跳动上架经验的老师指点一下](https://forum.cocos.org/t/topic/113946)
有作者拿自己的后台做对照：后台注册约 3000，页面展示近 1 万“最近在玩”。
同帖还有日活约 2000、按人日广告收入的讨论。
它提供了口径不一致的具体实例，没有提供可拟合新品榜的同时点名次。
人日广告收入也不是每位新用户的 D90 收入，不能直接代替 LTV。

[曾嵘，2025-06-09：关于抄榜立项：抖音小游戏热门榜单深度分析](https://blog.zengrong.net/post/douyin-chart-based-pitching/)
区分热门、新品、收益与发行人榜，不能拿不同榜的第 10 名视为同一数量。
文章用于理解榜单与品类，不提供“新品榜第 200 名 = X 新用户”的系数。

模型支持 `users = anchor_daily_users × (anchor_rank / rank) ** exponent`。
锚点和指数默认留空，必须记录场景/来源；不会偷偷把指数设成 1。
月累计用户除以 30，不能自动变成榜首当天新增锚点。

## 2. 每个新增用户能带来多少收入

[AppsFlyer：App monetization report](https://www.appsflyer.com/resources/reports/app-marketing-monetization-report/)
公开报告提供全球移动应用 D90 每安装用户收入：

| 全球样本品类 | IAP 美元/新增用户 | IAA 美元/新增用户 |
| --- | ---: | ---: |
| Casual | 1.34 | 0.55 |
| Midcore | 2.13 | 0.40 |
| Casino | 2.43 | 0.47 |
| Hypercasual | 未采用 | 0.22 |

这些是全球宽品类参考，不能改名为“中国三消”或“美国 SLG”的实测值。
报告有区域差异描述，但本轮没有取得完整、可复用的单国×细品类参数矩阵。
IAP 和 IAA 的不同样本均值不直接相加为混合游戏 LTV。

基准保存在 `mobile_benchmarks`，不自动套给中国或抖音游戏。
可计算参数表 `profiles` 需要：
`country`、`genre`、`monetization`、`horizon_days`、
`iap_cny`/`iaa_cny`、`basis`、`source`。
`basis=developer_net` 表示已经扣到开发者净额，不再次抽成。
混合模型还需 `joint_sample=true`，说明两项是同一批新增用户。
国家构成必须合计为 1；缺任一国家参数就列出缺项，不按国民收入臆造付费倍数。

Steam 的当前路径只估付费本体销售，不估 DLC/内购：
美国当前售价×全球均价调整系数×汇率，再按结算规则扣减。
全球均价调整系数暂为 1，是情景，不是已测全球平均成交价。

## 3. 分成与实际能结算的部分

- Steam：[销售报表与付款](https://partner.steamgames.com/doc/finance/payments_salesreporting)。
  官方区分销售显示金额与扣退款、拒付、税费后的付款报表。
  第一版小规模付费游戏按开发者 70% 分成情景；未知退款/税费暂设为 0 并展示假设。
- TapTap：[IAA 小游戏](https://developer.taptap.cn/minigameapidoc/quick-start/monetization/IAA-game/)、
  [Dirichlet 介绍](https://developer.taptap.cn/minigameapidoc/tap-operation/operation/monetization-operation/IAA/dirichlet-intro/)。
  文档列出 Dirichlet 零分成、开发者 100% 现金分成，仅作为对应小游戏广告合同模板。
  不代表所有 TapTap 应用或广告主支出都 100% 给开发者。
- 抖音：[广告收入结算规则](https://developer.open-douyin.com/docs/resource/zh-CN/mini-game/guide/business-guide/revenue/sharingclause/mini-game)、
  [结算导览](https://developer.open-douyin.com/docs/resource/zh-CN/mini-game/guide/business-guide/revenue/sharingclause/game-settlement-guide)。
  2026-10-26 实际打开官方正文核验：广告投广现金分成 90%，非投广 60%。
  内购一般版为扣支付渠道费后结算基数的 60%；减免版分场景。
  发票税率和个人/企业主体影响实付，不能只乘一个分成率就声称个人到手。
  投放金/任务金不是可提现现金，不加进现金收入。
- Apple：[Small Business Program](https://developer.apple.com/app-store/small-business-program/)。
  合资格并加入计划后，付费应用/内购佣金 15%；不自动假设每个账号都有资格。
  App Store 内购佣金不重复扣在广告收入上。
- Roblox、Fortnite、Poki、CrazyGames、微信、小米、好游快爆、itch.io：
  尚未完成对应发行身份/合同与当前现金结算条款核验，默认不填统一抽成率。

代码分开处理：消费者退款、价内间接税、支付渠道费、内购/广告平台分成、
广告发票结算调整、预扣与结算扣减。已经是开发者净额的数据不扣第二次。
广告网络已扣除的部分不再按广告主支出重扣。
税费为零的情景不等于税费已验证为零，界面列出该假设。

## 配置示例

以下仅说明字段，不是已经校准的参数。可放到单游戏 `extra.revenue`：

```json
{
  "genre": "puzzle",
  "monetization": "iaa",
  "country_mix": {"CN": 1},
  "contract": "douyin_iaa_organic_enterprise",
  "conversion": {
    "kind": "rank",
    "metric": "rank_new",
    "anchor_rank": 1,
    "anchor_daily_users": null,
    "exponent": null,
    "min_rank": 51,
    "max_rank": 200,
    "source": "待填配对样本或明确的情景说明"
  }
}
```

不要为填满榜单把 `null` 随意改为数字。收入链完整的才参加收入排序；
链条不完整的游戏保留在待估清单，显示缺哪一项。
同 TapTap app ID 去重；跨平台没有独立收入/用户证明时不叠加，
当前每个游戏选一个完整渠道情景作为代表，界面显示渠道，不伪装成全平台总收入。
主排名在旧热度截断前独立计算，低热度高收入游戏不会被旧榜单挡住。

## 校验

`python -X utf8 -m unittest tests.test_revenue tests.test_heat tests.test_discovery tests.test_dymini_charts`

覆盖评论换算、缺失不等于零、免费游戏、榜单窗口、过期名次、税费/抽成、
净额避免二次扣减、国家权重、周期一致、混合样本、平台去重与独立收入排名。

本轮浏览器检查使用现有 CDP 和 Playwright，验证桌面/手机无页面横向溢出、
预估收益默认排序、低热度高收入优先、趋势/热度切换；测试数据只在浏览器内存使用，
没有写入实际采样。截图在 `output/revenue-qa/`。

全量测试另有 19 项错误，涉及现有缺失的 `_parallel`、`UPDATE_SCOPE` 和已删除社交模块；
这些模块未在本轮收入模型中修复。

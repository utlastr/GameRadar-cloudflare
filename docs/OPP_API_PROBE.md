# 接口实测结果（2026-10-20 16:49）

| 接口 | 结果 | 说明 |
|---|---|---|
| 3.1 dynamic/region | FAIL | TypeError: not enough arguments for format string |
| 3.1 newlist(备用) | OK | code=0 条数=0 |
| 3.2 view 详情 | FAIL | IndexError: list index out of range |
| 3.2 tags 标签 | FAIL | IndexError: list index out of range |
| 3.3 relation/stat 粉丝 | OK | follower=20853530 |
| 3.3 space/wbi/arc 投稿 | FAIL | RuntimeError: code=-352 风控校验失败 |
| 3.4 reply/wbi 评论 | OK | code=-400（oid=0 只为验证签名是否被接受） |
| 3.5 B站热搜 | OK | 10 条，首条 美国禁止政府使用DeepSeek |
| 3.5 微博热搜 | FAIL | RuntimeError: HTTP Error 403: Forbidden（weibo.com/ajax/side/hotSearch） |
| 3.5 抖音热榜 | FAIL | RuntimeError: 返回内容是空的 |
| 3.5 百度指数 | OK | 返回 461 字节，开头 {"status":10018,"data":"","logid":2449611818,"message":"\u60a8\u597d\uff0c\u767e |
| 3.7 App Store 中国 | OK | resultCount=5 首个=TapTap - 发现好游戏 |
| 3.6 Steam 中文评论 | OK | total_reviews=220272 |
| 3.6 Steam 在线 | OK | players=42439 |

共 8/14 可用。

## B站连通复测（2026-10-23）

- 同一网络下，`newlist?rid=4` 和 `view` 返回 `code=0`；
  `ranking/v2?rid=4` 返回 `code=-352`。不能据此暂停其他已验证可用的入口。
- `newlist` 可出现 `page.count=0` 但 `archives` 有真实投稿的情况。
  应按 BV 号保存、去重，不能把 `page.count` 当游戏区视频总量。
- 限量真实采集完成：221 个去重 BV 号，其中分区最新 3 页得到 57 个去重 BV 号；
  全站热门以及“铸剑大师”“ForgeTax”的最新/最多播放搜索均返回数据。
  详情请求另外遇到两个 `-404` 和一个 `62012`，错误保留在采集报告中。
- 当前客户端对 `-352` 按入口暂停，对 `-412` 保留全局暂停。
  所有工作线程的客户端请求共用最小 1.5 秒间隔，风控响应不自动重试。
  外层再次处理同一个 `RiskError` 不重复累计风控次数。
- 视频采集先保存分区最新，再补榜单、搜索和详情。搜索被风控时停止该轮后续搜索；
  全局暂停时仅记录一次原因，不再逐个关键词/BV号追加同一条暂停提示。
- `BVIDEO_NEWLIST_PAGES` 默认 3，可在现有 `settings.json` 中配置。
  此入口目前是分页抽样，不是游戏区每日全量；`video_collection.partition.complete=false`。
  按日期统计这些记录只能得到“已观测投稿数”，未验证连续覆盖前不得报告为全区总数。

本次通过真实非空分区列表验证连通后，留存旧暂停时间至
`bili_recovery_probe`，再解除旧版遗留的全局暂停。未删除历史视频和采样。

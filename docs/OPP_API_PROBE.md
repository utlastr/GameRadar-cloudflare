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

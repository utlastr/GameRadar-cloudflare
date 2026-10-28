# B站作品名筛选

## 目标

把具体游戏作品与普通词、题材、类型、平台、内容形式、主播、角色、关卡和活动分开。
词频和UP主数只决定检查优先级，不证明一个词是游戏名。

## 已接通的处理顺序

1. 从完整标题括号和原始Tag生成线索，不从名称中删除大厂名称或普通词。
2. 用规范化完整词查询分类表；只做整词匹配，不做包含匹配。
3. 与已有商店游戏名称匹配；商店记录标注为DLC、Demo、音乐、视频或泛分类时不当游戏。
4. 未确定的去重词，每批最多20个，携带最多3条标题与Tag，优先不同UP主，交给已有AI接口分类。
5. AI分类必须引用实际输入中的证据；低置信度、编造证据、无证据的结果留待确认。
6. 游戏结果进入作品名候选，未确定词保留在待确认队列，明确非作品词退出游戏候选表。
7. 游戏名复用既有视频去重与播放、点赞等统计；AI不会伪造商店ID或自动合并游戏。

分类表：`radar/bili_term_rules.json`。
AI判断和证据：`bd_term_judgments`。
原始视频、Tag、标题、统计：`bd_videos`，此次过滤不删除这些字段。

## 完整词处理示例

| 完整词 | 类别 | 作品榜处理 |
| --- | --- | --- |
| 真的、剧情、手机、精彩、可爱 | 普通词或描述词 | 排除 |
| 三国、二次元、像素风 | 题材或风格 | 排除 |
| MMORPG、FPS、MOBA、RPG | 类型 | 排除 |
| Roblox、Steam、TapTap | 平台 | 排除，但保留在原始Tag中 |
| DLC、MOD、OST、DEMO | 内容形式 | 排除 |
| UP主激励计划、周年庆 | 活动 | 排除 |
| 三国杀、三国志 | 作品名形式 | 不因含“三国”而过滤，继续匹配/判定 |
| 炉石传说、恶龙传说 | 完整作品名形式 | 不因含“传说”而过滤 |
| DLC Quest、手机帝国 | 完整作品名形式 | 不因含DLC或手机而过滤 |
| 一个此前未收录的新名字 | 待确认 | 不因商店库没有而删除 |

“作品名形式”不等于已经核实存在。需要已有作品记录或具体上下文证据。
配置中的已有大游戏排除名单仍独立生效；混合视频中的其他新游戏线索不会一起删除。
Roblox平台标签被排除，不代表排除Roblox上的具体游戏。

## 缓存、成本与回查

- 整词表使用哈希查询，不逐词遍历整个黑名单。
- 同一词和相同上下文不重复调用AI。
- 每天检查上下文是否变化；有新语境时允许普通词重新成为作品名候选。
- 独立后台线程每轮只做一小批，不把AI请求放进B站列表或Tag采集线程。
- 历史标题回扫按批处理，并为每个作品词保存进度；正式流程每次最多处理2000条标题，只回扫作品词。
- 复用现有AI密钥、接口和每日预算。费用按项目已有估算方式记账；接口缺少用量时按每批0.05元预留估算。
- 没有密钥、额度达到上限、接口失败时，未判定词继续待确认，不批量判成“不是游戏”。
- 每日作品表只展示已匹配作品和AI判定的作品名候选；AI候选不冒充已核实商店作品。

## 旧数据修复

旧逻辑把“崩坏星穹铁道”改成“铁道”、把“暗区突围”改成“突围”、把“炉石传说”改成“传说”。
新逻辑保留完整原词。修复脚本仅重建`bd_word_hits`和派生计数，保留全部原始视频。
旧词条本身保留，失去原始证据的残词不再晋级。

```powershell
python tools/filter_bili_terms.py --rebuild --report output/term-filter-migration.json
python tools/filter_bili_terms.py --ai-batches 1 --batch-size 20 --report output/term-filter-ai.json
```

## 成熟方法与本项目采用方式

调查日期：2026-10-26。以下官方文档已实际访问，均返回HTTP 200。

1. **命名实体识别（Named Entity Recognition，NER）和实体类型判定。**
   spaCy文档介绍识别文本中的实体以及按语境标注类型。通用预训练模型的类别不能直接等同于游戏作品；
   主播、角色、活动也可能是专有名词。本项目采用针对游戏领域的明确类别和上下文分类。
   来源：https://spacy.io/usage/linguistic-features#named-entities
2. **实体链接/消歧（Entity Linking）。**
   spaCy的EntityLinker将提及链接到知识库中的唯一实体。本项目只在已有商店记录准确对应时保存原有ID；
   未链接的作品名候选保留，不用“链接失败”判定它不是新游戏。
   来源：https://spacy.io/api/entitylinker
3. **突发词/显著性检测（Burst Detection / Significant Terms）。**
   Elasticsearch官方文档强调比较当前集合与背景集合的相对出现频率，而非只取最常见词，
   并通过采样和去重减少重复文本干扰。这里用于确定后续热点检测方向。
   本次实现的是作品名过滤，不宣称已实现Elasticsearch的显著性聚合或完整突发检测模型。
   来源：https://www.elastic.co/docs/reference/aggregations/search-aggregations-bucket-significanttext-aggregation

补充定义：

- Steamworks官方将DLC定义为可下载附加内容，不能把单独的“DLC”当作品名字。
  https://partner.steamgames.com/doc/store/application/dlc
- Roblox官方文档区分平台、其上的具体游戏以及游戏内部的场景。
  https://create.roblox.com/docs/production/publishing/publish-experiences-and-places

## 测试范围

测试覆盖完整词保护、中英文及全角规范化、大厂残词修复、混合视频保留、原始Tag保留、
未知词隔离、商店名称链接、模型输出验证、缓存、语境变化重判、预算和密钥缺失、异常释放租约。

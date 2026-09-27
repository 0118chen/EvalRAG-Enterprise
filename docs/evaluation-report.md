# 法规问答检索评测报告（2026-09-27）

固定语料、固定题集、六组检索配置的实测结果。所有数字来自本仓库自己的检索与评测链路
（`app/core/retrieval_service.py` + `app/core/evaluation_runner.py`），可由
`scripts/run_golden_experiment.py` 一键复跑。原始输出：`docs/evaluation/golden-set-2026-09-27.json`。

## 0. 一句话结论

**标注可用，指标已饱和。** 52 道题的证据引文 100% 落在标注页码上，标注可信；
但纯 BM25 + 重排已经拿到文档级/页级满分（Recall@1 = 1.000、MRR = 1.000、page_hit = 1.000），
这套题集**没有留下可证明"改进"的空间**——所以本仓库不写"显著提升召回率"这类说法。

真正可复现的结论是两条负面但有用的结果：

1. 本地 hash 向量（32 维）构成的 dense 通道质量远低于 BM25，把它按等权 RRF 融合进去会**拉低**结果；
2. 单次检索 p50 0.69–1.14 s（316 chunk，缓存未命中），瓶颈是每查询重建检索器并把整个语料重新嵌入。

## 1. 数据审计：golden set 是否合格

`python -m scripts.audit_golden_set --json docs/evaluation/golden-set-audit.json`
（原始结果：`docs/evaluation/golden-set-audit.json`）

| 检查项 | 结果 |
|---|---|
| 题量 / 文档数 / 覆盖率 | 52 题，13 份文件，每份文件恰好 4 题，无遗漏文档 |
| 字段完整性 | 无缺失字段，52 个问题互不重复，`source_filename` 全部存在于语料 |
| **证据引文回验**（把引文重新到原文里找，用与摄取相同的解析器） | **52/52 命中**，且都在标注页码上；`answers_with_numbers_absent_from_quote` 为空 |
| 页码合法性 | 无越界页码 |
| 与既有 API 格式的一致性 | `golden_eval_api.json` / `golden_eval_v2_api.json` 与 v1 同题同页同答案 |

题集由 `scripts/generate_golden_set.py` 用 LLM 起草，生成时即强制"引文必须能在所标页面逐字找到"，
本次审计又独立复验了一遍——这是它可以作为标注使用的核心理由。

审计发现的三个局限（不影响可用性，但影响解读）：

| 局限 | 影响 |
|---|---|
| **docx 只有一页**：5 份 docx 解析出的页码恒为 1（`python-docx` 无版面信息） | 这 20 题的 `page_hit` 退化成"文档级召回"，只有 PDF 的 32 题能真正检验页码 |
| **民法典占 52% 的 chunk**（163/316），却只占 4/52 的题 | 任何 chunk 级指标都被这一份超长文档主导 |
| 3 道题（三份《贷款管理办法》的"贷款人"定义）**引文在三份文件里逐字相同** | 文档级标签对这三题不可区分，错文件也算命中 |
| 49/52 的提问直接点名了所属法规 | 词面匹配被极度优待，这是 BM25 接近满分的原因之一 |

## 2. 方法

```bash
# 导入 law/ 下 13 份文件（走生产同一条 worker 函数），重建数据库与题集，跑六组配置
python -m scripts.run_golden_experiment \
    --json docs/evaluation/golden-set-2026-09-27.json \
    --markdown docs/evaluation/golden-set-2026-09-27.md
# 延迟归因（可选）
python -m scripts.profile_retrieval --database data/experiments/golden.db --tenant golden-experiment
# 饱和成因探针（可选）：引文排名分布 + 去掉法规名的对照
python -m scripts.probe_golden_difficulty --database data/experiments/golden.db --tenant golden-experiment
```

- **语料**：13 份法规（8 PDF + 5 DOCX），316 chunk，版本 `latest`，单租户。
- **题集**：52 题，`top_k=5`，每题单文档单页。
- **嵌入**：`EMBEDDING_PROVIDER=hash`（32 维），`DENSE_RETRIEVAL_BACKEND=local`、
  `SPARSE_RETRIEVAL_BACKEND=local`——**没有接外部向量库/ES**，也没有用外部 embedding API。
- **缓存关闭**（`CACHE_ENABLED=false`）：保证每题都真正走一次检索，延迟即"缓存未命中"的延迟。
- **无外部付费调用**：重排是词面 `LexicalReranker`，查询改写是规则表 `RuleBasedQueryRewriter`
  （`app/core/query_rewrite.py`），全程确定性、可重复。
- **指标口径**：`Recall@k`/`MRR`/`nDCG@k`/`precision@k` 为**文档级**（`app/core/evaluation.py`）；
  `page_hit` = top-5 里存在 (期望文档, 期望页码) 的 chunk；
  `passage_hit`/`passage_at_1`/`passage_mrr` = 证据引文是否被检索到、是否排第一、倒数排名（平台内计算，
  需要样例带 `evidence_quote`）；
  另有脚本侧独立实现的 `quote_hit` 作为交叉校验，两者逐位一致。
- **可复核性**：结果 JSON 里每题一行（`per_example`：问题、类别、来源文件、recall@1/@5、page_hit、
  quote_hit、`passage_rank`、延迟、命中 chunk 的文档/页码/分数），所以下表任何汇总数字都能回查到具体是哪几道题。

**随机基线**（同样 top-5、从 316 chunk 里随机抽，按每题相关 chunk 数精确计算，非估计）：

| 指标 | 随机 | BM25 | 
|---|---|---|
| 文档命中 | 0.246 | 0.981（Recall@1） |
| 页命中 | 0.160 | 1.000 |

所以这套题**不是**随机可解的；它的问题是"太容易"，不是"无意义"。

## 3. 结果

| 配置 | 它隔离的变量 | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | **passage@1** | passage_mrr | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | 纯 BM25 基线 | 0.981 | 1.000 | 1.000 | 0.990 | 0.993 | 0.993 | 1.000 | 0.769 | 0.845 | 0.962 | 703 | 898 |
| **sparse-bm25-rerank** | BM25 + 重排 | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | **0.769** | **0.848** | 0.962 | 704 | 879 |
| dense-hash | 只要 dense 通道 | 0.692 | 0.885 | 0.942 | 0.786 | 0.801 | 0.825 | 0.788 | 0.288 | 0.396 | 0.635 | **419** | 508 |
| hybrid-rrf | 两通道 RRF，不重排 | 0.904 | 0.981 | 1.000 | 0.941 | 0.947 | 0.956 | 0.904 | 0.519 | 0.628 | 0.827 | 1123 | 1326 |
| hybrid-rrf-rerank | 再加重排 | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.923 | 0.635 | 0.721 | 0.846 | 1135 | 1349 |
| hybrid-rrf-rerank-rewrite | 再加查询改写（产品默认） | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.942 | 0.615 | 0.715 | 0.865 | 1193 | 2236 |

`passage@1`/`passage_mrr`/`quote_hit` 三列是**文档级指标看不见的部分**：前三列几乎打平时，
"答段是否排在第一"仍有 0.769 : 0.288 的差距。其中 `passage@1`/`passage_mrr` 由评测平台自己计算
（`evidence_quote` 字段 + `app/core/evaluation.py::passage_metrics`），
`quote_hit` 是脚本侧独立实现的引文命中率，两边逐位一致（结果 JSON 的 `passage_cross_check`）。

按文件类型拆开（quote/page 命中数）：

| 配置 | PDF 32 题 quote / page | DOCX 20 题 quote / page |
|---|---|---|
| sparse-bm25-rerank | 31/32 / 32/32 | 19/20 / 20/20 |
| hybrid-rrf-rerank-rewrite（默认） | 28/32 / 29/32 | 17/20 / 20/20 |
| dense-hash | 22/32 / 23/32 | 11/20 / 18/20 |

分类型看更清楚：**产品默认配置在 PDF 上比纯 BM25 少命中 3 题**，在 DOCX 上少命中 2 题。

## 4. 五个发现

### F1 dense 通道本身就是噪声源，hybrid 因此比 BM25 更差

dense-only（R@1 0.692）和 BM25（R@1 0.981）差距巨大，而两者的等权 RRF 融合（R@1 0.904）
落在中间且**低于** BM25。逐题对照更直接，按"quote 与 page 都命中"作为完全正确：

| 对照 | 完全正确的题数 |
|---|---|
| BM25 + 重排 胜过 产品默认（默认丢、sparse 中） | **6** |
| 产品默认 胜过 BM25 + 重排 | **0** |

即 BM25+重排在这套题上**严格优于**当前默认的 hybrid 配置，且没有任何反向案例。被拖累的 6 题：

| 题目 | dense-only | BM25+重排 | 产品默认 |
|---|---|---|---|
| 《流动资金贷款管理办法》贷款期限规定 | 丢 | 中 | 只中页码，丢引文 |
| 商业银行破产清算优先支付 | 丢 | 中 | 只中页码，丢引文 |
| 《流动资金贷款管理办法》贷款人违规经营应被采取什么措施 | 丢 | 中 | 全丢 |
| 《个人贷款管理办法》"银行业金融机构"定义 | 丢 | 中 | 中引文，丢页码 |
| 民法典：监护人的责任 | 丢 | 中 | 只中页码，丢引文 |
| 《农户贷款管理办法》无法收回的贷款如何处理 | 丢 | 中 | 全丢 |

全量引文级漏检：sparse+重排 2/52、产品默认 7/52、dense-only 19/52；
页级漏检：sparse+重排 0/52、产品默认 3/52、dense-only 11/52。

根因：`EMBEDDING_PROVIDER=hash` 是 32 维哈希词袋，只有词面信息且维度过低，
它排出来的名次被 RRF 当成与 BM25 同等可信的一票。**修法不是调 RRF 权重，而是换真正的语义向量**——
在拿到 embedding API key 之前，仓库里不应该声称 hybrid 优于 BM25。

### F2 重排确实有效，但只在 BM25 之上补齐最后一题

BM25 的 R@1 0.981 → 加词面重排后 1.000：唯一被改动的是《农村集体经济组织法》施行日期那题，
期望文档由第 2 位提到第 1 位（该题答段在两版里都排第 4，passage@1 不变）。延迟差异在噪声范围内
（同一脚本四次重跑 p50 波动约 ±5%），所以词面重排的代价可以忽略。
但同一套重排在 hybrid 上只能从 0.904 补到 0.942，补不回 dense 造成的损伤。

### F3 指标饱和，这套题集无法区分配置——两个成因都已量化

BM25+重排在 Recall@1/3/5、MRR、nDCG@3/5、page_hit 上全为 1.000。
继续做检索层"优化"在这个语料上**无法被验证**。饱和不是单一原因，用
`scripts/probe_golden_difficulty.py`（同一脚本可复跑）测出两个来源：

**成因一：文档级指标在 13 份文档上太粗（差异被聚合吃掉）。**
把"证据引文所在 chunk 的排名"单独统计，同一批检索结果里的差距立刻显现
（下表来自 `scripts/probe_golden_difficulty.py`，与平台内 `passage@1` 指标逐位一致）：

| 配置 | 引文排第 1 | 第 2–5 | 不在 top-5 | passage@1 |
|---|---|---|---|---|
| sparse-bm25 | 40 | 10 | 2 | 0.769 |
| sparse-bm25-rerank | 40 | 10 | 2 | 0.769 |
| hybrid-rrf-rerank | 33 | 11 | 8 | 0.635 |
| hybrid-rrf-rerank-rewrite | 32 | 13 | 7 | 0.615 |
| hybrid-rrf | 27 | 16 | 9 | 0.519 |
| dense-hash | 15 | 18 | 19 | 0.288 |

文档级全是 1.000/0.94 的时候，passage@1 的差距是 0.769 : 0.288。
语料只有 13 份文档、每份约 24 chunk（占语料 7.6%），随机抽 5 个 chunk 就有 0.246 的文档命中率，
"命中文档"本身是个廉价事件。另外注意 `sparse-bm25` 与 `sparse-bm25-rerank` 的 passage@1 相同（0.769），
说明重排修的是文档级 rank-1 的那一题，并没有改变"答段是否排在第一"。

**成因二：提问点名法规，而法规全称在原文里逐字出现。**
49/52 的提问含《…》法规全称，等于给 BM25 一条近乎唯一的词面线索。把《…》整段去掉后重跑同一套检索：

| 变体 | R@1 | R@3 | R@5 | MRR | page_hit | quote_hit |
|---|---|---|---|---|---|---|
| 原始提问 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.962 |
| 去掉法规名 | 0.885 | 0.962 | 0.981 | 0.925 | 0.981 | 0.962 |

即 **11.5 个百分点的 Recall@1 来自"问题里报了法规名"**。`quote_hit` 不变说明摘掉标题后 BM25 仍能找回那段话，
掉的是排序；同时重排的增益完全消失（去掉标题后 rerank 开关对结果零影响），
说明它原先补的那道题正是靠标题词面拉平的。

**第三层偏置（本批数据无法证伪，但方向明确）**：题集由 LLM 从原文起草且强制"引文必须在所标页面逐字找到"，
因此问题措辞贴近原文用词，且全部是单文档、单页、单跳、必有答案，没有同义改写、跨文档多跳或应拒答的负样本。
唯一的三道近邻干扰题（三份文件"贷款人"定义逐字相同）恰好是默认配置丢分的地方。

**顺序**：先修指标口径（把 passage 级指标做进产品），再修题集难度——否则改完题集也没有可比的基线。

### F4 单次检索 p50 0.69–1.14 s，全是可消除的重复计算

用配置之间的差值可以把延迟拆开（数据来自上表，属推断而非微基准）：

| 环节 | 实测依据 | 量级 |
|---|---|---|
| 全语料嵌入（316 chunk）+ 余弦 | dense-only = 410 ms（只做这一件事） | ~400 ms |
| BM25 打分（含对全语料重新分词） | sparse 685 ms − dense 410 ms | ~280 ms |
| 两者叠加 | hybrid 1087 ms ≈ 2×410 + 280 | ~1100 ms |

`scripts/profile_retrieval.py` 的 cProfile 佐证：`hybrid` 每查询调用 `embed()` 634 次
（= 316 chunk × 2），`sparse` 也要 317 次；改写触发时（p95 2268 ms）翻到 4 倍。

### F5 三条代码级原因（第 2 步的具体工作项）

1. **`retrieve()` 在 mode 分支之前无条件算两个通道**（`app/core/retrieval.py:88-107`）：
   dense 分数算完、排序完，才在最后判断 `mode == "sparse"` 并把它丢掉。
   所以"只做 BM25"的配置也在为全语料嵌入付费——**这也是 sparse(685 ms) 比 dense-only(410 ms) 更慢的原因**。
2. **检索器每查询重建**：`RetrievalService._build_retriever()` 按查询调用 `create_retriever()`，
   每次都新建 `LocalRetriever` 并对全语料重新分词/嵌入。BM25 应当是建库时算好的倒排索引 + 文档长度统计。
3. **`HybridRetriever` 组合两个 `LocalRetriever`**，而每个 `LocalRetriever` 内部又会走完整的 `retrieve()`，
   于是同一份语料被嵌入两遍、BM25 也被算两遍（`app/core/backends.py:73-89`）。

粗算修完第 1、3 条后的量级：sparse 685 → ~280 ms，hybrid 1087 → ~700 ms。
这是**基于实测分解的推断**，第 2 步落地后必须重新测量，不能直接写进简历。

## 5. 可以写什么、不能写什么

| 不能写 | 原因 |
|---|---|
| "混合检索显著提升召回率" | 实测是下降（0.981 → 0.904/0.942） |
| "Recall@5 达到 1.0，检索效果优秀" | 13 份文档、52 题的饱和指标，换个语料必然回落 |
| "对 1 万/10 万 chunk 做过性能测试" | 只测了 316 chunk 的本地后端 |

| 可以写（有原始数据支撑） | 证据 |
|---|---|
| 自建 52 题页面级评测集，标注可用证据引文逐条回验（52/52） | `docs/evaluation/golden-set-audit.json` |
| 用同一评测链路对比 6 组检索配置，并给出随机基线 | `docs/evaluation/golden-set-2026-09-27.json` |
| 评测平台支持 passage 级指标（证据引文命中/排名），文档级饱和时仍能区分配置 | `passage@1` 0.769 vs 0.288 |
| 定位到"每查询重建检索器/重复嵌入全语料"的性能问题并量化 | `scripts/profile_retrieval.py` 输出 |
| 发现并修掉负向优化（无语义的 dense 通道按等权融合反而降低召回） | 上文 F1 对照表 |

## 6. 已知局限

- 未接真实 embedding 模型、未接真实 Milvus/ES，全部为本地后端；hybrid 的结论**只对 hash 向量成立**。
- 文档级指标在 13 份文档、每份约 24 chunk 的规模下区分度有限；`quote_hit` 是本报告自加的补充指标。
- 证据引文用严格子串匹配，chunk 为 800 字窗口。已核对：BM25+重排剩下的 2 次漏检**不是**窗口切断造成的
  ——引文（35 字、65 字）完整落在语料内单个 chunk 里，只是那个 chunk 没进 top-5，属真实排序失误。
- 延迟为单机 Windows + SQLite + 缓存关闭下的串行测量，不是并发压测结果（同一脚本四次重跑，p50 波动约 ±5%，
  并发跑测试时会被推高到 820 ms）；质量指标可复现（四次重跑逐位相同），
  且平台内的 passage 指标与脚本侧独立实现完全一致（结果 JSON 的 `passage_cross_check` 全为 true）。

## 7. 下一步

0. **已完成（2026-09-27）**：评测平台现在能承载难样本。
   - passage 级指标：`EvaluationExampleCreate.evidence_quote`
     → `evaluation_examples.evidence_quote`（migration `0008`）→ Runner 计算
     `passage_hit`/`passage_at_1`/`passage_mrr` 并逐题记录 `passage_rank`，
     评测结果因此恢复了区分配置的能力（见 §3 表格）。
   - 多跳与应拒答：`expected_document_id` 变可空，新增 `should_refuse` 与 `expected_evidence`
     （migration `0009`）。指标口径随之扩展：`recall@k` 在多个期望文档上取集合命中比例，
     另有严格的 `all_targets@k`（每一跳都要进 top-k）；`page_hit` 要求每个期望 (文档,页) 都在；
     `passage_hit` 要求**每一跳**的引文都命中，`passage_mrr` 按跳取平均。
     应拒答样例不进任何检索指标，单独报 `negative_retrieved_rate`
     ——那是检索层的假阳性代理，真正的拒答判定要走答案链路，所以不与召回混在一起。
     单跳数据集的数字与改造前逐位相同（既有测试守住）。
   - 降级保护：`downgrade` 遇到多跳/应拒答样本会**拒绝执行**并报出条数，而不是静默丢标签。
1. 修 `document_version="latest"` 语义（上传指定版本后默认查询命中 0 条，已记在 `docs/priority-fixes.md`）；
2. 按 F5 三条改造查询路径，并在同一脚本上重测 p50/p95，给出改造前后对照；
3. 拿到 embedding key 后重跑本脚本，验证 F1 的结论是否随语义向量反转（这才是能写"提升"的前提）；
4. 按 F3 的两个成因生成 v2 题集（不点名法规、同义改写、跨文档多跳、近邻干扰、应拒答负样本），
   用 passage 指标与 `all_targets@k` 做 v1/v2 基线对比——**这一步才能回答"BM25 的优势有多少靠词面泄漏"**。

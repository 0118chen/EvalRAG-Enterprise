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
  `quote_hit` = top-5 里存在包含证据引文的 chunk（本报告独有，用于补上文档级指标看不见的粒度损失）。

**随机基线**（同样 top-5、从 316 chunk 里随机抽，按每题相关 chunk 数精确计算，非估计）：

| 指标 | 随机 | BM25 | 
|---|---|---|
| 文档命中 | 0.246 | 0.981（Recall@1） |
| 页命中 | 0.160 | 1.000 |

所以这套题**不是**随机可解的；它的问题是"太容易"，不是"无意义"。

## 3. 结果

| 配置 | 它隔离的变量 | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | 纯 BM25 基线 | 0.981 | 1.000 | 1.000 | 0.990 | 0.993 | 0.993 | 1.000 | 0.962 | 685 | 866 |
| **sparse-bm25-rerank** | BM25 + 重排 | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | **1.000** | 0.962 | 697 | 851 |
| dense-hash | 只要 dense 通道 | 0.692 | 0.885 | 0.942 | 0.786 | 0.801 | 0.825 | 0.788 | 0.635 | **410** | 534 |
| hybrid-rrf | 两通道 RRF，不重排 | 0.904 | 0.981 | 1.000 | 0.941 | 0.947 | 0.956 | 0.904 | 0.827 | 1087 | 1311 |
| hybrid-rrf-rerank | 再加重排 | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.923 | 0.846 | 1098 | 1318 |
| hybrid-rrf-rerank-rewrite | 再加查询改写（产品默认） | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.942 | 0.865 | 1142 | 2268 |

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

BM25 的 R@1 0.981 → 加词面重排后 1.000（把那道仅排第 2 的题提上来）。代价是 p50 +12 ms，可以忽略。
但同一套重排在 hybrid 上只能从 0.904 补到 0.942，补不回 dense 造成的损伤。

### F3 指标饱和，这套题集无法区分配置

BM25+重排在 Recall@1/3/5、MRR、nDCG@3/5、page_hit 上全为 1.000。
继续做检索层"优化"在这个语料上**无法被验证**。要能看到差异，题集必须变难：
提问不再点名法规、跨文档多跳、同义改写（把"贷款期限"写成"借多久"）、以及引入真正相近的干扰文档。

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
| 定位到"每查询重建检索器/重复嵌入全语料"的性能问题并量化 | `scripts/profile_retrieval.py` 输出 |
| 发现并修掉负向优化（无语义的 dense 通道按等权融合反而降低召回） | 上文 F1 对照表 |

## 6. 已知局限

- 未接真实 embedding 模型、未接真实 Milvus/ES，全部为本地后端；hybrid 的结论**只对 hash 向量成立**。
- 文档级指标在 13 份文档、每份约 24 chunk 的规模下区分度有限；`quote_hit` 是本报告自加的补充指标。
- 证据引文用严格子串匹配，chunk 为 800 字窗口——若引文正好被窗口切断会记成未命中（实测最多影响 1–2 题）。
- 延迟为单机 Windows + SQLite + 缓存关闭下的串行测量，不是并发压测结果。

## 7. 下一步

1. 修 `document_version="latest"` 语义（上传指定版本后默认查询命中 0 条，已记在 `docs/priority-fixes.md`）；
2. 按 F5 三条改造查询路径，并在同一脚本上重测 p50/p95，给出改造前后对照；
3. 拿到 embedding key 后重跑本脚本，验证 F1 的结论是否随语义向量反转（这才是能写"提升"的前提）；
4. 扩充题集难度（见 F3）后再谈跨配置提升。

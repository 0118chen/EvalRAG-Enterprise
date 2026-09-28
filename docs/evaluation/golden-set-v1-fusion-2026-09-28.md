# Golden set experiment (52 questions, 18 documents, 386 chunks)

- commit: `56db26f`  
- generated: 2026-09-28T12:09:59.068323+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `lexical`  
- random 5-chunk draw: document hit 0.214, page hit 0.142 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.769 | 0.845 | 1.000 | — | — | 0.962 | 814.568 | 977.380 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.769 | 0.848 | 1.000 | — | — | 0.962 | 803.968 | 1065.936 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.673 | 0.885 | 0.923 | 0.771 | 0.794 | 0.810 | 0.769 | 0.269 | 0.382 | 0.923 | — | — | 0.615 | 478.858 | 562.320 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.904 | 0.981 | 1.000 | 0.944 | 0.950 | 0.958 | 0.904 | 0.538 | 0.646 | 1.000 | — | — | 0.827 | 1371.226 | 1581.254 |
| hybrid-rrf-weighted | equal-weight fusion was the problem: weight the weak dense channel down | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.923 | 0.615 | 0.712 | 1.000 | — | — | 0.865 | 1275.801 | 1426.456 |
| hybrid-rrf-truncate | only the top 5 of each channel may contribute, not its tail | 0.923 | 1.000 | 1.000 | 0.962 | 0.972 | 0.972 | 0.981 | 0.538 | 0.699 | 1.000 | — | — | 0.942 | 1266.305 | 1491.835 |
| hybrid-convex | keep the score magnitudes RRF throws away (min-max normalised sum) | 0.923 | 1.000 | 1.000 | 0.958 | 0.969 | 0.969 | 0.981 | 0.577 | 0.718 | 1.000 | — | — | 0.923 | 1266.364 | 1466.357 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.712 | 0.812 | 1.000 | — | — | 0.962 | 1240.212 | 1435.146 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.750 | 0.835 | 1.000 | — | — | 0.981 | 1329.245 | 1592.107 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.923 | 0.635 | 0.720 | 1.000 | — | — | 0.846 | 1324.790 | 1483.796 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.942 | 0.615 | 0.714 | 1.000 | — | — | 0.865 | 1269.228 | 2555.215 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 业务经营 | 1 | 1.000 | 1.000 | 1.000 |  |
| 业务规范 | 1 | 1.000 | 1.000 | 0.000 |  |
| 定义 | 11 | 0.909 | 0.909 | 1.000 |  |
| 定义与比例 | 1 | 1.000 | 1.000 | 1.000 |  |
| 施行日期 | 1 | 1.000 | 1.000 | 1.000 |  |
| 期限 | 8 | 1.000 | 1.000 | 0.875 |  |
| 期限与比例 | 1 | 1.000 | 1.000 | 1.000 |  |
| 法律责任 | 6 | 1.000 | 0.833 | 0.500 |  |
| 法律适用 | 1 | 1.000 | 1.000 | 1.000 |  |
| 流程 | 5 | 1.000 | 1.000 | 1.000 |  |
| 监督管理 | 4 | 1.000 | 0.750 | 0.750 |  |
| 监督管理与法律责任 | 1 | 1.000 | 1.000 | 1.000 |  |
| 组织治理 | 1 | 1.000 | 1.000 | 1.000 |  |
| 经营要求 | 1 | 1.000 | 1.000 | 1.000 |  |
| 设立条件 | 2 | 1.000 | 1.000 | 1.000 |  |
| 资金运用 | 1 | 1.000 | 1.000 | 1.000 |  |
| 适用对象 | 2 | 1.000 | 1.000 | 1.000 |  |
| 适用范围 | 4 | 1.000 | 1.000 | 0.750 |  |

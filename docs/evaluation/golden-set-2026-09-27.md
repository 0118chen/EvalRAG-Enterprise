# Golden set experiment (52 questions, 13 documents, 316 chunks)

- commit: `e7899ef`  
- generated: 2026-09-27T13:02:56.139261+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- random 5-chunk draw: document hit 0.246, page hit 0.160 (24.3 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.981 | 1.000 | 1.000 | 0.990 | 0.993 | 0.993 | 1.000 | 0.769 | 0.845 | 0.962 | 702.712 | 897.637 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.769 | 0.848 | 0.962 | 703.552 | 878.504 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.692 | 0.885 | 0.942 | 0.786 | 0.801 | 0.825 | 0.788 | 0.288 | 0.396 | 0.635 | 419.374 | 507.694 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.904 | 0.981 | 1.000 | 0.941 | 0.947 | 0.956 | 0.904 | 0.519 | 0.628 | 0.827 | 1122.755 | 1325.718 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.923 | 0.635 | 0.721 | 0.846 | 1135.125 | 1349.063 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.942 | 0.981 | 1.000 | 0.963 | 0.964 | 0.972 | 0.942 | 0.615 | 0.715 | 0.865 | 1192.520 | 2235.579 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit |
|---|---|---|---|
| 业务经营 | 1 | 1.000 | 1.000 |
| 业务规范 | 1 | 1.000 | 1.000 |
| 定义 | 11 | 0.909 | 0.909 |
| 定义与比例 | 1 | 1.000 | 1.000 |
| 施行日期 | 1 | 1.000 | 1.000 |
| 期限 | 8 | 1.000 | 1.000 |
| 期限与比例 | 1 | 1.000 | 1.000 |
| 法律责任 | 6 | 1.000 | 0.833 |
| 法律适用 | 1 | 1.000 | 1.000 |
| 流程 | 5 | 1.000 | 1.000 |
| 监督管理 | 4 | 1.000 | 0.750 |
| 监督管理与法律责任 | 1 | 1.000 | 1.000 |
| 组织治理 | 1 | 1.000 | 1.000 |
| 经营要求 | 1 | 1.000 | 1.000 |
| 设立条件 | 2 | 1.000 | 1.000 |
| 资金运用 | 1 | 1.000 | 1.000 |
| 适用对象 | 2 | 1.000 | 1.000 |
| 适用范围 | 4 | 1.000 | 1.000 |

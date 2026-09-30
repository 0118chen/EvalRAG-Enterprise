# Golden set experiment (16 questions, 8 documents, 8 chunks)

- commit: `1186dd9`  
- generated: 2026-09-30T00:59:25.512788+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `lexical`  
- random 5-chunk draw: document hit 0.625, page hit 0.625 (1.0 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.600 | 0.867 | 0.867 | 0.700 | 0.742 | 0.742 | 0.867 | 0.600 | 0.700 | 0.867 | — | 1.000 | 0.867 | 2.235 | 2.937 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.333 | 0.667 | 0.800 | 0.522 | 0.535 | 0.592 | 0.800 | 0.333 | 0.522 | 0.800 | — | 1.000 | 0.800 | 0.288 | 0.475 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.333 | 0.867 | 0.933 | 0.594 | 0.652 | 0.681 | 0.933 | 0.333 | 0.594 | 0.933 | — | 1.000 | 0.933 | 1.531 | 1.770 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 0.600 | 0.800 | 0.867 | 0.706 | 0.717 | 0.746 | 0.867 | 0.600 | 0.706 | 0.867 | — | 1.000 | 0.867 | 1.781 | 3.109 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 0.600 | 0.867 | 0.933 | 0.728 | 0.751 | 0.780 | 0.933 | 0.600 | 0.728 | 0.933 | — | 1.000 | 0.933 | 2.204 | 3.934 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 15 | 0.867 | 0.933 | 0.933 |  |
| 应拒答 | 1 | — | — | — | 无检索指标；检索到内容的比例 1.000，top 分数中位数 0.7 |

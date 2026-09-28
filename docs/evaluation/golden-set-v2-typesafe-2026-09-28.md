# Golden set experiment (75 questions, 18 documents, 386 chunks)

- commit: `56db26f`  
- generated: 2026-09-28T11:58:54.240642+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `typesafe` (TypeSafe, 1500/1500 calls, 1659530 input tokens, $0.0697)  
- random 5-chunk draw: document hit 0.265, page hit 0.207 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.660 | 0.851 | 0.944 | 0.790 | 0.792 | 0.829 | 0.851 | 0.612 | 0.728 | 0.934 | 1.000 | 1.000 | 0.821 | 813.722 | 1114.259 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 0.646 | 0.868 | 0.929 | 0.786 | 0.801 | 0.823 | 0.866 | 0.582 | 0.719 | 0.918 | 1.000 | 1.000 | 0.836 | 1270.213 | 1578.048 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 0.769 | 0.925 | 0.948 | 0.872 | 0.885 | 0.891 | 0.896 | 0.716 | 0.830 | 0.934 | 1.000 | 1.000 | 0.881 | 3085.960 | 3559.216 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 52 | 0.942 | 0.904 | 0.904 |  |
| 多跳 | 9 | 0.833 | 0.778 | 0.667 |  |
| 等价多标签 | 6 | 0.917 | 1.000 | 1.000 |  |
| 应拒答 | 8 | — | — | — | 无检索指标；检索到内容的比例 1.000，top 分数中位数 0.0 |

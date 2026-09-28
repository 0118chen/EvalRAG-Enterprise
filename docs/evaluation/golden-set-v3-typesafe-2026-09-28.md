# Golden set experiment (20 questions, 18 documents, 386 chunks)

- commit: `56db26f`  
- generated: 2026-09-28T11:52:14.428990+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `typesafe` (TypeSafe, 400/400 calls, 427239 input tokens, $0.017944)  
- random 5-chunk draw: document hit 0.168, page hit 0.088 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.800 | 0.850 | 0.950 | 0.850 | 0.832 | 0.875 | 0.900 | 0.700 | 0.775 | 0.950 | — | — | 0.850 | 748.143 | 966.698 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 0.850 | 0.900 | 0.950 | 0.887 | 0.882 | 0.903 | 0.950 | 0.700 | 0.750 | 0.950 | — | — | 0.800 | 1229.337 | 1340.959 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 0.950 | 0.950 | 1.000 | 0.963 | 0.950 | 0.972 | 1.000 | 0.950 | 0.950 | 1.000 | — | — | 0.950 | 3029.780 | 3209.371 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 16 | 0.938 | 1.000 | 0.938 |  |
| 测算表 | 4 | 1.000 | 1.000 | 1.000 |  |

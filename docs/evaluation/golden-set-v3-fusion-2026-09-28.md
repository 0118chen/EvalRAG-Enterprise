# Golden set experiment (20 questions, 18 documents, 386 chunks)

- commit: `56db26f`  
- generated: 2026-09-28T11:44:54.126678+00:00  
- embedding: hash (32d), dense=local, sparse=local  
- top_k: 5  
- rerank backend: `lexical`  
- random 5-chunk draw: document hit 0.168, page hit 0.088 (21.4 chunks/document)

| config | what it isolates | Recall@1 | Recall@3 | Recall@5 | MRR | nDCG@3 | nDCG@5 | page_hit | passage@1 | passage_mrr | all@5 | any@5 | neg_retrieved | quote_hit | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sparse-bm25 | BM25 only, the no-frills baseline | 0.800 | 0.850 | 0.950 | 0.850 | 0.832 | 0.875 | 0.900 | 0.700 | 0.775 | 0.950 | — | — | 0.850 | 741.427 | 826.462 |
| sparse-bm25-rerank | the reranking stage on top of BM25 alone | 0.800 | 0.850 | 0.950 | 0.850 | 0.832 | 0.875 | 0.900 | 0.700 | 0.775 | 0.950 | — | — | 0.850 | 700.004 | 856.753 |
| dense-hash | the dense channel on its own (32-d hash embedding, no semantics) | 0.300 | 0.400 | 0.450 | 0.343 | 0.350 | 0.369 | 0.300 | 0.050 | 0.112 | 0.450 | — | — | 0.250 | 438.507 | 549.977 |
| hybrid-rrf | RRF fusion of both channels, no rerank | 0.600 | 0.850 | 0.850 | 0.700 | 0.738 | 0.738 | 0.750 | 0.450 | 0.539 | 0.850 | — | — | 0.700 | 1170.143 | 1379.578 |
| hybrid-rrf-weighted | equal-weight fusion was the problem: weight the weak dense channel down | 0.650 | 0.850 | 0.850 | 0.733 | 0.763 | 0.763 | 0.750 | 0.500 | 0.571 | 0.850 | — | — | 0.700 | 1182.011 | 1362.312 |
| hybrid-rrf-truncate | only the top 5 of each channel may contribute, not its tail | 0.500 | 0.900 | 0.900 | 0.700 | 0.752 | 0.752 | 0.850 | 0.350 | 0.571 | 0.900 | — | — | 0.850 | 1163.713 | 1298.403 |
| hybrid-convex | keep the score magnitudes RRF throws away (min-max normalised sum) | 0.700 | 0.900 | 0.900 | 0.792 | 0.820 | 0.820 | 0.900 | 0.600 | 0.671 | 0.900 | — | — | 0.800 | 1191.010 | 1350.882 |
| hybrid-convex-weighted | normalised scores plus a down-weighted dense channel | 0.850 | 0.900 | 0.950 | 0.887 | 0.882 | 0.903 | 0.950 | 0.700 | 0.750 | 0.950 | — | — | 0.800 | 1111.788 | 1346.609 |
| hybrid-convex-weighted-rerank | the same shortlist through the reranker; which backend that is depends on RERANK_BACKEND, so this config name is the apples-to-apples slot for comparing lexical against TypeSafe | 0.850 | 0.900 | 0.950 | 0.887 | 0.882 | 0.903 | 0.950 | 0.700 | 0.760 | 0.950 | — | — | 0.850 | 1183.172 | 1353.455 |
| hybrid-rrf-rerank | reranking on top of the fusion | 0.650 | 0.850 | 0.850 | 0.733 | 0.763 | 0.763 | 0.750 | 0.550 | 0.589 | 0.850 | — | — | 0.700 | 1142.319 | 1310.512 |
| hybrid-rrf-rerank-rewrite | rule-based query expansion on top of the product default | 0.650 | 0.850 | 0.850 | 0.733 | 0.763 | 0.763 | 0.750 | 0.550 | 0.589 | 0.850 | — | — | 0.700 | 1157.958 | 1388.963 |

## Per category (product default configuration)

| category | examples | Recall@3 | page_hit | quote_hit | notes |
|---|---|---|---|---|---|
| 同义改写 | 16 | 0.812 | 0.688 | 0.625 |  |
| 测算表 | 4 | 1.000 | 1.000 | 1.000 |  |
